#!/usr/bin/env bash
#
# 一步一步看懂：普通用户怎样"拿到" cgroup，然后执行 runner_helper。
#
# 这份脚本刻意写得啰嗦，每一步都只做一件事，并立刻把结果打印出来。
# 它不依赖 memory_cgroup.py，只用普通 shell 命令，让你看清底层到底发生了什么。
#
# 用法（在仓库根目录执行）：
#   bash docs/tutorial/examples/delegate_step_by_step.sh
#
# 脚本会自己完成"委派"，不需要你先手动敲 systemd-run。

# 这里故意不写 set -e：脚本要主动制造几个"失败"，来展示限制的存在。
set -uo pipefail

# ---------------------------------------------------------------------------
# 第一阶段 / 第二阶段 的分界
#
# 同一个脚本会被执行两次：
#   第一次：在你当前终端里（"外面"）  → 展示你做不到什么
#   第二次：在 systemd 新建的 scope 里（"里面"）→ 展示拿到了什么
#
# 靠第一个参数区分，而不是靠环境变量：因为 systemd-run 不会把我们自定义的
# 环境变量带进新 scope（后面会亲眼看到这一点）。
# ---------------------------------------------------------------------------
PHASE=${1:-outside}

# 新概念 ①：cgroup
#   Linux 用"组"来统计和限制一组进程的资源。它长得像一棵目录树，挂在
#   /sys/fs/cgroup 下。目录里的特殊文件（如 memory.max）不是普通文件，
#   写进去就等于给内核下命令。
#
# 新概念 ②：scope
#   systemd（Linux 的"进程总管"）给一条命令开的一块 cgroup 地盘。
#   你可以在里面自由建子目录。

# 新概念 ③：委派（delegate）
#   把某块 cgroup 地盘的所有权交给你的用户，之后你就能在里面随便建子目录。
#   默认不给你——因为 cgroup 目录归 root 所有。

print_where_am_i() {
    local path
    path=$(awk -F: '/^0::/{print $3}' /proc/self/cgroup)
    echo "  我当前所在的 cgroup："
    echo "    $path"
    echo
    echo "  对应的实际目录："
    echo "    /sys/fs/cgroup$path"
}

# shell 遇到写失败时，报错信息里会带上脚本路径和行号。压成一行人话，
# 只留下真正的原因（如 Permission denied）。
brief_error() {
    sed 's/.*: //' <<<"$1"
}

# ===========================================================================
#                            第一阶段：外面
# ===========================================================================
if [[ $PHASE == outside ]]; then

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║  阶段一：在你现在的终端里，看看自己能对 cgroup 做什么            ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo
echo "【步骤 1】我在哪？"
echo
print_where_am_i
echo

MY_GROUP="/sys/fs/cgroup$(awk -F: '/^0::/{print $3}' /proc/self/cgroup)"

echo "【步骤 2】这个目录归谁所有？"
echo
ls -ld "$MY_GROUP" | sed 's/^/    /'
echo
echo "  看第 3、4 列：owner 和 group 都是 root。"
echo "  你虽然“能读”它，但没有写权限——所以不能在这里建子目录。"
echo

echo "【步骤 3】不信？试着建一个子目录"
echo
if err=$(mkdir "$MY_GROUP/demo-test" 2>&1); then
    echo "    居然成功了（你的环境和本文假设不同）；先删掉"
    rmdir "$MY_GROUP/demo-test" 2>/dev/null
else
    echo "    $(brief_error "$err")"
    echo
    echo "  ↑ 失败了。记住这一点：cgroup 不是“随便建文件夹就能用”的，"
    echo "    权限归 root，普通用户在这里没有写权限。"
fi
echo

echo "【步骤 4】试着开启 memory 控制器"
echo
echo "  （控制器 = 内核提供的功能开关，memory 这个开关负责内存统计和限制）"
echo
# 注意这里的 { ...; } 2>&1 写法。echo 是 shell 内建命令，"重定向本身失败"
# 时错误来自 shell 而不是 echo，直接写 err=$(echo ... 2>&1) 是捕不到的；
# 用 { } 把整个命令包起来，才能把重定向阶段的错误也接到管道里。
if err=$( { echo +memory >"$MY_GROUP/cgroup.subtree_control"; } 2>&1 ); then
    echo "    居然成功了（同样说明环境和本文假设不同）"
