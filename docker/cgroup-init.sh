#!/usr/bin/env bash
# 在容器内准备一个已委派且启用 memory controller 的 cgroup v2 父目录，
# 供 local_judge.py --cgroup-root 使用。只操作自己创建的目录，不动系统其他组。
#
# 需要 --privileged（cgroup 文件系统在普通容器里是只读的）。
set -u

CGROUP_ROOT="${CGROUP_ROOT:-/sys/fs/cgroup}"
JUDGE_DIR="${JUDGE_DIR:-$CGROUP_ROOT/judge}"

log() { echo "[cgroup-init] $*" >&2; }

if [ ! -f "$CGROUP_ROOT/cgroup.controllers" ]; then
  log "cgroup2 未挂载，尝试挂载"
  mount -t cgroup2 none "$CGROUP_ROOT" 2>/dev/null || { log "挂载失败"; exit 1; }
fi

if ! mkdir -p "$JUDGE_DIR" 2>/dev/null; then
  log "cgroup 只读，尝试 remount rw"
  mount -o remount,rw "$CGROUP_ROOT" 2>/dev/null || { log "remount 失败，请加 --privileged"; exit 1; }
  mkdir -p "$JUDGE_DIR" || { log "mkdir $JUDGE_DIR 失败"; exit 1; }
fi

# domain controller 要求管理目录自身没有进程，因此先把 root 里的进程（含 PID 1）
# 迁到 init/ 叶子组。迁移过程中随时可能有新进程落进 root，所以下面重试。
mkdir -p "$CGROUP_ROOT/init" || { log "mkdir init 失败"; exit 1; }

migrate_all() {
  for pid in $(cat "$CGROUP_ROOT/cgroup.procs" 2>/dev/null); do
    echo "$pid" > "$CGROUP_ROOT/init/cgroup.procs" 2>/dev/null
  done
}

enable_memory() {
  local dir="$1" tries=0
  while [ "$tries" -lt 20 ]; do
    migrate_all
    if echo "+memory" > "$dir/cgroup.subtree_control" 2>/dev/null; then return 0; fi
    tries=$((tries + 1))
    sleep 0.05
  done
  return 1
}

enable_memory "$CGROUP_ROOT" || { log "$CGROUP_ROOT 启用 memory 失败"; exit 1; }
enable_memory "$JUDGE_DIR" || { log "$JUDGE_DIR 启用 memory 失败"; exit 1; }

log "就绪：$JUDGE_DIR（subtree_control = $(cat "$JUDGE_DIR/cgroup.subtree_control")）"
