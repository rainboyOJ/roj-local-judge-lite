#!/usr/bin/env bash
#
# 第 8 章配套实验：不经过 memory_cgroup.py，用 shell 手动做一遍同样的事。
#
# 这个脚本要回答一个问题：MemoryCgroup 到底做了什么？答案是——它只做了
# 三件普通的事情，没有任何魔法：
#   1. mkdir 一个目录，往目录里的特殊文件写配置（这些文件就是内核接口）；
#   2. 拼一个参数数组，调用 runner_helper（执行仍然由 helper 负责）；
#   3. 读目录里的统计文件，拿到峰值和 OOM 事件，然后删掉目录。
#
# 脚本按 memory_cgroup.py 的 __enter__ / stop / memory_result / __exit__
# 四个阶段分节，每节先用 shell 手工复刻，再打印对应的 Python 源码行。
#
# 用法（在仓库根目录执行）：
#   bash docs/tutorial/examples/manual_cgroup.sh          # 自动委派
#   ROJ_JUDGE_CGROUP_ROOT=/some/root bash .../manual_cgroup.sh   # 用已有目录
#
# 前一章：07-signals-and-cleanup.md   下一章：09-python-api.md

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. 准备：自动委派一个 cgroup v2 父目录
#
# Python 版把这一步交给调用者（README 里是 systemd-run + examples/delegated.py）。
# 这里为了让实验开箱即用，在脚本内部做同一件事：
#   没有 ROJ_JUDGE_CGROUP_ROOT 时，用 systemd-run 在专用 scope 里重跑自己。
#
# 为什么要"重跑自己"？因为需要管理的目录必须是"本进程独占、且被 systemd 授权
# 委派"的新 scope。普通登录 session 的 scope 里有别的进程，改它会影响整个桌面。
#
# 注意：systemd-run --scope 不会把我们自定义的环境变量带进新 scope（它只保留
# 一份干净的登录环境）。所以不能靠 ROJ_JUDGE_CGROUP_ROOT 传递结果，而要像
# delegated.py 那样，在 scope 内部从 /proc/self/cgroup 反推出自己的路径。
# ---------------------------------------------------------------------------
delegate_self() {
    local user_flag=()
    [[ $EUID -ne 0 ]] && user_flag=(--user)
    echo "未发现委派目录，改用 systemd-run 在专用 scope 中重新执行本脚本..."
    echo "  systemd-run ${user_flag[*]} --scope -p Delegate=yes"
    echo
    exec systemd-run "${user_flag[@]}" --quiet --scope -p Delegate=yes -- \
        bash "$0" "$@"
}

if [[ -n ${ROJ_JUDGE_CGROUP_ROOT:-} ]]; then
    # 调用者已经准备好目录（例如通过 examples/delegated.py 启动），直接使用。
    ROOT=$ROJ_JUDGE_CGROUP_ROOT
else
    # 没有现成目录，就在一个新 scope 里运行，并从 /proc/self/cgroup 推出路径。
    # 对应 delegated.py 开头的检查：必须是 .scope、可写。
    #
    # 这里不照搬 delegated.py 的“组里只有自己”断言。那条断言在 Python 里成立，
    # 因为整个检查在一个进程里完成；但在 shell 里，每次 $(…) 或管道都会 fork
    # 出临时子进程，它们的 cgroup 归属和本脚本相同，会让计数忽大忽小。
    # 只要确认路径是个可写的 .scope，就可以直接使用。
    scope=$(awk -F: '/^0::/{print $3}' /proc/self/cgroup)
    candidate="/sys/fs/cgroup$scope"
    if [[ $candidate == *.scope && -w $candidate ]]; then
        ROOT=$candidate
    else
        delegate_self "$@"
        echo "错误：委派失败，仍未获得可用的 cgroup 父目录。" >&2
        exit 1
    fi
fi

# 到这里已经有了一个可管理的父目录。接下来像 examples/delegated.py 那样做
# 最后的准备：把管理器进程放到 manager 叶子组，再为父目录启用 memory controller。
# 对 domain controller 的要求是"管理目录自身不能有普通进程"，所以必须先搬家
# 再开启——目录里没进程时才不受这条限制。
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
MANAGER="$ROOT/manager"

if [[ ! -d $MANAGER ]]; then
    mkdir "$MANAGER"
    # write "0" 是把"当前进程"迁进去，和 helper 让子进程入组时用的是同一个技巧。
    echo $$ >"$MANAGER/cgroup.procs"
    echo +memory >"$ROOT/cgroup.subtree_control"