else
    echo "    $(brief_error "$err")"
    echo
    echo "  ↑ 也失败了。两方面原因："
    echo "    ① 权限归 root，你写不进去；"
    echo "    ② 这个目录里还装着别的进程（见下一步），规则不允许。"
fi
echo

echo "【步骤 5】这个目录里装了哪些进程？"
echo
count=$(cat "$MY_GROUP/cgroup.procs" 2>/dev/null | wc -l)
echo "    共 $count 个进程" | sed 's/^/  /'
echo "    （前几个：$(cat "$MY_GROUP/cgroup.procs" 2>/dev/null | head -3 | tr '\n' ' ')）"
echo
echo "  这就是整个桌面会话的进程组。你当然不希望动它——"
echo "  所以需要 systemd 给你“另开一块干净的地盘”。"
echo

echo "【步骤 6】请 systemd 帮我们开一块地盘"
echo
echo "  要执行的命令是："
echo
echo "    systemd-run --user --scope -p Delegate=yes -- bash 本脚本 inside"
echo
echo "  三个部分的意思："
echo "    --user           用“你自己的” systemd，而不是系统级的（后者要 root）"
echo "    --scope          立刻把这条命令放进一块新的 cgroup 地盘执行"
echo "    -p Delegate=yes  把这块地盘的“所有权”交给你（这就是“委派”）"
echo
echo "  下面真的执行它。注意看 systemd 报告它创建的 scope 名字。"
echo
echo "───────────────────────────────────────────────────────────────────"
echo

USER_FLAG=()
[[ $EUID -ne 0 ]] && USER_FLAG=(--user)

# 这里是"重生"：脚本在新 scope 里被重新执行一遍。
# 把 $0（本脚本路径）和 inside 一起传进去，让第二次运行知道自己在里面。
exec systemd-run "${USER_FLAG[@]}" --scope -p Delegate=yes -- bash "$0" inside

fi  # 阶段一结束。下面这段只会在"里面"执行。

# ===========================================================================
#                            第二阶段：里面
# ===========================================================================

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║  阶段二：现在已经在 systemd 新建的 scope 里了                     ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo
echo "【步骤 7】现在我在哪？"
echo
print_where_am_i
echo

SCOPE_DIR="/sys/fs/cgroup$(awk -F: '/^0::/{print $3}' /proc/self/cgroup)"

echo "  对比阶段一的路径：它不再是 session-3.scope（你的桌面会话），"
echo "  而是一个全新的、只属于本脚本的 scope。"
echo

echo "【步骤 8】这块地盘的属主变了吗？"
echo
ls -ld "$SCOPE_DIR" | sed 's/^/    /'
echo
echo "  ↑ owner 现在是 $(stat -c %U "$SCOPE_DIR")，不是 root 了。"
echo "    这就是 Delegate=yes 做的事：把这棵子树的写权限交给了你。"
echo

echo "【步骤 9】这块地盘能用哪些控制器？"
echo
echo "    $(cat "$SCOPE_DIR/cgroup.controllers")" | sed 's/^/  /'
echo
echo "  里面有 memory，说明内存控制器在这里可用。"
echo "  但“可用”不等于“已开启”——控制器需要显式打开才能给子组用。"
echo

echo "【步骤 10】试着立刻开启 memory 控制器"
echo
echo "  （这一步是用来“撞墙”的，让你看清 cgroup v2 的一条硬规则）"
echo
if err=$( { echo +memory >"$SCOPE_DIR/cgroup.subtree_control"; } 2>&1 ); then
    echo "    成功：$(cat "$SCOPE_DIR/cgroup.subtree_control")"
    ALREADY_ENABLED=1
else
    echo "    $(brief_error "$err")"
    ALREADY_ENABLED=0
