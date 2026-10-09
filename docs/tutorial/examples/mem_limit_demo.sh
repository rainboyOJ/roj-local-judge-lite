#!/usr/bin/env bash
#
# 普通用户不用 root，用 cgroup 限制一个程序的内存。
#
# 用法（在仓库根目录执行）：
#   bash docs/tutorial/examples/mem_limit_demo.sh
#   bash docs/tutorial/examples/mem_limit_demo.sh --limit 64M --eat 200
#
# 脚本会自己申请一个带委派的 scope，所以不需要你先手动敲 systemd-run。
#
# 配套文章：docs/tutorial/cgroup-user-memory.md

# 这里不用 set -e：我们要在程序被 OOM 杀掉（退出码 137）之后继续读成绩。
set -uo pipefail

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
# 先把原始参数存下来。下面的解析循环会用 shift 把它们一个个吃掉，
# 如果之后需要重新运行本脚本（自动委派那一步），必须用这份副本。
ORIG_ARGS=("$@")

LIMIT=256M        # 笼子上限，cgroup 接受 256M / 1G 这种写法
EAT=400           # 被限制的程序要申请多少 MB
HOLD=0            # 申请后保持几秒（0 表示申请完立刻退出）

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --limit) LIMIT=${2:?--limit 后面要跟一个值，例如 256M}; shift 2 ;;
        --eat)   EAT=${2:?--eat 后面要跟一个整数};           shift 2 ;;
        --hold)  HOLD=${2:?--hold 后面要跟一个秒数};          shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1（用 -h 看用法）" >&2; exit 2 ;;
    esac
done

[[ $LIMIT =~ ^[0-9]+[KMG]?$ ]] || { echo "--limit 格式不对：$LIMIT（应为 256M 这种）" >&2; exit 2; }
[[ $EAT =~ ^[0-9]+$ ]]         || { echo "--eat 格式不对：$EAT（应为整数 MB）" >&2; exit 2; }

SELF=$(readlink -f "$0")
EATER=$(dirname "$SELF")/mem_eater.py
[[ -f $EATER ]] || { echo "找不到配套程序 $EATER" >&2; exit 2; }

# ---------------------------------------------------------------------------
# 第一步：确认我们待在一个"可以自己建子组"的 cgroup 里
#
# 判断办法不是看路径名字，而是真的动手试一次：能不能在这里建一个子目录。
# 能建就说明这块地被委派给我们了。试完马上删掉，不留痕迹。
# ---------------------------------------------------------------------------
current_cgroup_dir() {
    printf '/sys/fs/cgroup%s\n' "$(awk -F: '/^0::/{print $3}' /proc/self/cgroup)"
}

can_manage_here() {
    local dir=$1 probe=$1/.probe-$$
    [[ -d $dir && -w $dir/cgroup.subtree_control ]] || return 1
    mkdir "$probe" 2>/dev/null || return 1
    rmdir "$probe" 2>/dev/null || return 1
    return 0
}

S=$(current_cgroup_dir)

if ! can_manage_here "$S"; then
    # 没被委派：请 systemd 开一块新地盘，然后重新运行本脚本。
    echo "当前 cgroup 不能自己建子组："
    echo "  $S"
    echo "请 systemd 开一块带委派的新地盘，重新运行本脚本……"
    echo
    user_flag=()
    [[ $EUID -ne 0 ]] && user_flag=(--user)
    exec systemd-run "${user_flag[@]}" --quiet --scope -p Delegate=yes -- \
        bash "$SELF" "${ORIG_ARGS[@]}"
fi

# ---------------------------------------------------------------------------
# 第二步：准备笼子
# ---------------------------------------------------------------------------
MANAGER="$S/manager"        # 本脚本自己住的小房间
BOX="$S/box-$$"             # 用来关被测程序的笼子

# 关键一步：父目录 $S 里不能有进程，否则下一步 +memory 会报
# "Device or resource busy"。所以先建 manager 房间，把自己搬进去。
mkdir -p "$MANAGER"
echo $$ >"$MANAGER/cgroup.procs"

# 把自己搬走之后，$S 里是不是真的空了？
#
# 这里必须用 readarray（bash 内建命令），不能用 $(cat ...)：后者会 fork 出
# 一个子进程，而那个子进程此刻正好也在 $S 里，会让这里永远读到「非空」。
remaining=()
readarray -t remaining <"$S/cgroup.procs"

