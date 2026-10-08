#!/usr/bin/env bash
#
# roj-local-judge-lite 一键安装脚本
#
# 直接从网络安装：
#   curl -fsSL https://raw.githubusercontent.com/rainboyOJ/roj-local-judge-lite/master/install.sh | bash
#
# 带参数（`| bash` 时参数要放在 -s -- 后面）：
#   curl -fsSL .../install.sh | bash -s -- --ref v0.1.0 --dir /opt/roj-local-judge-lite --force
#
# 本地运行：
#   bash install.sh --help
#
# 说明：本脚本必须支持 `curl | bash`，所以 stdin 是脚本自身，全程不做交互提问，
# 所有选择都通过命令行参数或环境变量给出。安装过程是「先在同级暂存目录完整构建，
# 成功后再整体搬进目标目录」，因此失败不会留下半个装好的目录。

if [ -z "${BASH_VERSION:-}" ]; then
    echo "请用 bash 运行本脚本：curl -fsSL <url> | bash" >&2
    exit 1
fi

set -Eeuo pipefail

REPO="rainboyOJ/roj-local-judge-lite"
REF="master"
DEST="${HOME:-}/.local/share/roj-local-judge-lite"
BIN_DIR="${HOME:-}/.local/bin"
MIRROR="https://gh-proxy.com"
USE_MIRROR=1
DO_BUILD=1
DO_SMOKE=1
DO_LAUNCHER=1
FORCE=0
TESTDATA=""

if [ -t 1 ]; then
    C_INFO=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
    C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_INFO=; C_OK=; C_WARN=; C_ERR=; C_BOLD=; C_OFF=
fi

info() { printf '%s==>%s %s\n' "$C_INFO" "$C_OFF" "$*"; }
ok() { printf '%s ok %s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die() { printf '%s错误%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
用法：install.sh [选项]

从 GitHub 克隆本仓库，构建 C helper 后安装到用户目录，
并在 ~/.local/bin 放一个启动器。

网络安装：
  curl -fsSL https://raw.githubusercontent.com/rainboyOJ/roj-local-judge-lite/master/install.sh | bash
  curl -fsSL <同上> | bash -s -- --ref v0.1.0 --force

选项：
  --ref <ref>        安装的分支、tag 或 commit（默认 master）
  --dir <path>       安装目录（默认 ~/.local/share/roj-local-judge-lite）
  --bin-dir <path>   启动器目录（默认 ~/.local/bin）
  --repo <owner/name> 仓库，便于装自己的 fork（默认 rainboyOJ/roj-local-judge-lite）
  --mirror <prefix>  GitHub 镜像前缀（默认 https://gh-proxy.com）
  --no-mirror        只直连 GitHub，不自动回退镜像
  --testdata <path>  指定测试数据目录，仅用于安装后的冒烟测试
  --no-build         不构建 runner_helper（跳过 cc/make 依赖）
  --no-smoke         跳过安装后的冒烟测试
  --no-launcher      不创建 ~/.local/bin 启动器
  -f, --force        目标目录已存在时直接覆盖
  -h, --help         显示本帮助

依赖：git、python3(>=3.8)，构建时需要 make 和一个 C 编译器。
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref 需要一个值}"; shift 2 ;;
        --dir) DEST="${2:?--dir 需要一个值}"; shift 2 ;;
        --bin-dir) BIN_DIR="${2:?--bin-dir 需要一个值}"; shift 2 ;;
        --repo) REPO="${2:?--repo 需要一个值}"; shift 2 ;;
        --mirror) MIRROR="${2:?--mirror 需要一个值}"; shift 2 ;;
        --no-mirror) USE_MIRROR=0; shift ;;
        --testdata) TESTDATA="${2:?--testdata 需要一个值}"; shift 2 ;;
        --no-build) DO_BUILD=0; shift ;;
        --no-smoke) DO_SMOKE=0; shift ;;
        --no-launcher) DO_LAUNCHER=0; shift ;;
        -f|--force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（用 --help 查看用法）" ;;
    esac
done

[ -n "${HOME:-}" ] || die "HOME 未设置，请用 --dir 和 --bin-dir 显式指定目录"

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------

command -v git >/dev/null 2>&1 || die "需要 git，请先安装"
command -v python3 >/dev/null 2>&1 || die "需要 python3，请先安装"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 8) else 1)' \
    || die "需要 Python 3.8 或更高版本：$(python3 -V 2>&1)"

if [ "$DO_BUILD" = 1 ]; then
    command -v make >/dev/null 2>&1 || die "需要 make，或用 --no-build 跳过构建"
    found_cc=""
    for candidate in cc gcc clang; do
        if command -v "$candidate" >/dev/null 2>&1; then found_cc="$candidate"; break; fi
    done
    [ -n "$found_cc" ] || die "需要一个 C 编译器（cc/gcc/clang），或用 --no-build 跳过构建"
fi

# ---------------------------------------------------------------------------
# 下载：先直连，失败再走镜像
# ---------------------------------------------------------------------------

sources=("https://github.com/$REPO.git")
if [ "$USE_MIRROR" = 1 ] && [ -n "$MIRROR" ]; then
    sources+=("$MIRROR/https://github.com/$REPO.git")
fi

is_sha() { [[ "$1" =~ ^[0-9a-fA-F]{7,40}$ ]]; }

clone_repo() {
    local url="$1" dest="$2"
    rm -rf "$dest"
    if is_sha "$REF"; then
        # git clone --branch 不接受裸 commit，只能完整克隆后再 checkout。
        git clone --quiet "$url" "$dest" 2>/dev/null || return 1
        git -C "$dest" checkout --quiet "$REF" 2>/dev/null || return 1
    else
        git clone --quiet --depth 1 --branch "$REF" "$url" "$dest" 2>/dev/null || return 1
    fi
    return 0
}