fi
echo
echo "  为什么失败？因为规则是："
echo
echo "    “一个要给子组下发资源的目录，自己里面不能有普通进程。”"
echo
echo "  而本脚本自己正待在这个目录里。所以这不是权限问题，是规则问题。"
echo

echo "【步骤 11】把自己搬到一个“叶子”目录里"
echo
echo "  办法：先建一个子目录 manager，把自己搬进去。"
echo "  这样父目录就空了，空目录才允许开启控制器。"
echo
mkdir -p "$SCOPE_DIR/manager"
echo "    mkdir $SCOPE_DIR/manager"

# 新概念 ④：入组 = 往 cgroup.procs 写一个 PID
#   写谁的 PID，就把那个进程搬进这个组。所以写入的内容具体是什么很重要：
#     · 写数字 0   = 特殊约定，表示“写这个文件的进程自己”（不需要先知道 PID）
#     · 写具体 PID = 搬指定的那个进程（通常需要更高权限）
#   这里我们直接写 $$（本脚本自己的 PID），效果与写 0 等价，但更直观。
#   runner_helper 让被测程序入组时用的是写 0（那时子进程还不知道自己的 PID）。
if err=$( { echo $$ >"$SCOPE_DIR/manager/cgroup.procs"; } 2>&1 ); then
    echo "    echo \$\$ > $SCOPE_DIR/manager/cgroup.procs    # \$\$ = 本脚本的 PID（$$）"
else
    echo "    把自己搬进 manager 失败：$(brief_error "$err")"
    echo "    后面的步骤无法继续，脚本退出。"
    exit 1
fi
echo
echo "  搬完了。现在父目录里的进程："
echo "    $(cat "$SCOPE_DIR/cgroup.procs" 2>/dev/null | wc -l) 个"
echo

echo "【步骤 12】再试一次开启 memory 控制器"
echo
if [[ $ALREADY_ENABLED == 0 ]]; then
    if err=$( { echo +memory >"$SCOPE_DIR/cgroup.subtree_control"; } 2>&1 ); then
        echo "    成功：$(cat "$SCOPE_DIR/cgroup.subtree_control")"
    else
        echo "    仍然失败：$(brief_error "$err")"
        echo "    下面的步骤可能无法继续，脚本退出。"
        exit 1
    fi
else
    echo "    已是：$(cat "$SCOPE_DIR/cgroup.subtree_control")"
fi
echo
echo "  ↑ 同样的命令，这次成功了。唯一的变化是：父目录里没有进程了。"
echo "    现在可以给子组提供内存统计和限制了。"
echo

echo "【步骤 13】建一个属于本次运行的 case 组"
echo
GROUP="$SCOPE_DIR/case-$RANDOM$RANDOM"
mkdir "$GROUP"
echo "    mkdir $GROUP"
echo
echo "  跟 memory_cgroup.py 用的 uuid 是同一个思路：每次运行都用新目录，"
echo "  这样内存计数从零开始，不会和上一次运行的残留混在一起。"
echo

echo "【步骤 14】写入内存限制配置"
echo
echo "  这三个都是“写进去就生效”的内核接口，不是普通配置文件。"
echo
JUDGE_KB=$((8 * 1024))                       # 判定线：超过就判 MLE
SLACK_KB=$((16 * 1024))                      # 保护余量：留点缓冲，避免踩线误杀
MAX_BYTES=$(((JUDGE_KB + SLACK_KB) * 1024))  # 硬上限：真超了就 OOM 杀掉

echo "$MAX_BYTES" >"$GROUP/memory.max"
echo 0            >"$GROUP/memory.swap.max"
echo 1            >"$GROUP/memory.oom.group"
echo "    echo $MAX_BYTES > \$GROUP/memory.max        # 硬上限 24MiB（判定线 8 + 余量 16）"
echo "    echo 0            > \$GROUP/memory.swap.max   # 禁止用 swap，否则超限内存可以“换出去”逃掉"
echo "    echo 1            > \$GROUP/memory.oom.group  # 超限时按整组处理，不只是杀一个进程"
echo
echo "  回读 memory.max = $(cat "$GROUP/memory.max") 字节"
echo