if (( ${#remaining[@]} > 0 )); then
    # 还有别人占着 $S。我们没法把它赶走，只能换一块干净的地盘。
    if [[ -n ${MEM_LIMIT_DEMO_SCOPE:-} ]]; then
        # 已经在自动委派出来的 scope 里了，再换就会无限循环，所以报错说清楚。
        {
            echo "无法启用 memory 控制器：$S 里还有别的进程。"
            echo "占用的 PID：${remaining[*]}"
            echo
            echo "cgroup v2 的规则：要往下分配资源的目录，自己里面不能有进程。"
            echo "请在一个干净的终端里单独运行本脚本，不要在别的委派 shell 里调用它。"
        } >&2
        exit 1
    fi
    echo "$S 里还有别的进程（${remaining[*]}），换一块干净的地盘重新运行……"
    echo
    user_flag=()
    [[ $EUID -ne 0 ]] && user_flag=(--user)
    exec systemd-run "${user_flag[@]}" --quiet --scope -p Delegate=yes \
        -E MEM_LIMIT_DEMO_SCOPE=1 -- bash "$SELF" "${ORIG_ARGS[@]}"
fi

# 打开 memory 开关（cgroup v2 里必须先为子组启用这个控制器）
if ! echo +memory >"$S/cgroup.subtree_control" 2>/dev/null; then
    echo "无法启用 memory 控制器：$S/cgroup.subtree_control" >&2
    exit 1
fi

mkdir "$BOX" || { echo "无法创建 $BOX" >&2; exit 1; }
echo "$LIMIT" >"$BOX/memory.max"
echo 0        >"$BOX/memory.swap.max"
# 这里故意不开 memory.oom.group（真实项目会开），这样只有肇事进程被杀，
# 统计数字也更干净：oom_kill 会是 1。

LIMIT_BYTES=$(cat "$BOX/memory.max")       # 内核会把 256M 换算成字节，顺便回读

# 不管怎么退出（正常结束、报错、Ctrl+C），都要杀掉笼子里的进程并删掉目录。
cleanup() {
    if [[ -d $BOX ]]; then
        echo 1 >"$BOX/cgroup.kill" 2>/dev/null
        local i
        for ((i = 0; i < 200; i++)); do
            grep -q 'populated 1' "$BOX/cgroup.events" 2>/dev/null || break
            sleep 0.005
        done
        rmdir "$BOX" 2>/dev/null || echo "警告：未能删除 $BOX（里面可能还有进程）" >&2
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 第三步：把被测程序放进笼子，看它被怎么处理
#
# 小技巧：$BASHPID 是"当前这个子 shell 的 PID"（$$ 不行，它始终是父 shell 的
# PID）。子 shell 先把自己写进 cgroup.procs，再用 exec 变成 python，
# 进程号不变，于是 python 一落地就在笼子里。
# ---------------------------------------------------------------------------
echo "═══════════════════════════════════════════════════════════════"
echo " cgroup 内存限制演示"
echo "═══════════════════════════════════════════════════════════════"
echo "  我是普通用户：$(id -un)  (uid=$(id -u))"
echo "  我的 cgroup ：$S"
echo "  笼子上限    ：$LIMIT（$LIMIT_BYTES 字节）"
echo "  申请量      ：${EAT}MB"
echo
echo "现在把程序放进笼子，让它申请 ${EAT}MB……"
echo

eat_args=("$EAT")
[[ $HOLD != 0 ]] && eat_args+=("$HOLD")

# 外面的 { ... } 2>/dev/null 只是为了遮掉 shell 自己那句 “Killed” 通知，
# 让输出干净；程序真正的退出码 137 仍会传出来。
{ ( echo $BASHPID >"$BOX/cgroup.procs"; exec python3 "$EATER" "${eat_args[@]}" ); } 2>/dev/null
EATER_RC=$?

# ---------------------------------------------------------------------------
# 第四步：先清空笼子，再读成绩
#
# 必须等笼子里没有进程了再读：只要还有进程在跑，内存峰值就可能继续往上走。
# ---------------------------------------------------------------------------
echo 1 >"$BOX/cgroup.kill"
for ((i = 0; i < 200; i++)); do
    grep -q 'populated 1' "$BOX/cgroup.events" 2>/dev/null || break
    sleep 0.005
done

PEAK=$(cat "$BOX/memory.peak")
OOM=$(awk '$1=="oom"{print $2}' "$BOX/memory.events")
OOM_KILL=$(awk '$1=="oom_kill"{print $2}' "$BOX/memory.events")

# 判定顺序和真实评测项目一致：先看 OOM 证据，再看峰值。
# 因为被 OOM 杀掉时，峰值会停在上限附近，已经不代表程序真实需求量。
if [[ ${OOM_KILL:-0} -gt 0 || ${OOM:-0} -gt 0 ]]; then
    VERDICT="MLE"
    REASON="cgroup 记录了 OOM 事件（内核把程序杀掉了）"
elif [[ $PEAK -gt $LIMIT_BYTES ]]; then
    VERDICT="MLE"
    REASON="内存峰值超过上限（程序自己跑完了，但峰值超了）"
else
    VERDICT="OK"
    REASON="没有超过上限"
fi

echo
echo "───────────────────────────────────────────────────────────────"
echo " 成绩"
echo "───────────────────────────────────────────────────────────────"
echo "  程序退出码  ：$EATER_RC $([[ $EATER_RC == 137 ]] && echo "（128+9，被 SIGKILL 强杀）")"
echo "  内存峰值    ：$PEAK 字节（$((PEAK / 1024 / 1024)) MB）"
echo "  笼子上限    ：$LIMIT_BYTES 字节（$((LIMIT_BYTES / 1024 / 1024)) MB）"
echo "  oom         ：${OOM:-0}"
echo "  oom_kill    ：${OOM_KILL:-0}"
echo
echo "  判定        ：$VERDICT —— $REASON"
echo "───────────────────────────────────────────────────────────────"
echo

echo "提示：本例只用了 memory.max 和 memory.swap.max 两个开关。真实评测"
echo "     项目还会打开 memory.oom.group，让超限时整组一起被杀，避免程序"
echo "     fork 出一堆子进程各自钻空子。"
echo

exit 0
