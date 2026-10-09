#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linux 单程序资源执行器：cgroup 管内存，wait4 统计 CPU，wall 看门狗兜底。

题目阈值与执行保护上限分开：CPU 默认多留 200ms（再向上取整到秒），
内存默认多留 16MiB。最终仍按原始题目阈值判断，不把保护余量当作放宽评分。

调用者提供已委派且启用 memory controller 的 cgroup v2 父目录。
用户进程在 exec 前进入独立子组，Python 和 helper 留在组外。
内存统计包含组内后代、文件缓存和部分内核内存，不再用 RSS 判断 MLE。

两种模式共用同一个独立 C helper：Python 管配置、cgroup 生命周期和判定，
helper 管 fork/exec、限额、wait4 和进程组清理。use_cgroup=False 只关闭
cgroup 能力，不切换执行实现；这时没有内存计量，也不能判定 MLE。

不编译、不比对答案；OK 只表示正常执行。依赖 Linux、Python 3.8+、预构建 helper。

执行顺序
--------
一次 run_case() 调用按下面的顺序发生。**代码按职责分段、不按步号排列**，所以文件里
step 编号会跳。每一步后面是讲解它的教程章节。

    step 01  限制口径       Limits / memory_max_bytes      第 06 章「第三步」
    step 02  校验配置       Limits.validate                第 09 章「第一步」
    step 03  固定路径       _check_stream_paths            第 04 章
    step 04  工作目录身份   run_case                       第 02、07 章
    step 05  准备 cgroup    MemoryCgroup / nullcontext     第 08 章
    step 06  启动 helper    helper_args / _invoke_helper   第 09 章「第四步」
    step 07  收尾 cgroup    group.stop / memory_result     第 08 章
    step 08  判定           _set_verdict / Verdict         第 09 章「第五步」
    step 09  报告结果       CaseResult / to_dict           第 05 章「第四步」
    step 10  CLI 入口       main                           第 09 章
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import os
import signal
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Dict, Optional

from memory_cgroup import MemoryCgroup

__all__ = ["Limits", "Verdict", "CaseResult", "run_case"]


# ── step 08 · 按优先级判定（第 09 章「第五步」）──────────────────────────────
class Verdict(str, enum.Enum):
    OK = "OK"
    TLE = "TLE"
    MLE = "MLE"
    RE = "RE"
    SYSTEM_ERROR = "SYSTEM_ERROR"


# ── step 01 · 限制口径：判定线与保护上限（第 06 章「第三步」）────────────────
@dataclasses.dataclass
class Limits:
    """单次执行的限制；时间单位 ms，memory_kb 单位 KiB，其余内存单位 MiB。"""

    time_ms: int = 1000
    memory_kb: int = 128 * 1024
    wall_time_ms: int = 0
    wall_slack_ms: int = 500
    cpu_slack_ms: int = 200
    memory_slack_kb: int = 16 * 1024
    stack_mb: int = 64
    output_limit_mb: int = 64
    nproc: int = 0

    # time_ms 和 memory_kb 是最终判定阈值，0 表示不设对应限制。
    # cpu_slack_ms / memory_slack_kb 只放宽保护上限，最终判定不加这两个值。
    # wall 是独立的防卡死上限，包含 I/O、调度和等待时间。
    # nproc 限制同一真实 UID 的总进程/线程数，默认关闭。

    # step 01：保护上限 = 判定线 + 余量，故意比判定线宽。
    def memory_max_bytes(self) -> int:
        if self.memory_kb == 0:
            return 0
        return (self.memory_kb + self.memory_slack_kb) * 1024

    # step 01：wall 是独立的防卡死上限，包含 I/O 与等待时间。
    def resolved_wall_ms(self) -> int:
        if self.wall_time_ms > 0:
            return self.wall_time_ms
        return self.time_ms + self.wall_slack_ms if self.time_ms > 0 else 0

    # ── step 02 · 校验配置 ────────────────────────────────────────────────────
    def validate(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field.name} 必须是非负整数")

    # ── step 06 · 转成 helper 的参数数组 ──────────────────────────────────────
    def helper_args(self, cgroup_procs: Optional[Path] = None) -> list[str]:
        """转换为 helper 的内部参数；顺序对应 C 中的 parse_options。

        CPU 向上取整成秒，栈与输出限额换成字节。cgroup 参数为空字符串时
        表示显式关闭入组；Popen 直接传递参数数组，因此空参数不会被吞掉。
        """
        values = [
            (self.time_ms + self.cpu_slack_ms + 999) // 1000 if self.time_ms else 0,
            self.stack_mb * 1024 * 1024,
            self.output_limit_mb * 1024 * 1024,
            self.nproc,
            self.resolved_wall_ms(),
        ]
        if any(value >= 2**63 - 1 for value in values) or self.memory_max_bytes() >= 2**63 - 1:
            raise ValueError("资源限制超出 helper 支持的整数范围")
        args = [str(value) for value in values]
        args.insert(1, str(cgroup_procs) if cgroup_procs is not None else "")
        return args