echo "【步骤 15】终于可以执行程序了——调用 runner_helper"
echo
echo "  注意：如果我们在 /sys/fs/cgroup 下已经有写权限了，"
echo "  直接跑一个程序不就行了吗？为什么还要 helper？"
echo
echo "  因为 helper 负责的是“内存管不到”的那些事："
echo "    · fork 出被测程序，并让它与监控者分离"
echo "    · 设置 CPU / 栈 / 输出大小等 rlimit"
echo "    · 用 wait4 拿到准确的 CPU 时间和退出状态"
echo "    · 做 wall-clock 超时看门狗"
echo
echo "  而 cgroup 负责内存。两者分工，Python 负责决定“判什么”。"
echo

HELPER="$(cd "$(dirname "$0")/../../.." && pwd)/runner_helper"
if [[ ! -x $HELPER ]]; then
    echo "  找不到 $HELPER，请先在仓库根目录运行 make"
    echo
    echo "  这一步跳过，直接看【步骤 16】的说明。"
    SKIP_HELPER=1
else
    SKIP_HELPER=0
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/delegate-demo-XXXXXX")
cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT

# 被测程序：申请 12MiB 并真实触碰每个页面。
# 12MiB 超过 8MiB 判定线，但没碰到 24MiB 硬上限 → 不会被杀，只会被判 MLE。
cat >"$WORK/allocate.py" <<'PY'
buf = bytearray(12 * 1024 * 1024)
for i in range(0, len(buf), 4096):   # 每页写一个字节，确保真的占用了物理内存
    buf[i] = 1
print("touched 12MiB")
PY

if [[ $SKIP_HELPER == 0 ]]; then
    echo "  要执行的命令（参数顺序由 runner.py 的 helper_args 决定）："
    echo
    echo "    runner_helper  2 \\                              # CPU 秒数"
    echo "                   \$GROUP/cgroup.procs \\            # ← 关键：告诉 helper 往哪个组里放进程"
    echo "                   67108864 67108864 0 2000 \\       # 栈 / 输出上限 / nproc / wall 毫秒"
    echo "                   0 65534 65534 \\                  # 是否降权 / uid / gid"
    echo "                   \$WORK /dev/null out err \\        # 工作目录 / stdin / stdout / stderr"
    echo "                   python3 allocate.py"
    echo
    echo "  ↑ 注意第 2 个参数。helper 自己不负责“决定”进哪个组，"
    echo "    它只是收到一个路径。真正写 cgroup.procs 的动作，"
    echo "    是 helper fork 出的子进程在 exec 用户代码之前完成的。"
    echo

    REPORT=$("$HELPER" \
        "2" \
        "$GROUP/cgroup.procs" \
        "67108864" "67108864" "0" "2000" \
        "0" "65534" "65534" \
        "$WORK" "/dev/null" "$WORK/out" "$WORK/err" \
        python3 "$WORK/allocate.py")
    echo "  helper 打印的 JSON 报告（只有 CPU / wall / rss / 退出状态，没有内存）："
    echo "    $REPORT" | sed 's/^/  /'
    echo
    echo "  用户程序自己的输出（写到了 \$WORK/out，和上面的报告互不干扰）："
    echo "    $(cat "$WORK/out" 2>/dev/null | sed 's/^/  /')"
    echo
fi

echo "【步骤 16】读取内存成绩"
echo
echo "  先杀掉组里可能残留的进程，等到完全空了再读。"
echo "  为什么必须先杀？因为只要有进程还在跑，峰值就可能继续往上涨。"
echo
echo 1 >"$GROUP/cgroup.kill"
deadline=$((SECONDS + 5))
while grep -q populated\ 1 "$GROUP/cgroup.events" 2>/dev/null; do
    (( SECONDS >= deadline )) && { echo "  组里还有进程没退出，放弃。"; exit 1; }
    sleep 0.001