stage="$(mktemp -d "${TMPDIR:-/tmp}/roj-local-judge-lite.XXXXXX")"
cleanup() { [ -n "${stage:-}" ] && rm -rf "$stage"; }
trap cleanup EXIT

src="$stage/src"
cloned=0
for url in "${sources[@]}"; do
    info "克隆 $REPO（ref=$REF）"
    if clone_repo "$url" "$src"; then
        cloned=1
        break
    fi
    warn "克隆失败：$url"
done
[ "$cloned" = 1 ] || die "所有下载源都失败，请检查网络，或用 --mirror 指定其他镜像"

[ -f "$src/local_judge.py" ] || die "$REPO 的 $REF 里找不到 local_judge.py，请确认 --ref 是否正确"

pkg="$stage/pkg"
mkdir -p "$pkg"
cp -a "$src/." "$pkg/"
# 克隆下来的 .git、构建产物和缓存都不该跟着安装包走；有就跑一次干净的 make。
rm -rf "$pkg/.git" "$pkg/__pycache__" "$pkg/runner_helper"

if [ "$DO_BUILD" = 1 ]; then
    info "构建 C helper（runner_helper）"
    if ! make -C "$pkg" >"$stage/make.log" 2>&1; then
        cat "$stage/make.log" >&2
        die "构建失败，请检查上面的编译错误"
    fi
    [ -x "$pkg/runner_helper" ] || die "构建结束但没有生成 runner_helper"
    ok "runner_helper 构建完成"
fi

# ---------------------------------------------------------------------------
# 冒烟测试：证明装出来的东西真的能用
# ---------------------------------------------------------------------------

if [ "$DO_SMOKE" = 1 ]; then
    info "冒烟测试"
    if ! ( cd "$pkg" && python3 -c "import local_judge, memory_cgroup, runner" ); then
        die "模块导入失败"
    fi
    ok "模块导入正常"

    if [ "$DO_BUILD" = 1 ]; then
        if ( cd "$pkg" && make check >"$stage/check.log" 2>&1 ); then
            ok "单元测试通过：$(grep -E '^Ran ' "$stage/check.log" | tail -1)"
        else
            warn "make check 未通过，建议排查："
            tail -n 15 "$stage/check.log" >&2
        fi
    fi

    cgroup_state="$( cd "$pkg" && python3 -c '
import local_judge
ready, why = local_judge.check_cgroup_root(local_judge.DEFAULT_CGROUP_ROOT)
print("ready" if ready else why)
' 2>&1 )" || cgroup_state="探测失败"
    if [ "$cgroup_state" = "ready" ]; then
        ok "cgroup v2 已可用，内存和 CPU 判定是精确的"
    else
        warn "cgroup v2 现在不可用（$cgroup_state）"
        info "local_judge.py 会自动用 systemd-run 起委派 scope，再不行就降级为只限 wall 和 CPU"
    fi

    if [ -z "$TESTDATA" ]; then
        for candidate in "$PWD/testData" "$PWD/../testData"; do
            if [ -d "$candidate" ]; then TESTDATA="$candidate"; break; fi
        done
    fi
    if [ -n "$TESTDATA" ] && [ -d "$TESTDATA" ]; then
        if ( cd "$pkg" && python3 local_judge.py --testdata "$TESTDATA" --list >"$stage/list.log" 2>&1 ); then
            ok "测试数据可用：$TESTDATA（$(grep -c '个测试点' "$stage/list.log") 道题）"
        else
            warn "能读到 $TESTDATA，但 --list 失败："
            tail -n 5 "$stage/list.log" >&2
        fi
    else
        info "没找到测试数据目录，跳过 --list；评测时用 --testdata 指定即可"
    fi
fi

# ---------------------------------------------------------------------------
# 安装到目标目录：前面都成功了才动目标目录
# ---------------------------------------------------------------------------

if [ -e "$DEST" ] && [ "$FORCE" != 1 ]; then
    die "$DEST 已存在。加 --force 覆盖，或用 --dir 换一个目录"
fi

# 冒烟测试会在包目录里重新生成 __pycache__，装出去之前再清一次。
find "$pkg" -name '__pycache__' -type d -prune -exec rm -rf {} +

info "安装到 $DEST"
mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
mv "$pkg" "$DEST"
ok "已安装（$(cd "$DEST" && ls | tr '\n' ' ' | sed 's/ $//')）"

launcher=""
if [ "$DO_LAUNCHER" = 1 ]; then
    mkdir -p "$BIN_DIR"
    launcher="$BIN_DIR/roj-local-judge-lite"
    cat >"$launcher" <<EOF
#!/bin/sh
# 由 roj-local-judge-lite 的 install.sh 生成；重新安装会覆盖本文件。
exec python3 "$DEST/local_judge.py" "\$@"
EOF
    chmod 0755 "$launcher"
    ok "启动器已就绪：$launcher"
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR 不在 PATH 里，把它加进 PATH 后才能在任意目录调用 roj-local-judge-lite" ;;
    esac
fi

printf '\n%s安装完成%s\n' "$C_BOLD" "$C_OFF"
printf '  安装目录  %s\n' "$DEST"
[ -n "$launcher" ] && printf '  启动器    %s\n' "$launcher"
cat <<EOF

开始评测（在有 testData/ 的项目目录下执行）：
  roj-local-judge-lite --list                         # 看有哪些题
  roj-local-judge-lite --pid 1000 solution.cpp        # 评测 C++ 提交
  roj-local-judge-lite --help                         # 全部参数

测试数据不在当前目录时显式指定：
  roj-local-judge-lite --pid 1000 solution.cpp --testdata /path/to/testData
EOF