# ── step 09 · 报告结果（第 05 章「第四步」）──────────────────────────────────
@dataclasses.dataclass
class CaseResult:
    verdict: Verdict = Verdict.SYSTEM_ERROR
    cpu_time_us: int = 0
    cpu_time_ms: int = 0
    real_time_ms: int = 0
    memory_kb: int = 0
    """cgroup memory.peak 的 KiB 展示值；精确比较使用下面的字节数。"""

    memory_peak_bytes: int = 0
    rss_kb: int = 0
    """wait4 的 ru_maxrss，仅作辅助诊断，不用于内存判定。"""

    oom_events: int = 0
    oom_kills: int = 0

    timed_out: bool = False
    signal: int = 0
    exit_code: int = 0
    message: str = ""
    output_path: str = ""
    stderr_path: str = ""

    # step 09：给 CLI 的 JSON 输出。
    def to_dict(self) -> Dict[str, object]:
        result = dataclasses.asdict(self)
        result["verdict"] = self.verdict.value
        return result


# ── step 06 · 给被测进程准备环境（第 09 章）─────────────────────────────────
def _child_env(work_dir: Path, inherit: bool) -> Dict[str, str]:
    env = dict(os.environ) if inherit else {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    env.update({
        "HOME": str(work_dir), "TMPDIR": str(work_dir),
        "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


# ── step 06 · 启动 helper 并收回资源报告（第 09 章「第四步」）────────────────
def _invoke_helper(command: list[str], env: Dict[str, str]) -> CaseResult:
    """启动独立监控进程，并把它的资源报告转换成 Python 结果。

    这里有两条不同的数据通道：helper 的 stdout 是 JSON 报告，用户程序
    的 stdout 是输出文件。分开后，用户打印任意文本都不会破坏报告。
    Python 只等待 helper；用户进程的 wait4 必须由其直接父进程 helper 做。
    """
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate()
    except BaseException:
        # Ctrl+C / 调用方异常时先让 helper 处理 SIGTERM：它会杀掉提交进程组并
        # wait4 回收。不能只杀 helper，否则被测进程的后代可能继续留在后台。
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    if process.returncode != 0:
        raise RuntimeError(f"helper 失败（exit {process.returncode}）: {stderr.strip()}")
    # helper 与 Python 属于同一个程序，JSON 字段就是 CaseResult 的资源字段。
    # 直接构造结果，避免另外维护一套字段清单再用 setattr 逐个复制。
    try:
        return CaseResult(**json.loads(stdout))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"helper 返回了无效的资源报告: {exc}") from exc


# ── step 08 · 按优先级判定（第 09 章「第五步」）──────────────────────────────
def _set_verdict(result: CaseResult, limits: Limits) -> None:
    """仅根据可验证的资源数据和退出状态分类，不读取或猜测 stderr 内容。"""
    if result.oom_events or result.oom_kills:
        result.verdict, result.message = Verdict.MLE, "cgroup 记录了内存 OOM 事件"
    elif result.timed_out:
        result.verdict, result.message = Verdict.TLE, "wall-clock 超时，已终止提交进程组"
    elif limits.memory_kb and result.memory_peak_bytes > limits.memory_kb * 1024:
        result.verdict, result.message = Verdict.MLE, "cgroup 内存峰值超过题目限制"
    elif (limits.time_ms and result.cpu_time_us > limits.time_ms * 1000) or result.signal == signal.SIGXCPU:
        result.verdict, result.message = Verdict.TLE, "CPU 时间超限"
    elif result.signal:
        result.verdict, result.message = Verdict.RE, f"被信号 {result.signal} 终止"
    elif result.exit_code:
        result.verdict, result.message = Verdict.RE, f"非零退出码 {result.exit_code}"
    else:
        result.verdict = Verdict.OK
    # 没有 OOM 证据时，SIGKILL/非零退出码仍为 RE；不能根据 stderr 猜 MLE。
    # CPU 判断使用原始微秒数，cpu_time_ms 的四舍五入仅供展示。


# ── step 03 · 固定文件路径（第 04 章）────────────────────────────────────────
def _check_stream_paths(*paths: Path) -> None:
    """防止 O_TRUNC 截断输入或让两个输出互相覆盖；也检查符号链接和硬链接。"""
    for i, path in enumerate(paths):
        for other in paths[:i]:
            same_name = path.resolve() == other.resolve()
            same_file = path.exists() and other.exists() and os.path.samefile(path, other)
            if same_name or same_file:
                raise ValueError("stdin、stdout、stderr 必须使用不同的文件")


# ── step 02 → step 09 · 一次完整的运行（第 09 章「第三步」把这里分成五段）──────
def run_case(
    argv: list[str],
    input_path: Path,
    output_path: Path,
    limits: Optional[Limits] = None,
    *,
    stderr_path: Optional[Path] = None,
    cwd: Optional[Path] = None,
    run_uid: int = 65534,
    run_gid: int = 65534,
    drop_privileges: Optional[bool] = None,
    inherit_env: bool = False,
    helper_path: Optional[Path] = None,
    cgroup_root: Optional[Path] = None,
    use_cgroup: bool = True,
) -> CaseResult:
    """执行一个已存在的程序，返回资源与退出状态。

    输入、输出、stderr 路径相对于调用者当前目录；命令中的 ./路径 相对于 cwd。
    cwd 默认是调用者当前目录。程序参数原样传递，不经过 shell。
    root 默认降权至 nobody；普通用户默认保留身份。降权后程序必须能访问 cwd
    和可执行文件，本函数不会修改目录权限。标准流在降权前打开。
    默认要求 cgroup 可用，失败返回 SYSTEM_ERROR，不自动降低保护等级。
    use_cgroup=False 时忽略 cgroup_root，不访问 cgroup；仍设置 CPU、wall、
    栈、输出、进程数限制并执行降权。内存字段保留 0 表示未测量，rss_kb
    只供诊断；此模式只能清理同一进程组，无法覆盖主动 setsid 的后代。
    配置错误抛 ValueError，启动/重定向/helper 故障返回 SYSTEM_ERROR。
    """
    # ── step 02 · 校验配置（第 09 章「第一步」）───────────────────────────────
    if not argv:
        raise ValueError("argv 不能为空")
    limits = limits or Limits()
    limits.validate()
    for name, value in (("run_uid", run_uid), ("run_gid", run_gid)):
        if type(value) is not int or not 0 <= value < 2**32 - 1:
            raise ValueError(f"{name} 必须是有效的非负 UID/GID")

    # ── step 03 · 固定文件路径（第 04 章）────────────────────────────────────
    # 先固定文件路径：helper 改变 cwd 之后，重定向仍要指向调用者指定的文件。
    input_path = Path(input_path).absolute()
    output_path = Path(output_path).absolute()
    stderr_path = Path(stderr_path or str(output_path) + ".err").absolute()
    _check_stream_paths(input_path, output_path, stderr_path)
    # ── step 04 · 决定工作目录与执行身份（第 02、07 章）───────────────────────
    work_dir = Path(cwd or Path.cwd()).absolute()
    helper = Path(helper_path or Path(__file__).with_name("executor")).absolute()
    if drop_privileges is None:
        drop_privileges = os.geteuid() == 0

    # ── step 05 · 准备 cgroup 生命周期（第 08 章）────────────────────────────
    root = Path(cgroup_root or os.environ.get("ROJ_JUDGE_CGROUP_ROOT", "/sys/fs/cgroup/roj-judge")).absolute()
    try:
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise FileNotFoundError(f"找不到可执行的 helper: {helper}；请先在其源码目录运行 make")
        # nullcontext 是“不做额外操作的 with”，进入后得到 None。
        # 模式只决定是否包一层 cgroup 生命周期，下面的执行代码始终只有一份。
        context = MemoryCgroup(root, limits.memory_max_bytes()) if use_cgroup else nullcontext()
        with context as group:
            # ── step 06 · 拼参数、备环境、启动 helper（第 09 章「第四步」）────────
            command = [
                str(helper), *limits.helper_args(group.procs_path if group is not None else None),
                str(int(drop_privileges)), str(run_uid), str(run_gid),
                str(work_dir), str(input_path), str(output_path), str(stderr_path), *argv,
            ]
            result = _invoke_helper(command, _child_env(work_dir, inherit_env))
            if group is not None:
                # ── step 07 · 收尾 cgroup 并补内存统计（第 08 章）────────────────
                # 直接子进程退出不代表所有后代都退出。先停止整个组，再读取最终
                # 峰值和 OOM 事件；with 的退出清理也覆盖启动失败和 Ctrl+C。
                group.stop()
                result = dataclasses.replace(result, **group.memory_result())
        # ── step 08 · 按优先级判定（第 09 章「第五步」）──────────────────────
        # 两种模式都按同一规则判定。没有 cgroup 就没有内存超限证据，
        # 不拿 rss_kb 补位，也不根据用户 stderr 中的 MemoryError 猜 MLE。
        _set_verdict(result, limits)
    except (OSError, RuntimeError) as exc:
        result = CaseResult(message=str(exc))

    # ── step 09 · 报告结果（第 05 章「第四步」）──────────────────────────────
    result.output_path = str(output_path)
    result.stderr_path = str(stderr_path)
    return result


# ── step 10 · CLI 入口（第 09 章）────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="单程序资源限制与统计，不编译、不比较答案")
    parser.add_argument("--input", type=Path, default=Path("/dev/null"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, help="默认是 OUTPUT.err")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--time", type=int, default=1000, help="CPU 限制 ms；0 不限制")
    parser.add_argument("--memory", type=int, default=128, help="cgroup 内存判定阈值 MiB；0 不限制")
    parser.add_argument("--wall-time", type=int, default=0, help="wall 限制 ms；0 自动计算")
    parser.add_argument("--wall-slack-ms", type=int, default=500)
    parser.add_argument("--cpu-slack-ms", type=int, default=200, help="CPU 保护余量 ms")
    parser.add_argument("--memory-slack", type=int, default=16, help="内存保护余量 MiB")
    parser.add_argument("--cgroup-root", type=Path, help="已委派且为子组启用 memory 的父目录")
    parser.add_argument("--no-cgroup", action="store_true",
                        help="显式关闭 cgroup：不限制或计量内存，无法判定 MLE")
    parser.add_argument("--stack", type=int, default=64, help="栈限制 MiB；0 不设置")
    parser.add_argument("--output-limit", type=int, default=64, help="单个输出文件限制 MiB；0 不设置")
    parser.add_argument("--nproc", type=int, default=0, help="同一 UID 的进程/线程数限制；0 不设置")
    parser.add_argument("--uid", type=int, default=65534)
    parser.add_argument("--gid", type=int, default=65534)
    parser.add_argument("--no-drop-privileges", action="store_true")
    parser.add_argument("--inherit-env", action="store_true")
    parser.add_argument("--helper", type=Path, help="预先构建的 executor 路径")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 后接可执行文件及其参数")
    args = parser.parse_args(argv)
    # step 10 · CLI 把参数翻译成一次 run_case 调用。
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("请在 -- 后指定可执行文件及其参数")
    try:
        result = run_case(
            command, args.input, args.output,
            Limits(time_ms=args.time, memory_kb=args.memory * 1024,
                   wall_time_ms=args.wall_time, wall_slack_ms=args.wall_slack_ms,
                   cpu_slack_ms=args.cpu_slack_ms, memory_slack_kb=args.memory_slack * 1024,
                   stack_mb=args.stack,
                   output_limit_mb=args.output_limit, nproc=args.nproc),
            stderr_path=args.stderr, cwd=args.cwd, run_uid=args.uid, run_gid=args.gid,
            drop_privileges=False if args.no_drop_privileges else None,
            inherit_env=args.inherit_env, helper_path=args.helper, cgroup_root=args.cgroup_root,
            use_cgroup=not args.no_cgroup,
        )
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if result.verdict == Verdict.OK:
        return 0
    if result.verdict == Verdict.SYSTEM_ERROR:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