done
echo "    echo 1 > \$GROUP/cgroup.kill   → 组内进程已清零"
echo

PEAK_BYTES=$(cat "$GROUP/memory.peak")
echo "  现在组是空的，但统计还在："
echo "    memory.peak   = $PEAK_BYTES 字节  ($((PEAK_BYTES / 1024)) KiB)"
echo "    memory.events = $(tr '\n' ' ' <"$GROUP/memory.events")"
echo
echo "  ↑ 程序早就释放了那 12MiB 并正常退出了，但峰值仍被内核记着。"
echo "    这就是为什么“自己读 /proc 采样”不可靠：你很可能正好错过峰值。"
echo

echo "【步骤 17】判定"
echo
echo "  对照 runner.py 的 _set_verdict，顺序是："
echo "    ① 有 OOM 事件 → MLE    ② 峰值超过判定线 → MLE    ③ 其他情况"
echo
if grep -qE '^(oom|oom_kill) [1-9]' "$GROUP/memory.events"; then
    echo "    结果：MLE —— 因为 cgroup 记录了 OOM 事件"
elif (( PEAK_BYTES > JUDGE_KB * 1024 )); then
    echo "    结果：MLE —— 峰值 $((PEAK_BYTES / 1024))KiB 超过判定线 ${JUDGE_KB}KiB"
else
    echo "    结果：OK"
fi
echo
echo "  这里申请的是 12MiB，硬上限 24MiB 没被碰到，所以程序是“正常退出”的"
echo "  （exit=0、signal=0、没有任何 OOM）。但实测峰值 $((PEAK_BYTES / 1024))KiB 已经"
echo "  超过判定线 ${JUDGE_KB}KiB，所以仍然判 MLE。"
echo
echo "  峰值 $((PEAK_BYTES / 1024))KiB 比申请的 12MiB（=12288KiB）多一些，因为"
echo "  python3 解释器本身也要占内存，cgroup 把这些一起算进去了。"
echo
echo "  如果申请量改成 64MiB：会撞上 24MiB 硬上限，内核直接 OOM 杀掉，"
echo "  memory.events 出现 oom_kill，峰值停在硬上限附近。那时峰值已经不代表"
echo "  程序真实需求，所以要优先看 OOM 事件——这就是判定顺序把 ① 放在 ② 前面的原因。"
echo

echo "【步骤 18】清理"
echo
rmdir "$GROUP"
echo "    rmdir $GROUP"
echo
echo "  必须在“读完成绩”之后删：目录一删，memory.peak 这些接口文件就没了。"
echo
REMAIN=$(find "$SCOPE_DIR" -maxdepth 1 -name 'case-*' 2>/dev/null | wc -l)
echo "  剩余 case-* 目录：$REMAIN 个"
echo

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║  复盘                                                             ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo
echo "  整件事一共就这么几步："
echo
echo "    1. 普通用户不能直接用 cgroup —— 目录归 root，没写权限；"
echo "       而且你自己的会话目录里塞满了别的进程，不能动。"
echo
echo "    2. systemd-run --user --scope -p Delegate=yes"
echo "       = 请 systemd 开一块新地盘，并把所有权交给你。"
echo
echo "    3. 新地盘里一开始也不能立刻开 memory 控制器，"
echo "       因为“要下发资源的目录自己不能有进程”。"
echo "       先把自己搬进一个叶子子目录（manager），父目录空了才能开。"
echo
echo "    4. 开好 memory 之后，新建 case 组、写 memory.max，"
echo "       然后调用 runner_helper，把 cgroup.procs 路径作为参数交给它。"
echo
echo "    5. helper 的子进程在 exec 用户代码之前写 cgroup.procs，"
echo "       把自己放进 case 组，之后申请的每一页内存都被记账。"
echo
echo "    6. 杀掉整组、读 memory.peak / memory.events、判定、rmdir。"
echo
echo "  memory_cgroup.py 做的就是第 3~6 步，"
echo "  examples/delegated.py 做的就是第 2~3 步。"
echo
