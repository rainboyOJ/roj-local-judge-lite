#!/usr/bin/env bash
# 在 Linux 容器里跑 local_judge.py，让 macOS 也能用这台评测机。
#
#   ./docker/judge.sh --pid 1000 solution.cpp
#   ./docker/judge.sh --testdata ~/data/testData --pid 1000 solution.py
#   ./docker/judge.sh --no-cgroup --pid 1000 solution.cpp   # 无 --privileged 时
#
# 提交文件与 --testdata / --checker 指向的路径都按宿主机原路径挂进容器，
# 因此容器内的命令行参数与宿主机完全一致。
set -euo pipefail

IMAGE="${IMAGE:-roj-local-judge-lite}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat >&2 <<'EOF'
用法：docker/judge.sh [local_judge.py 的参数...]

参数原样转发给容器内的 python3 local_judge.py。额外支持：
  --no-cgroup   不申请 --privileged，走降级模式（MLE 无法判定）
  --rebuild     强制重新构建镜像
  -h, --help    显示本帮助

环境变量：
  IMAGE         镜像名（默认 roj-local-judge-lite）
EOF
}

privileged=1
rebuild=0
passthrough=()
for arg in "$@"; do
  case "$arg" in
    --no-cgroup) privileged=0 ;;
    --rebuild) rebuild=1 ;;
    -h|--help) usage; exit 0 ;;
    *) passthrough+=("$arg") ;;
  esac
done

if [ "${#passthrough[@]}" -eq 0 ]; then
  usage
  echo >&2
  echo "错误：至少要给一个参数（例如 --pid 1000 solution.cpp）。" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "错误：找不到 docker。请先安装 Docker Desktop / OrbStack / colima。" >&2
  exit 1
fi

if [ "$rebuild" = 1 ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "构建镜像 $IMAGE ..." >&2
  docker build -t "$IMAGE" -f "$REPO_DIR/docker/Dockerfile" "$REPO_DIR" >&2
fi

# 逐个参数判断：它是不是一个需要挂进容器的宿主机路径。
# 命中后把参数改写成绝对路径，容器内 cwd 是 /work，相对路径会失效。
mounts=()
normalized=()
add_mount() {
  local abs="$1"
  for existing in ${mounts[@]+"${mounts[@]}"}; do
    [ "$existing" = "$abs" ] && return 0
  done
  mounts+=("$abs")
}
resolve_path() {
  # 输出绝对路径；目录取自身，文件取自身，都不经由符号链接展开。
  local path="$1"
  ( cd "$(dirname "$path")" 2>/dev/null && printf '%s/%s' "$(pwd)" "$(basename "$path")" )
}

prev_opt=""
for arg in "${passthrough[@]}"; do
  value=""
  case "$arg" in
    --testdata=*|--checker=*) value="${arg#*=}" ;;
    --testdata|--checker) prev_opt="$arg"; normalized+=("$arg"); continue ;;
    --*) normalized+=("$arg"); prev_opt="$arg"; continue ;;
    *)
      if [ "$prev_opt" = "--testdata" ] || [ "$prev_opt" = "--checker" ]; then
        value="$arg"
      elif [ -f "$arg" ]; then
        value="$arg"
      fi
      ;;
  esac
  if [ -n "$value" ] && [ -e "$value" ]; then
    abs="$(resolve_path "$value")"
    add_mount "$abs"
    normalized+=("$abs")
  else
    normalized+=("$arg")
  fi
  prev_opt="$arg"
done

docker_args=(--rm)
if [ "$privileged" = 1 ]; then
  docker_args+=(--privileged)
fi
for m in ${mounts[@]+"${mounts[@]}"}; do
  docker_args+=(-v "$m:$m:ro")
done

if [ "$privileged" = 1 ]; then
  inner=(bash -lc 'cp -r /work /judge && cd /judge && make -s && cgroup-init.sh && exec python3 /judge/local_judge.py "$@"' _
         --cgroup-root /sys/fs/cgroup/judge)
else
  inner=(bash -lc 'cp -r /work /judge && cd /judge && make -s && exec python3 /judge/local_judge.py "$@"' _ --no-cgroup)
fi

# 仓库以只读方式挂载，容器内先拷一份再构建：避免把 Linux ELF runner_helper
# 写回宿主机工作区（那个路径在 macOS 上不可执行，还会污染 git status）。
exec docker run "${docker_args[@]}" \
  -v "$REPO_DIR:/work:ro" \
  "$IMAGE" "${inner[@]}" "${normalized[@]}"