fi

HELPER="$REPO/runner_helper"
[[ -x $HELPER ]] || { echo "错误：找不到 $HELPER，请先在仓库根目录运行 make" >&2; exit 1; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/manual-cgroup-XXXXXX")
cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT

# 实验参数。与 08 章 cgroup_demo.py 一致，方便对照：
#   题目阈值 8MiB + 保护余量 16MiB = memory.max 24MiB
JUDGE_KB=$((8 * 1024))          # Limits.memory_kb —— 判定线
SLACK_KB=$((16 * 1024))         # Limits.memory_slack_kb —— 保护余量
MAX_BYTES=$(((JUDGE_KB + SLACK_KB) * 1024))   # memory.max —— 保护上限
REQUEST_MIB=${REQUEST_MIB:-12}     # allocate 申请量；可用 REQUEST_MIB=4/64 观察另外两种结局

GROUP="$ROOT/case-$RANDOM$RANDOM"   # 对应 uuid.uuid4().hex，每次唯一

echo "═══════════════════════════════════════════════════════════════════"
echo " 手动复刻 MemoryCgroup：root=$ROOT"
echo "═══════════════════════════════════════════════════════════════════"
echo

# ---------------------------------------------------------------------------
# 1. __enter__ —— 建组、写保护配置、检查接口
#
# memory_cgroup.py:24-40
#   enabled = (self.root / "cgroup.subtree_control").read_text().split()
#   if "memory" not in enabled: raise OSError(...)
#   self.path.mkdir()
#   (self.path / "memory.max").write_text(...)
#   (self.path / "memory.swap.max").write_text("0")
#   (self.path / "memory.oom.group").write_text("1")
#   for name in ("memory.peak", "memory.events", "cgroup.kill"): ...
# ---------------------------------------------------------------------------
echo "【1】__enter__：创建独立子组并写入保护配置"
echo

# 对应 __enter__ 里对 cgroup.subtree_control 的检查。Python 是 split() 后
# 判断成员；shell 用 word matching。父目录未启用 memory 时子组无法计量内存。
enabled=$(<"$ROOT/cgroup.subtree_control")
if [[ " $enabled " != *" memory "* ]]; then
    echo "错误：$ROOT 尚未为子组启用 memory controller" >&2
    exit 1
fi
echo "  cat $ROOT/cgroup.subtree_control"
echo "    → $enabled        （含 memory，可以为子组启用内存计量）"
echo

# 对应 self.path.mkdir()。mkdir 的原子性同时充当并发判题的互斥。
mkdir "$GROUP"
echo "  mkdir $GROUP"
echo "    → 这是一个真实的 cgroup 目录，不是普通文件夹"
echo

# 对应 __enter__ 里的三次 write_text。这些是内核接口文件，写入即生效，
# 不会在磁盘上留下"配置文件"。
echo "$MAX_BYTES" >"$GROUP/memory.max"
echo 0            >"$GROUP/memory.swap.max"
echo 1            >"$GROUP/memory.oom.group"
echo "  echo $MAX_BYTES > \$GROUP/memory.max       # 保护上限 = ($JUDGE_KB + $SLACK_KB)KiB"
echo "  echo 0            > \$GROUP/memory.swap.max  # 禁止换出，避免超限内存逃进 swap"
echo "  echo 1            > \$GROUP/memory.oom.group # 超限时整组处理，不只是杀一个进程"
echo "    → 回读 memory.max: $(<"$GROUP/memory.max") 字节（内核按页对齐后可能略有变化）"
echo

# 对应 __enter__ 里对三个必需接口的提前检查：缺任何一个就报错，
# 不允许悄悄退回 RSS 采样或无内存限制的运行。
for name in memory.peak memory.events cgroup.kill; do
    [[ -e $GROUP/$name ]] || { echo "错误：内核缺少 cgroup 接口 $name" >&2; exit 1; }
done
echo "  检查 memory.peak / memory.events / cgroup.kill 均存在"
echo "    → 接口齐全，后续可以精确判定而不是靠采样"
echo

# ---------------------------------------------------------------------------
# 2. 拼参数并调用 runner_helper —— 执行不属于 MemoryCgroup 的职责
#
# runner.py:261-264
#   command = [str(helper), *limits.helper_args(group.procs_path ...),
#              str(int(drop_privileges)), str(run_uid), str(run_gid),
#              str(work_dir), str(input_path), str(output_path), str(stderr_path), *argv]
#
# Limits.helper_args 的参数顺序（runner.py:81-98）：
#   argv[1] = CPU 秒（向上取整）
#   argv[2] = cgroup.procs 路径，空字符串表示显式禁用 cgroup
#   argv[3] = 栈字节
#   argv[4] = 输出文件上限字节
#   argv[5] = nproc
#   argv[6] = wall 毫秒
#   argv[7..9] = drop_privileges / uid / gid
#   argv[10..13] = cwd / stdin / stdout / stderr
#   argv[14...] = 用户程序及其参数
#
# 关键：helper 只是"执行器"。真正把进程放进 cgroup 的动作，是 helper 在
# fork 之后、exec 用户代码之前，由子进程自己写 cgroup.procs 完成的。
# 本脚本把这个路径作为 argv[2] 传进去，就是在交付"往哪个组里放"。
# ---------------------------------------------------------------------------
echo "【2】调用 runner_helper：把 group.procs 路径交给执行器"
echo

INPUT="$WORK/input";  printf 'hello\n' >"$INPUT"
OUTPUT="$WORK/output"
STDERR="$WORK/stderr"
ALLOCATE="$REPO/docs/tutorial/examples/.build/allocate"
[[ -x $ALLOCATE ]] || { echo "错误：找不到 $ALLOCATE，请运行 make -C docs/tutorial/examples" >&2; exit 1; }

CPU_SECONDS=2       # --time 1000ms + cpu_slack 200ms 向上取整
STACK_BYTES=$((64 * 1024 * 1024))
OUTPUT_LIMIT_BYTES=$((64 * 1024 * 1024))
NPROC=0
WALL_MS=2000
DROP_PRIVILEGES=0   # 教学实验不降权：root 才能在 /sys/fs/cgroup 下操作
RUN_UID=65534
RUN_GID=65534

echo "  \$HELPER $CPU_SECONDS \\"
echo "          $GROUP/cgroup.procs \\"
echo "          $STACK_BYTES $OUTPUT_LIMIT_BYTES $NPROC $WALL_MS \\"
echo "          $DROP_PRIVILEGES $RUN_UID $RUN_GID \\"
echo "          $WORK $INPUT $OUTPUT $STDERR \\"
echo "          $ALLOCATE $REQUEST_MIB"
echo

# helper 的 stdout 是 JSON 资源报告，和用户程序的 stdout（这里写进了 $OUTPUT
# 文件）是两条完全独立的通道，所以用户打印什么都破坏不了报告。
REPORT=$("$HELPER" \
    "$CPU_SECONDS" \
    "$GROUP/cgroup.procs" \
    "$STACK_BYTES" "$OUTPUT_LIMIT_BYTES" "$NPROC" "$WALL_MS" \
    "$DROP_PRIVILEGES" "$RUN_UID" "$RUN_GID" \
    "$WORK" "$INPUT" "$OUTPUT" "$STDERR" \
    "$ALLOCATE" "$REQUEST_MIB")
echo "  helper 的 JSON 报告（只含 CPU / wall / rss / 退出状态）："
echo "    $REPORT"
echo "  用户程序 stdout（写进了 \$OUTPUT，不影响上面的报告）："
echo "    $(tr '\n' ' ' <"$OUTPUT")"
echo

# ---------------------------------------------------------------------------
# 3. stop() —— 用 cgroup.kill 清空整组，等待 populated 归零
#
# memory_cgroup.py:47-54
#   (self.path / "cgroup.kill").write_text("1")
#   deadline = time.monotonic() + 5
#   while read_counters(self.path / "cgroup.events")["populated"]:
#       if time.monotonic() >= deadline: raise OSError(...)
#       time.sleep(0.001)
#
# 为什么不用 kill(1) / killpg？因为提交可以自己调 setsid 逃离进程组，
# 那种后代 kill(-pgid) 打不到，但它的 cgroup 成员关系逃不掉。
# ---------------------------------------------------------------------------
echo "【3】stop()：清空整组并确认 populated 归零"
echo

echo 1 >"$GROUP/cgroup.kill"
echo "  echo 1 > \$GROUP/cgroup.kill"
deadline=$((SECONDS + 5))
while [[ $(<"$GROUP/cgroup.events") == *"populated 1"* ]]; do
    if (( SECONDS >= deadline )); then
        echo "错误：cgroup 中的进程未退出，保留目录便于排查: $GROUP" >&2
        trap - EXIT
        exit 1
    fi
    sleep 0.001
done
echo "  populated 已归零：$(tr '\n' ' ' <"$GROUP/cgroup.events")"
echo "    → 组内确实没有进程了，可以安全读取最终统计"
echo

# ---------------------------------------------------------------------------
# 4. memory_result() —— 读峰值与 OOM 事件
#
# memory_cgroup.py:56-62
#   peak = int((self.path / "memory.peak").read_text())
#   events = read_counters(self.path / "memory.events")
#   return {"memory_peak_bytes": peak, "memory_kb": (peak + 1023) // 1024,
#           "oom_events": events["oom"], "oom_kills": events["oom_kill"]}
#
# 必须"先 stop 再读"：还有进程在跑时，峰值可能继续上涨。
# 这也解释了为什么 run_case 在 with 体内显式调一次 stop()，__exit__ 里再调一次。
# ---------------------------------------------------------------------------
echo "【4】memory_result()：读取最终峰值与 OOM 证据"
echo

PEAK_BYTES=$(<"$GROUP/memory.peak")
# read_counters 把 "key value" 每行解析成字典；shell 用 while read 达到同样效果。
declare -A EVENTS=()
while read -r key value; do EVENTS[$key]=$value; done <"$GROUP/memory.events"

echo "  cat \$GROUP/memory.peak    → $PEAK_BYTES 字节"
echo "  cat \$GROUP/memory.events  → oom=${EVENTS[oom]:-0} oom_kill=${EVENTS[oom_kill]:-0}"
echo

# 对应 runner.py:176-184 的 _set_verdict。判定用的全是普通整数，
# 与 cgroup 目录再无关系——所以下一步可以放心把目录删掉。
PEAK_KB=$(( (PEAK_BYTES + 1023) / 1024 ))
if (( ${EVENTS[oom]:-0} || ${EVENTS[oom_kill]:-0} )); then
    VERDICT=MLE; REASON="cgroup 记录了内存 OOM 事件"
elif (( PEAK_BYTES > JUDGE_KB * 1024 )); then
    VERDICT=MLE; REASON="cgroup 内存峰值超过题目限制"
else
    VERDICT=OK;  REASON=""
fi

echo "【5】_set_verdict：用普通整数做判定（此时目录还没删）"
echo
echo "  判定阈值：$JUDGE_KB KiB     实测峰值：$PEAK_KB KiB     申请量：${REQUEST_MIB}MiB"
echo "  verdict = $VERDICT${REASON:+   （$REASON）}"
echo
echo "  注意：申请量 12MiB 超过了 8MiB 判定线，但没碰到 24MiB 保护上限，"
echo "  所以进程正常退出（exit=0 signal=0）却仍判 MLE。这就是“先 stop"
echo "  再读峰值”的意义：程序已经释放并退出，峰值仍被内核保留。"
echo

# ---------------------------------------------------------------------------
# 5. __exit__ —— 删除目录
#
# memory_cgroup.py:63-66
#   def __exit__(self, exc_type, exc, traceback):
#       self.stop()
#       self.path.rmdir()
#
# rmdir 只能删空目录：cgroup v2 里"空"指没有子组，此时 populated 已为 0。
# 顺序必须是"先读完统计，再 rmdir"——删掉目录后这些接口文件就没了。
#
# 真实项目里 __exit__ 承担的是"异常路径的兜底"：helper 崩了、Ctrl+C、
# _invoke_helper 抛 RuntimeError，都会直接跳到 __exit__ 执行清理。
# 本脚本用 trap 表达同样的意图：无论怎么退出，都不留下垃圾 cgroup。
# ---------------------------------------------------------------------------
echo "【6】__exit__：删除目录"
echo

rmdir "$GROUP"
echo "  rmdir $GROUP"
echo "    → 已删除（必须在读完成绩之后）"
echo

# 验证没有遗留。对应 test_runner.py 的 assert_no_remaining_cgroups。
leftover=$(find "$ROOT" -maxdepth 1 -name 'case-*' -printf '%f ' 2>/dev/null || true)
echo "  剩余 case-* 目录：${leftover:-（无）}"
echo
echo "═══════════════════════════════════════════════════════════════════"
echo " 结束。三件事都只是普通文件操作："
echo "   ① mkdir + 写 memory.max/swap.max/oom.group   = 限制与计量内存"
echo "   ② 把 cgroup.procs 路径当 argv[2] 传给 helper = 交给执行器入组"
echo "   ③ 读 memory.peak/memory.events 后 rmdir      = 取成绩并清理"
echo "═══════════════════════════════════════════════════════════════════"
