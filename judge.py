#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地评测工具：编译提交、跑 testData/<pid>/data 的测试点、比对答案并汇总结果。

    python3 judge.py --pid 1000 solution.cpp
    python3 judge.py --pid 1000 solution.py --testdata ../testData

测试数据默认自动查找：先当前目录及其上级（你自己项目里的 testData/ 优先），
再找包内自带的示例数据 testData/；也可以用 `--testdata` 直接指定。

它把三件事串起来，让用户不用起 judge_server 就能自己验一份代码：

1. 编译：C++ 用和 judge_server 相同的 `g++ -std=c++17 -O2 -DONLINE_JUDGE`；
   Python 用 `py_compile` 做语法检查，把解释型语言统一映射到“编译阶段”。
2. 执行：调用 C executor，用 wait4 统计 CPU、设置资源限额；
   cgroup v2 可用时额外限制与计量内存，不可用时明确提示 MLE 无法判定。
3. 比对：优先用 `/judge/checker/fcmp2`，否则按行比较，忽略行尾空白和末尾空行
   —— 与 judge_server 的 fallback 路径同一套规则。

限制计算与判定规则（classify_execution）也在本文件；它不替代服务端判题，
详细差异见本目录 README.md 的“与 judge_server 的差异”一节。

执行顺序
--------
对应 docs/tutorial/10-judge-pipeline.md 的七步。**代码按职责分段，不按步号排列**，
下表是「文章第 N 步 ↔ 本文件位置」的对照：

    step 01  运行 CLI，把一份提交交给本地评测    main() 入口
    step 02  解析参数、定位测试数据、读题目限制   resolve_testdata / load_cases / load_problem_meta
    step 03  编译一次，之后所有测试点复用         compile_submission / detect_language
    step 04  决定执行模式：隔离 / 自动委派 / 降级  check_cgroup_root / try_auto_delegate / run_delegated
             委派细节：prepare_delegated_scope / run_in_scope
    step 05  逐测试点执行，只对 OK 的运行比答案    judge_case / judge_submission
    step 06  比对规则：内置按行比较 / 外部 checker normalize_lines / compare_output / first_difference
    step 07  汇总、清理、返回退出码               main() 尾部与 finally

注意 step 03 与 step 04 的**执行顺序和文章编号相反**：模式决策发生在编译之前，
因为它可能直接重启整个 CLI。main() 里对应位置有说明。
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Dict, Optional

# 同目录导入：允许直接 `python3 judge.py` 而不需要安装成包。
sys.path.insert(0, str(Path(__file__).resolve().parent))

# judge 是唯一的 Python 入口：环境准备、编译、测试点事务、判定与汇总都在这里。
# 底层的进程执行由独立的 C executor 完成，本文件不实现第二套执行器。
import case_io
from memory_cgroup import MemoryCgroup

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup/roj-judge")
CHECKER_PATH = Path("/judge/checker/fcmp2")
COMPILE_TIMEOUT_S = 120

LANG_BY_SUFFIX = {
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".py": "python",
}


# --------------------------------------------------------------------------
# step 02 · 测试点发现与题目限制（第 10 章「第二步」）
# --------------------------------------------------------------------------


def _natural_key(name: str) -> list[tuple[int, object]]:
    """让 problem2 排在 problem10 前面；同时保证不同名字之间始终可比较。"""
    parts = re.split(r"(\d+)", name)
    return [(0, int(part)) if part.isdigit() else (1, part) for part in parts]


def load_cases(data_dir: Path) -> list[tuple[str, Path, Path]]:
    """扫描 <pid>/data，按 judge_server 的规则配对 .in/.out。"""
    cases: list[tuple[str, Path, Path]] = []
    for in_path in data_dir.iterdir():
        if not in_path.is_file() or in_path.suffix != ".in":
            continue
        out_path = in_path.with_suffix(".out")
        if out_path.exists():
            cases.append((in_path.stem, in_path, out_path))
    return sorted(cases, key=lambda case: _natural_key(case[0]))


def resolve_testdata(explicit: Optional[Path]) -> tuple[Optional[Path], list[Path]]:
    """确定测试数据根目录，返回 (目录, 尝试过的路径)。

    顺序是「用户自己的优先，包内自带的兜底」：先当前目录及其上级，用户在自己项目里
    运行时不会被动到包里的示例数据；再找包内 testData/，所以装到
    ~/.local/share/roj-local-judge-lite 后在任意目录都能直接评测自带的示例题；
    最后保留包上级目录，兼容包位于仓库子目录的老布局。
    """
    cwd = Path.cwd()
    candidates = [
        explicit,
        cwd / "testData",
        cwd.parent / "testData",
        PACKAGE_DIR / "testData",
        PROJECT_ROOT / "testData",
    ]
    tried: list[Path] = []
    for candidate in candidates:
        if candidate is None:
            continue
        path = Path(candidate).resolve()
        if path in tried:
            continue
        tried.append(path)
        if path.is_dir():
            return path, tried
    return None, tried


def report_missing_testdata(tried: list[Path]) -> int:
    print("找不到测试数据目录，已尝试：", file=sys.stderr)
    for path in tried:
        print(f"  - {path}", file=sys.stderr)
    print("请用 --testdata 指定，或把题目数据放在当前目录的 testData/ 下。", file=sys.stderr)
    return 2


def _read_problem_config(problem_dir: Path) -> dict:
    """读题目 config.json 的 JSON 对象；缺失或损坏返回空 dict。"""
    config_path = problem_dir / "config.json"
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_problem_config(problem_dir: Path) -> tuple[str, int, int, case_io.IOConfig]:
    """读题目配置：标题、CPU ms、内存 MiB、IO 模式。

    缺少配置文件或字段时用默认值；IO 配置不合法抛 case_io.IOConfigError，
    由 CLI 在编译前报错退出。
    """
    data = _read_problem_config(problem_dir)
    title, time_ms, memory_mb = "", 1000, 128
    if isinstance(data.get("title"), str):
        title = data["title"]
    for key, default in (("time", time_ms), ("memory", memory_mb)):
        value = data.get(key)
        if isinstance(value, int) and value > 0:
            if key == "time":
                time_ms = value
            else:
                memory_mb = value
    io_config = case_io.parse_io_config(data.get("io"))
    return title, time_ms, memory_mb, io_config


def load_problem_meta(problem_dir: Path) -> tuple[str, int, int]:
    """兼容包装：只要标题与限制。IO 配置不合法时也向上抛。"""
    title, time_ms, memory_mb, _ = load_problem_config(problem_dir)
    return title, time_ms, memory_mb


# --------------------------------------------------------------------------
# step 03 · 编译（第 10 章「第三步」）
# --------------------------------------------------------------------------


def compile_submission(lang: str, source: Path, work_dir: Path) -> tuple[Optional[list[str]], str]:
    """返回 (运行命令, 编译输出)。命令为 None 表示编译失败（CE）。"""
    if lang == "cpp":
        executable = work_dir / "solution"
        # 与 judge_server 的 RunnerCompileSupport.cpp 保持同一组参数。
        command = ["g++", "-std=c++17", "-O2", "-DONLINE_JUDGE",
                   "-o", str(executable), str(source)]
        try:
            proc = subprocess.run(command, capture_output=True, text=True,
                                  timeout=COMPILE_TIMEOUT_S)
        except FileNotFoundError:
            return None, "找不到 g++，请先安装 C++ 编译器"
        except subprocess.TimeoutExpired:
            return None, f"编译超过 {COMPILE_TIMEOUT_S}s 未结束，已终止"
        if proc.returncode != 0 or not executable.is_file():
            return None, (proc.stderr or proc.stdout).strip()
        return [str(executable)], ""

    if lang == "python":
        try:
            proc = subprocess.run([sys.executable, "-m", "py_compile", str(source)],
                                  capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S)
        except FileNotFoundError:
            return None, f"找不到 Python 解释器: {sys.executable}"
        except subprocess.TimeoutExpired:
            return None, f"语法检查超过 {COMPILE_TIMEOUT_S}s 未结束，已终止"
        if proc.returncode != 0:
            return None, (proc.stderr or proc.stdout).strip()
        return ["python3", str(source)], ""

    return None, f"不支持的语言: {lang}"


def detect_language(source: Path, requested: Optional[str]) -> tuple[Optional[str], str]:
    if requested and requested != "auto":
        if requested not in ("cpp", "python"):
            return None, f"未知语言 {requested}，只支持 cpp 和 python"
        return requested, ""
    lang = LANG_BY_SUFFIX.get(source.suffix.lower())
    if lang:
        return lang, ""
    if source.suffix.lower() == ".c":
        return None, "judge_server 当前不支持 C 语言 runner，请改用 .cpp"
    return None, f"无法从后缀 {source.suffix!r} 判断语言，请用 --lang 指定"


# --------------------------------------------------------------------------
# step 06 · 输出比对（第 10 章「第六步」）
# --------------------------------------------------------------------------


def normalize_lines(text: str) -> list[str]:
    """忽略行尾空白和末尾空行，与 RunnerExecutionSupport.cpp 一致。"""
    lines = [line.rstrip(" \t\r") for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def compare_output(input_path: Path, expected_path: Path, user_output: Path,
                   checker: Optional[Path]) -> bool:
    if checker is not None:
        # fcmp2 的调用约定与 judge_server 相同：<输入> <用户输出> <标准答案>。
        proc = subprocess.run([str(checker), str(input_path), str(user_output),
                               str(expected_path)], capture_output=True, text=True)
        return proc.returncode == 0
    return normalize_lines(_read_text(expected_path)) == normalize_lines(_read_text(user_output))


def _clip(text: Optional[str], limit: int = 60) -> str:
    if text is None:
        return "<无此行>"
    return text if len(text) <= limit else text[:limit] + "…"


def first_difference(expected_path: Path, user_output: Path) -> str:
    """给 WA 找第一条不同的行，方便用户直接定位。"""
    expected = normalize_lines(_read_text(expected_path))
    actual = normalize_lines(_read_text(user_output))
    for index in range(max(len(expected), len(actual))):
        want = expected[index] if index < len(expected) else None
        got = actual[index] if index < len(actual) else None
        if want != got:
            return f"第 {index + 1} 行：期望 {_clip(want)}，实际 {_clip(got)}"
    return ""


# --------------------------------------------------------------------------
# step 04 · cgroup 可用性与自动委派（第 10 章「第四步」）
# --------------------------------------------------------------------------


def check_cgroup_root(root: Path) -> tuple[bool, str]:
    """确认父目录存在、已为子组启用 memory controller，并且真的能建子组。"""
    if not root.is_dir():
        return False, f"{root} 不存在"
    try:
        enabled = (root / "cgroup.subtree_control").read_text().split()
    except OSError as exc:
        return False, f"无法读取 {root}/cgroup.subtree_control：{exc}"
    if "memory" not in enabled:
        return False, f"{root} 未为子组启用 memory controller"
    probe = root / f"local-judge-probe-{os.getpid()}"
    try:
        probe.mkdir()
        probe.rmdir()
    except OSError as exc:
        return False, f"{root} 不可写：{exc}"
    return True, ""


def _delegate_mode_args() -> list[str]:
    """root 没有自己的用户管理器，自动委派改用系统管理器。

    `sudo python3 judge.py ...` 时 sudo 会重置环境（XDG_RUNTIME_DIR 消失），
    普通用户那条路走不通；而 systemd-run 的系统管理器本来就需要 root，正好对上。
    """
    return [] if os.geteuid() == 0 else ["--user"]


def delegation_blocker() -> str:
    """返回阻碍自动委派的原因；空串表示可以尝试。"""
    if not shutil.which("systemd-run"):
        return "未找到 systemd-run"
    # 用户管理器靠 XDG_RUNTIME_DIR 找到 socket；系统管理器不需要。
    if _delegate_mode_args() and not os.environ.get("XDG_RUNTIME_DIR"):
        return "缺少 XDG_RUNTIME_DIR"
    return ""


def try_auto_delegate() -> tuple[bool, str]:
    """探测能否用 systemd-run 起一个委派 scope；成功则重新执行自己。

    探测走的是与真实执行完全相同的命令形状（同一个 judge.py、同样的
    `--in-scope --` 位置），只是把要跑的命令换成一句 print。这样“探测通过”
    就等于“真跑能准备好父目录”，不会出现探测过了、执行却失败的情况。

    这样不必手工拼 `systemd-run [--user] --scope -p Delegate=yes`，
    也避免在容器等没有用户管理器的环境里报错退出。
    """
    blocker = delegation_blocker()
    if blocker:
        return False, blocker
    probe = [*_delegated_prefix(), sys.executable,
             "-c", "print('local-judge-delegated-ok')"]
    try:
        proc = subprocess.run(probe, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"systemd-run 探测失败：{exc}"
    if proc.returncode == 0 and "local-judge-delegated-ok" in proc.stdout:
        return True, ""
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return False, detail[-1] if detail else f"systemd-run 退出码 {proc.returncode}"


def _delegated_prefix() -> list[str]:
    """构造 `systemd-run ... judge.py --in-scope -- <命令>` 的前缀。

    --quiet 去掉 systemd-run 自己的 “Running as unit” 提示，但保留被测命令的
    stdout/stderr；--scope 让命令同步执行并原样传回退出码。

    这里不再经过 examples/delegated.py：那个脚本是给 make check 用的通用演示
    入口，本工具自带同一套准备逻辑，省掉一层解释器启动。
    """
    return ["systemd-run", *_delegate_mode_args(), "--quiet", "--scope",
            "-p", "Delegate=yes", "--",
            sys.executable, str(Path(__file__).resolve()), "--in-scope", "--"]


def _delegated_command(raw_args: list[str]) -> list[str]:
    """把用户参数包成在委派 scope 里执行的完整命令。

    `--` 之后是「要执行的命令」：重跑本 CLI，并带上原参数与 `--no-delegate`
    防止子进程再次尝试委派。与探测共用同一个前缀形状。
    """
    return [*_delegated_prefix(), sys.executable, str(Path(__file__).resolve()),
            *raw_args, "--no-delegate"]


def prepare_delegated_scope() -> tuple[bool, str]:
    """在 systemd-run 建立的 scope 内准备可委派的父目录（`--in-scope` 模式）。

    与 examples/delegated.py 做的是同一件事：从 /proc/self/cgroup 反推出所在
    scope 路径，确认它是本进程独占且可写的新 scope，再把执行器放进 manager
    叶子组、为父目录启用 memory controller，最后设置 ROJ_JUDGE_CGROUP_ROOT。

    为什么需要 manager 叶子组：cgroup v2 的 domain controller 要求管理目录本身
    没有进程。先把执行器搬进一个不受管辖的叶子组，父目录才能安全地开启 +memory。

    返回 (是否成功, 失败原因)。
    """
    try:
        line = next(text for text in
                    Path("/proc/self/cgroup").read_text().splitlines()
                    if text.startswith("0::"))
    except (OSError, StopIteration) as exc:
        return False, f"无法从 /proc/self/cgroup 读取所在 scope：{exc}"
    scope = line.split("::", 1)[1].strip()
    root = Path("/sys/fs/cgroup") / scope.lstrip("/")
    if not root.name.endswith(".scope") or not os.access(root, os.W_OK):
        return False, "--in-scope 只能在 systemd-run --scope -p Delegate=yes 内使用"
    try:
        members = set((root / "cgroup.procs").read_text().split())
        controllers = (root / "cgroup.controllers").read_text().split()
    except OSError as exc:
        return False, f"无法读取 {root} 的 cgroup 接口：{exc}"
    if members != {str(os.getpid())}:
        return False, f"{root} 不是本进程独占的新 scope，拒绝改动"
    if "memory" not in controllers:
        return False, f"{root} 没有可委派的 memory controller"
    try:
        manager = root / "manager"
        manager.mkdir(exist_ok=True)
        (manager / "cgroup.procs").write_text(str(os.getpid()))
        (root / "cgroup.subtree_control").write_text("+memory")
    except OSError as exc:
        return False, f"准备委派父目录失败：{exc}"
    os.environ["ROJ_JUDGE_CGROUP_ROOT"] = str(root)
    return True, ""


def run_in_scope(argv: list[str]) -> int:
    """`--in-scope` 模式：准备委派父目录，再 exec `--` 后面的命令。

    由 `_delegated_prefix()` 启动：argv 形如

        --in-scope -- <python> -c print(...)          # 探测
        --in-scope -- <python> judge.py <原参数> --no-delegate   # 真实重跑

    `--` 后面是「要执行的命令」而不是本 CLI 的参数，所以两种用途共用同一条路径。
    用 execvp 而不是 subprocess：准备完之后本进程没有任何剩余状态，直接换掉最省事，
    退出码也天然透传。
    """
    separator = argv.index("--") if "--" in argv else -1
    command = argv[separator + 1:] if separator >= 0 else []
    if not command:
        print("内部错误：--in-scope 后面缺少 `--` 与要执行的命令", file=sys.stderr)
        return 2
    ok, reason = prepare_delegated_scope()
    if not ok:
        print(f"无法在当前 scope 中准备委派目录：{reason}", file=sys.stderr)
        return 2
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"在委派 scope 中执行命令失败：{exc}", file=sys.stderr)
        return 2


def run_delegated(raw_args: list[str]) -> int:
    """在委派 scope 里换壳重跑自己，返回退出码。

    命令链是 `systemd-run --scope -- python3 judge.py --in-scope -- <原参数>`：
    systemd 先建 scope，`--in-scope` 分支在里面准备好父目录，再 os.execvp 回到同一个
    CLI、带着 ROJ_JUDGE_CGROUP_ROOT 直接命中隔离路径。没有额外的中间脚本。

    用 execvp 而不是 subprocess：父进程的状态到这里已经没用，直接换掉可以少一个
    进程、退出码天然透传；systemd-run 自己也是这个路子。
    """
    command = _delegated_command(raw_args)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"无法在委派 scope 中启动评测：{exc}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------
# 限制、执行适配与判定（judge 的职责）
#
# 一次 judge_case() 的职责分段：限制口径 → 校验 → 固定路径 → 身份 → cgroup 生命
# 周期 → 启动 executor → 收尾统计 → 判定 → 报告结果。判定和限制计算属于
# judge，启动 executor 与解析报告是唯一一处调用适配。
# --------------------------------------------------------------------------


class Verdict(str, enum.Enum):
    """测试点最终判定；OK 只是内部中间态，SYSTEM_ERROR 是基础设施故障。"""

    OK = "OK"                # 内部：执行正常，可以比较答案；不作为最终结果输出
    AC = "AC"
    WA = "WA"
    TLE = "TLE"
    MLE = "MLE"
    RE = "RE"
    SE = "SE"                # 基础设施故障（对应内部的 SYSTEM_ERROR）
    SYSTEM_ERROR = "SYSTEM_ERROR"  # 判定函数的中间态；展示前会映射为 SE


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

    def memory_max_bytes(self) -> int:
        """保护上限 = 判定线 + 余量，故意比判定线宽。"""
        if self.memory_kb == 0:
            return 0
        return (self.memory_kb + self.memory_slack_kb) * 1024

    def resolved_wall_ms(self) -> int:
        """wall 是独立的防卡死上限，包含 I/O 与等待时间。"""
        if self.wall_time_ms > 0:
            return self.wall_time_ms
        return self.time_ms + self.wall_slack_ms if self.time_ms > 0 else 0

    def validate(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field.name} 必须是非负整数")

    def executor_args(self, cgroup_procs: Optional[Path] = None) -> list[str]:
        """转换为 executor 的内部参数；顺序对应 C 中的 parse_options。

        保护值的计算集中在 make_protection_limits()；本方法只负责把结果
        排成 C 期望的顺序。cgroup 参数为空字符串时表示显式关闭入组；
        Popen 直接传递参数数组，因此空参数不会被吞掉。
        """
        p = make_protection_limits(self)
        args = [str(p.cpu_seconds), str(p.stack_bytes), str(p.output_bytes),
                str(p.nproc), str(p.wall_ms)]
        args.insert(1, str(cgroup_procs) if cgroup_procs is not None else "")
        return args


@dataclasses.dataclass
class ExecutionReport:
    """executor 报告的执行事实：C JSON 报告的七个字段，不含判定。

    判定（verdict）由 judge 根据这些事实加内存统计得出；本类型不携带任何
    评测标签，也不为缺失字段生成默认成功——无效报告必须显式报错。
    """

    cpu_time_us: int
    """精确 CPU 微秒数；判定用它，不用展示值。"""

    cpu_time_ms: int
    """C 侧按现有舍入方式得到的展示值。"""

    real_time_ms: int
    """wall 时间。"""

    rss_kb: int
    """wait4 的 ru_maxrss，仅辅助诊断，不用于内存判定。"""

    timed_out: bool
    """wall 看门狗是否触发。"""

    signal: int
    """提交因哪个信号终止；未发生时为 0。"""

    exit_code: int
    """提交正常退出时的退出码；被信号终止时为 0。"""

    # JSON 字段清单与 executor 的输出一一对应；类型不符或缺字段都必须报错，
    # 不能静默变成全零的“成功”报告。
    _REQUIRED_TYPES = {
        "cpu_time_us": int, "cpu_time_ms": int, "real_time_ms": int,
        "rss_kb": int, "timed_out": bool, "signal": int, "exit_code": int,
    }

    @classmethod
    def from_json(cls, text: str) -> "ExecutionReport":
        """解析 executor 的 stdout JSON；无效报告抛 ValueError。

        bool 是 int 的子类，所以先排除 bool 再检查 int，否则 timed_out
        的检查会被 0/1 混过，int 字段也会接受 true/false。
        """
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"executor 报告不是有效 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"executor 报告必须是 JSON 对象，实际是 {type(data).__name__}")
        missing = sorted(set(cls._REQUIRED_TYPES) - set(data))
        if missing:
            raise ValueError(f"executor 报告缺少字段: {', '.join(missing)}")
        extra = sorted(set(data) - set(cls._REQUIRED_TYPES))
        if extra:
            raise ValueError(f"executor 报告出现未知字段: {', '.join(extra)}")
        for name, expected in cls._REQUIRED_TYPES.items():
            value = data[name]
            if expected is int and type(value) is bool:
                raise ValueError(f"executor 报告字段 {name} 应为整数，实际是布尔值")
            if type(value) is not expected:
                raise ValueError(
                    f"executor 报告字段 {name} 应为 {expected.__name__}，"
                    f"实际是 {type(value).__name__}")
        fields = {name: data[name] for name in cls._REQUIRED_TYPES}
        return cls(**fields)


@dataclasses.dataclass
class CaseResult:
    """一个测试点的最终结果；verdict 是唯一的判定来源（AC/WA/TLE/MLE/RE/SE）。

    执行事实放在 report，内存统计放在 memory（None 表示未测量）。展示、
    汇总和序列化都读本对象，不再维护另一份可能不同步的局部判定。
    """

    verdict: Verdict = Verdict.SE
    message: str = ""
    """判定原因或基础设施故障描述（含可能的清理诊断）。"""

    report: Optional[ExecutionReport] = None
    """执行器报告的执行事实；启动失败等场景为 None。"""

    memory: Optional[MemoryResult] = None
    """cgroup 内存统计；无 cgroup 时为 None，不拿 RSS 补位。"""

    output_path: str = ""
    stderr_path: str = ""

    # 展示用便捷属性：从 report/memory 派生，避免调用方到处判空。
    @property
    def cpu_time_ms(self) -> int:
        return self.report.cpu_time_ms if self.report else 0

    @property
    def cpu_time_us(self) -> int:
        return self.report.cpu_time_us if self.report else 0

    @property
    def real_time_ms(self) -> int:
        return self.report.real_time_ms if self.report else 0

    @property
    def rss_kb(self) -> int:
        return self.report.rss_kb if self.report else 0

    @property
    def timed_out(self) -> bool:
        return self.report.timed_out if self.report else False

    @property
    def signal(self) -> int:
        return self.report.signal if self.report else 0

    @property
    def exit_code(self) -> int:
        return self.report.exit_code if self.report else 0

    @property
    def memory_kb(self) -> int:
        return (self.memory.peak_bytes + 1023) // 1024 if self.memory else 0

    def to_dict(self) -> Dict[str, object]:
        """序列化；verdict 与对象一致，不会出现两套表示。"""
        return {
            "verdict": self.verdict.value,
            "message": self.message,
            "output_path": self.output_path,
            "stderr_path": self.stderr_path,
        }


def _child_env(work_dir: Path, inherit: bool) -> Dict[str, str]:
    """给被测进程准备环境。"""
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


class ExecutorError(RuntimeError):
    """executor 本身的基础设施故障：启动、重定向、入组、exec、报告无效。

    与“提交程序非零退出”严格区分——后者是执行事实，走报告的 exit_code；
    本异常由 judge 映射为 SE，不能伪装成用户程序的退出行为。
    """


def invoke_executor(command: list[str], env: Dict[str, str]) -> ExecutionReport:
    """启动 executor，等待监控结束，把 JSON 报告解析成 ExecutionReport。

    两条数据通道严格分离：executor 的 stdout 是 JSON 报告，用户程序的
    stdout 是输出文件，用户打印任意文本都不会污染报告。Python 只等待
    executor；用户进程的 wait4 必须由其直接父进程 executor 完成。

    executor 非零退出、报告无效都属于基础设施故障，抛 ExecutorError；
    取消（Ctrl+C）先通知 executor 终止并等待它清理提交进程组，超宽限
    后强杀，再原样重新抛出取消。
    """
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
        )
    except OSError as exc:
        # executor 不存在或无法启动：属于基础设施故障，不是提交程序的退出事实。
        raise ExecutorError(f"无法启动 executor: {exc}") from exc
    try:
        stdout, stderr = process.communicate()
    except BaseException:
        # Ctrl+C / 调用方异常时先让 executor 处理 SIGTERM：它会杀掉提交进程组并
        # wait4 回收。不能只杀 executor，否则被测进程的后代可能继续留在后台。
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    if process.returncode != 0:
        raise ExecutorError(
            f"executor 失败（exit {process.returncode}）: {stderr.strip()}")
    try:
        return ExecutionReport.from_json(stdout)
    except ValueError as exc:
        raise ExecutorError(str(exc)) from exc


# ── step 01 · 集中限制计算与纯判定规则 ───────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class ProtectionLimits:
    """从题目阈值加余量算出的实际保护上限；字段直接表达 executor 需要的量。

    这些值只用于“不让进程失控”，不参与最终判定。判定的比对对象始终是原始
    题目阈值（Limits.time_ms / Limits.memory_kb），不因为多留了余量而放宽。
    """

    cpu_seconds: int
    """CPU soft limit 秒数（题目阈值 + CPU 余量后向上取整）。

    写入 RLIMIT_CPU 的 rlim_cur；executor 会把 rlim_max 再设高 1 秒，
    所以 hard limit 比本值多一秒（默认：soft 2s / hard 3s）。
    """

    memory_max_bytes: int
    """cgroup memory.max 字节数（阈值 + 内存余量）；0 表示不设。"""

    wall_ms: int
    """wall 看门狗毫秒数；0 表示不限。"""

    stack_bytes: int
    """栈限制字节数；0 表示不覆盖继承值。"""

    output_bytes: int
    """单个输出文件限制字节数；0 表示不设。"""

    nproc: int
    """同一真实 UID 的进程/线程数限制；0 表示不设。"""


def make_protection_limits(limits: Limits) -> ProtectionLimits:
    """把题目阈值换算成 executor 的保护参数（集中的唯一一处换算）。

    负责：CPU 余量加完后向上取整成秒、内存与栈/输出换算成字节、0 关闭
    对应限制。不负责判定——判定用原始阈值，见 classify_execution。
    溢出到 executor 整数范围之外时抛 ValueError。
    """
    cpu_seconds = (limits.time_ms + limits.cpu_slack_ms + 999) // 1000 if limits.time_ms else 0
    memory_max_bytes = limits.memory_max_bytes()
    values = [cpu_seconds, limits.stack_mb * 1024 * 1024,
              limits.output_limit_mb * 1024 * 1024, limits.nproc, limits.resolved_wall_ms(),
              memory_max_bytes]
    if any(value >= 2**63 - 1 for value in values):
        raise ValueError("资源限制超出 executor 支持的整数范围")
    return ProtectionLimits(
        cpu_seconds=cpu_seconds,
        memory_max_bytes=memory_max_bytes,
        wall_ms=limits.resolved_wall_ms(),
        stack_bytes=limits.stack_mb * 1024 * 1024,
        output_bytes=limits.output_limit_mb * 1024 * 1024,
        nproc=limits.nproc,
    )


@dataclasses.dataclass
class MemoryResult:
    """cgroup 内存统计；None 表示无 cgroup 时未测量（不得用 RSS 补位）。"""

    peak_bytes: int
    oom_events: int = 0
    oom_kills: int = 0

    @classmethod
    def from_group(cls, stats: Dict[str, int]) -> "MemoryResult":
        return cls(
            peak_bytes=stats.get("memory_peak_bytes", 0),
            oom_events=stats.get("oom_events", 0),
            oom_kills=stats.get("oom_kills", 0),
        )


def classify_execution(report: ExecutionReport, memory: Optional[MemoryResult],
                       limits: Limits) -> tuple[Verdict, str]:
    """根据执行事实、内存统计和题目阈值返回执行结局与原因。

    纯函数：不启动进程、不读文件、不操作 cgroup、不打印。混合证据的优先级
    在这里一处决定（OOM → wall → 峰值 → CPU/SIGXCPU → 信号 → 退出码 → OK）。
    没有 OOM 证据时不凭 SIGKILL 或 stderr 文本猜测 MLE。
    返回 Verdict.OK 仅表示“可以比较答案”，不是最终测试点结果。
    """
    if memory is not None and (memory.oom_events or memory.oom_kills):
        return Verdict.MLE, "cgroup 记录了内存 OOM 事件"
    if report.timed_out:
        return Verdict.TLE, "wall-clock 超时，已终止提交进程组"
    if memory is not None and limits.memory_kb and memory.peak_bytes > limits.memory_kb * 1024:
        return Verdict.MLE, "cgroup 内存峰值超过题目限制"
    if (limits.time_ms and report.cpu_time_us > limits.time_ms * 1000) or report.signal == signal.SIGXCPU:
        return Verdict.TLE, "CPU 时间超限"
    if report.signal:
        return Verdict.RE, f"被信号 {report.signal} 终止"
    if report.exit_code:
        return Verdict.RE, f"非零退出码 {report.exit_code}"
    # 没有 OOM 证据时，SIGKILL/非零退出码仍为 RE；不根据 stderr 猜 MLE。
    # CPU 判断使用原始微秒数，cpu_time_ms 的四舍五入仅供展示。
    return Verdict.OK, ""


# ── step 03 · 固定文件路径（第 04 章）────────────────────────────────────────
def _same_file(a: Path, b: Path) -> bool:
    """两个路径是否指向同一个文件：字符串相同、符号链接、硬链接都算。"""
    if a.resolve() == b.resolve():
        return True
    # samefile 需要两边都存在；不存在时已由上面的 resolve 比较覆盖。
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _check_stream_paths(*paths: Path) -> None:
    """防止 O_TRUNC 截断输入或让两个输出互相覆盖；也检查符号链接和硬链接。"""
    for i, path in enumerate(paths):
        for other in paths[:i]:
            if _same_file(path, other):
                raise ValueError("stdin、stdout、stderr 必须使用不同的文件")


def _check_readonly_targets(expected_path: Path, *write_paths: Path) -> None:
    """拒绝把写目标指向只读输入（题目标准答案）。

    executor 会用 O_TRUNC 打开 stdout/stderr，写目标一旦与标准答案同文件，
    就会先抹掉题目数据、再拿被覆盖的文件自我比较——错误答案也会得到 AC。
    这里在启动任何进程前拦截。符号链接与硬链接别名一律算冲突。
    两个只读输入共享路径是允许的：本函数只看“写目标 vs 只读输入”。
    """
    if expected_path is None:
        return
    for target in write_paths:
        if _same_file(target, expected_path):
            raise ValueError(
                f"输出目标不能与题目标准答案指向同一个文件：{target}")


def _describe_failure(exc: BaseException) -> str:
    """把基础设施故障和附带的清理诊断合成一条可读消息。

    MemoryCgroup 在清理失败时不会另抛异常，而是把诊断挂到原始异常上
    （cleanup_notes）；这里把它们一起呈现，避免清理错误掩盖执行原因。
    """
    message = str(exc)
    notes = getattr(exc, "cleanup_notes", None) or []
    if notes:
        message += "；清理失败：" + "；".join(notes)
    return message


def _prepare_execution(argv: list[str], input_path: Path, output_path: Path,
                       limits: Limits, *, stderr_path: Optional[Path], cwd: Optional[Path],
                       run_uid: int, run_gid: int, drop_privileges: Optional[bool],
                       executor_path: Optional[Path],
                       expected_path: Optional[Path] = None) -> tuple[list[str], Path, Path, Path, Path, bool]:
    """参数校验与路径固定；返回 (argv, 工作目录, 输入, 输出, stderr, 是否降权)。

    校验顺序在启动任何进程之前：先拒绝标准流互相冲突，再拒绝写目标
    指向题目标准答案。两者都抛 ValueError，不会截断任何文件。
    """
    if not argv:
        raise ValueError("argv 不能为空")
    limits.validate()
    for name, value in (("run_uid", run_uid), ("run_gid", run_gid)):
        if type(value) is not int or not 0 <= value < 2**32 - 1:
            raise ValueError(f"{name} 必须是有效的非负 UID/GID")
    # 先固定文件路径：executor 改变 cwd 之后，重定向仍要指向调用者指定的文件。
    input_path = Path(input_path).absolute()
    output_path = Path(output_path).absolute()
    stderr_path = Path(stderr_path or str(output_path) + ".err").absolute()
    _check_stream_paths(input_path, output_path, stderr_path)
    if expected_path is not None:
        # 只读输入（标准答案）不能被 stdout/stderr 的写目标覆盖。
        _check_readonly_targets(Path(expected_path).absolute(), output_path, stderr_path)
    work_dir = Path(cwd or Path.cwd()).absolute()
    if drop_privileges is None:
        drop_privileges = os.geteuid() == 0
    return argv, work_dir, input_path, output_path, stderr_path, bool(drop_privileges)


# --------------------------------------------------------------------------
# 展示（服务于 step 05 与 step 07 的输出）
# --------------------------------------------------------------------------


def format_case_line(index: int, name: str, result: CaseResult) -> str:
    """一行展示；判定与详情都只从 result 读取。"""
    verdict = result.verdict.value
    timing = f"{result.cpu_time_ms:>5}ms {result.memory_kb / 1024:>6.1f}MiB"
    line = f"  #{index:<3} {name:<12} {verdict:<6} {timing}"
    detail = describe(result)
    return f"{line}   {detail}" if detail else line


def describe(result: CaseResult) -> str:
    """把结果的判定与事实转成一行详情；不再接受外部传入的判定。"""
    verdict = result.verdict
    if verdict is Verdict.AC:
        return ""
    if verdict is Verdict.WA:
        return result.message  # judge_case 已算好首行差异
    if verdict is Verdict.TLE:
        wall = "wall 超时，" if result.timed_out else ""
        return f"{wall}CPU {result.cpu_time_ms}ms / 实际 {result.real_time_ms}ms"
    if verdict is Verdict.MLE:
        return f"内存峰值 {result.memory_kb / 1024:.1f}MiB"
    if verdict is Verdict.RE:
        if result.signal:
            return f"被信号 {result.signal} 终止"
        return f"退出码 {result.exit_code}"
    return result.message


def list_problems(testdata_root: Path, tried: list[Path]) -> int:
    if not testdata_root.is_dir():
        return report_missing_testdata(tried)
    print(f"测试数据目录：{testdata_root}")
    for problem_dir in sorted(testdata_root.iterdir(), key=lambda p: _natural_key(p.name)):
        if not problem_dir.is_dir():
            continue
        data_dir = problem_dir / "data"
        count = len(load_cases(data_dir)) if data_dir.is_dir() else 0
        title, time_ms, memory_mb, io_config = load_problem_config(problem_dir)
        mode_note = f"  {io_config.mode}" if io_config.is_file_mode else ""
        print(f"  {problem_dir.name:<8} {count:>3} 个测试点  {time_ms}ms / {memory_mb}MiB{ mode_note }  {title}")
    return 0


# --------------------------------------------------------------------------
# 主流程：step 01 → step 07 的实际执行顺序
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="judge.py",
        description="本地评测：编译提交、跑测试点、比对答案（不需要 judge_server）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python3 judge.py --pid 1000 solution.cpp
  python3 judge.py --list
  python3 judge.py --pid 1000 solution.py --time 2000 --memory 256
""")
    parser.add_argument("source", nargs="?", type=Path, help="提交源文件（.cpp 或 .py）")
    parser.add_argument("--pid", help="题目编号，对应 testData/<pid>/")
    parser.add_argument("--lang", choices=("auto", "cpp", "python"), default="auto",
                        help="提交语言，默认按后缀判断")
    parser.add_argument("--testdata", type=Path, default=None,
                        help="测试数据根目录；默认依次尝试 ./testData、./../testData、包内 testData/")
    parser.add_argument("--time", type=int, help="CPU 限制 ms，覆盖题目 config.json")
    parser.add_argument("--memory", type=int, help="内存限制 MiB，覆盖题目 config.json")
    parser.add_argument("--checker", default="auto",
                        help="输出比较器；auto 表示存在 /judge/checker/fcmp2 就用它，none 强制按行比较")
    parser.add_argument("--cgroup-root", type=Path, default=None,
                        help=f"已委派的 cgroup v2 父目录，默认 $ROJ_JUDGE_CGROUP_ROOT 或 {DEFAULT_CGROUP_ROOT}")
    parser.add_argument("--delegate", choices=("auto", "never", "always"), default="auto",
                        help="cgroup 不可用时是否自动用 systemd-run 起委派 scope")
    parser.add_argument("--no-delegate", dest="delegate", action="store_const", const="never",
                        help="内部使用：禁止再次自动委派")
    parser.add_argument("--no-cgroup", action="store_true",
                        help="跳过 cgroup，直接用降级模式（只限 wall 和 CPU）")
    parser.add_argument("--keep-work-dir", action="store_true",
                        help="保留临时工作目录，便于查看输出和编译日志")
    parser.add_argument("--list", action="store_true", help="列出 testData 下可用的题目后退出")
    return parser


def resolve_checker(spec: str) -> tuple[Optional[Path], str]:
    if spec == "none":
        return None, "内置按行比较（忽略行尾空白和末尾空行）"
    if spec == "auto":
        if CHECKER_PATH.is_file():
            return CHECKER_PATH, f"{CHECKER_PATH}（与 judge_server 优先路径一致）"
        return None, "内置按行比较（未找到 /judge/checker/fcmp2）"
    path = Path(spec)
    if not path.is_file():
        raise SystemExit(f"指定的比较器不存在：{path}")
    return path, str(path)


def _run_transaction(run_argv: list[str], input_path: Path, output_path: Path,
                     limits: Limits, *, work_dir: Path, stderr_path: Optional[Path],
                     cwd: Optional[Path], run_uid: int, run_gid: int,
                     drop_privileges: Optional[bool], inherit_env: bool,
                     executor_path: Optional[Path], cgroup_root: Optional[Path],
                     isolated: bool, expected_path: Optional[Path],
                     checker: Optional[Path] = None,
                     answer_path: Optional[Path] = None) -> CaseResult:
    """测试点事务的共用核心：创建组 → 执行 → 停止后代 → 读统计 → 删组 → 判定。

    expected_path 为 None 时不做答案比对，内部 OK 原样返回（供
    execute_program 使用）；给出时，只有执行正常才比较答案，收口成 AC/WA。
    answer_path 是比较答案实际读取的文件；未给出时用 output_path
    （stdio 模式二者相同）。

    cgroup 文件操作仍全部在 MemoryCgroup 里，本函数不复制它们；executor 的
    启动与报告解析交给 invoke_executor()；判定交给 classify_execution()。
    取消（KeyboardInterrupt）不被吸收，向顶层传播。
    """
    limits = limits or Limits()
    answer_abs = Path(answer_path).absolute() if answer_path else Path(output_path).absolute()
    executor = Path(executor_path or Path(__file__).with_name("executor")).absolute()
    try:
        argv, work_dir_abs, input_abs, output_abs, stderr_abs, drop = _prepare_execution(
            run_argv, input_path, output_path, limits, stderr_path=stderr_path,
            cwd=cwd or work_dir, run_uid=run_uid, run_gid=run_gid,
            drop_privileges=drop_privileges, executor_path=executor,
            expected_path=expected_path,
        )
        protection = make_protection_limits(limits)
        if not executor.is_file() or not os.access(executor, os.X_OK):
            raise FileNotFoundError(
                f"找不到可执行的 executor: {executor}；请先在其源码目录运行 make")
        root = Path(cgroup_root or os.environ.get(
            "ROJ_JUDGE_CGROUP_ROOT", "/sys/fs/cgroup/roj-judge")).absolute()
        # 无 cgroup 用空上下文；两种模式的执行代码只有一份。
        context = MemoryCgroup(root, protection.memory_max_bytes) if isolated else nullcontext()
        with context as group:
            command = [
                str(executor), *limits.executor_args(group.procs_path if group is not None else None),
                str(int(drop)), str(run_uid), str(run_gid),
                str(work_dir_abs), str(input_abs), str(output_abs), str(stderr_abs), *argv,
            ]
            report = invoke_executor(command, _child_env(work_dir_abs, inherit_env))
            memory: Optional[MemoryResult] = None
            if group is not None:
                # 直接子进程退出不代表所有后代都退出。先停止整个组，再读取最终
                # 峰值和 OOM 事件；with 的退出清理也覆盖启动失败和 Ctrl+C。
                group.stop()
                memory = MemoryResult.from_group(group.memory_result())
            # 没有 cgroup 就没有内存超限证据，不拿 rss_kb 补位。
            verdict, message = classify_execution(report, memory, limits)
            result = CaseResult(verdict=verdict, message=message,
                                report=report, memory=memory)
        # 只有执行正常时才看答案——OK 是内部中间态，在此收口成 AC/WA。
        # 写目标（stdout/stderr）仍受只读保护；答案文件是提交写、judge 读，
        # 不做写目标检查，只检查其类型与别名。
        if expected_path is not None and result.verdict is Verdict.OK:
            expected_abs = Path(expected_path).absolute()
            usable, why = case_io.check_answer_file(
                answer_abs, stdin_path=input_abs, expected_path=expected_abs)
            if not usable:
                result.verdict = Verdict.WA
                result.message = why
            else:
                ac = compare_output(input_abs, expected_abs, answer_abs, checker)
                result.verdict = Verdict.AC if ac else Verdict.WA
                if not ac:
                    result.message = first_difference(expected_abs, answer_abs)
    except KeyboardInterrupt:
        # 取消不被吸收：停止整次评测，由顶层给出退出码 130。
        raise
    except (OSError, ExecutorError) as exc:
        # 基础设施故障映射为 SE。若同时有清理失败，两份诊断都保留。
        # ValueError（配置/路径错误）不在此处捕获：那是调用方的编程错误，
        # 应在启动任何进程之前抛出，而不是变成一个测试点的 SE。
        # 配置准备可能尚未算出绝对路径（_prepare_execution 抛错），
        # 所以这里退回调用方传入的原值，不引用未赋值的局部变量。
        return CaseResult(verdict=Verdict.SE, message=_describe_failure(exc),
                          output_path=str(output_path), stderr_path=str(stderr_path or ""))
    # 返回路径：output_path 指向实际用于判题的答案文件（见 plan §5.3）。
    result.output_path = str(answer_abs)
    result.stderr_path = str(stderr_abs)
    return result


def judge_case(run_argv: list[str], input_path: Path, expected_path: Path,
               limits: Limits, *, work_dir: Path, index: int, checker: Optional[Path],
               cgroup_root: Path, isolated: bool,
               run_uid: int = 65534, run_gid: int = 65534,
               drop_privileges: Optional[bool] = None, inherit_env: bool = False,
               executor_path: Optional[Path] = None,
               output_path: Optional[Path] = None,
               stderr_path: Optional[Path] = None,
               cwd: Optional[Path] = None,
               answer_output_path: Optional[Path] = None) -> CaseResult:
    """一个测试点的完整事务：执行 → 收尾 → 判定 → 比较答案。

    返回值就是最终结果（AC/WA/TLE/MLE/RE/SE）；内部 OK 不会成为最终结果。

    output_path 是 executor 抛获 stdout 的文件；answer_output_path 是比较
    答案实际读取的文件（file 模式下为提交生成的输出文件）。未给出时两者
    相同，现有调用不变。

    写目标与原输入/标准答案必须是不同文件：同路径、软链接、硬链接都会在
    启动 executor 之前被 _check_readonly_targets 拒绝（ValueError）。
    """
    output_dir = Path(cwd) if cwd else work_dir
    user_output = Path(output_path) if output_path else output_dir / f"case-{index}.out"
    if stderr_path is not None:
        stderr_path = Path(stderr_path)
    elif output_path:
        stderr_path = user_output.with_suffix(user_output.suffix + ".err")
    else:
        stderr_path = output_dir / f"case-{index}.err"
    answer_path = Path(answer_output_path) if answer_output_path else user_output
    return _run_transaction(
        run_argv, input_path, user_output, limits,
        stderr_path=stderr_path, work_dir=work_dir, cwd=cwd,
        run_uid=run_uid, run_gid=run_gid, drop_privileges=drop_privileges,
        inherit_env=inherit_env, executor_path=executor_path,
        cgroup_root=cgroup_root, isolated=isolated, expected_path=expected_path,
        checker=checker, answer_path=answer_path,
    )


def execute_program(run_argv: list[str], input_path: Path, output_path: Path,
                    limits: Optional[Limits] = None, *, work_dir: Path,
                    stderr_path: Optional[Path] = None, cwd: Optional[Path] = None,
                    run_uid: int = 65534, run_gid: int = 65534,
                    drop_privileges: Optional[bool] = None, inherit_env: bool = False,
                    executor_path: Optional[Path] = None,
                    cgroup_root: Optional[Path] = None, isolated: bool = True) -> CaseResult:
    """只执行一个程序并返回执行事实，不比较答案。

    与 judge_case 共用同一份 _run_transaction()（cgroup 创建 → 执行 → 停止
    → 读统计 → 删除），区别只是不做答案比对：内部 OK 保持为 OK。供想自己
    做判定的调用方和执行层测试使用，避免为了拿到事实而伪造标准答案。
    """
    limits = limits or Limits()
    return _run_transaction(
        run_argv, input_path, Path(output_path), limits,
        stderr_path=stderr_path, work_dir=work_dir, cwd=cwd,
        run_uid=run_uid, run_gid=run_gid, drop_privileges=drop_privileges,
        inherit_env=inherit_env, executor_path=executor_path,
        cgroup_root=cgroup_root, isolated=isolated, expected_path=None,
    )


def judge_submission(source: Path, lang: str, cases: list[tuple[str, Path, Path]],
                     limits: Limits, *, checker: Optional[Path],
                     cgroup_root: Path, isolated: bool,
                     keep_work_dir: bool = False,
                     io_config: Optional[case_io.IOConfig] = None,
                     drop_privileges: Optional[bool] = None,
                     run_uid: int = 65534, run_gid: int = 65534) -> int:
    """一份提交的完整评测：管理工作目录，编译一次，逐点执行，汇总并清理。

    资源边界：工作目录在进入后立即创建，并在 finally 里清理（除非
    keep_work_dir），所以编译失败、执行异常、取消都经过同一个收尾。
    每个测试点有独立的运行目录（见 case_io.prepare_case_files）；
    单点的进程/cgroup 生命周期在 judge_case 里。

    返回 CLI 退出码：全部 AC 为 0，有非 AC 为 1，编译失败为 2。
    """
    io_config = io_config or case_io.IOConfig()
    drop = case_io.resolve_drop_privileges(drop_privileges, run_uid)
    work_dir = Path(tempfile.mkdtemp(prefix="local-judge-"))
    try:
        # chmod 与编译、执行同在清理边界内：目录一旦建成就不会因
        # 后续任何失败（包括权限设置失败）而遗留。
        if os.geteuid() == 0:
            # 降权后运行的提交需要能进入工作目录；judge_server 同样要处理这一点。
            os.chmod(work_dir, 0o755)
        # 编译一次，之后所有测试点复用同一个产物。
        run_argv, compile_output = compile_submission(lang, source, work_dir)
        if run_argv is None:
            print("编译失败（CE）：")
            print(compile_output or "（编译器没有输出）")
            return 2
        print("编译通过")

        started = time.monotonic()
        verdicts: list[str] = []
        for index, (name, input_path, expected_path) in enumerate(cases, start=1):
            # 准备失败（权限、磁盘等）记为 SE，继续下一点，与现有循环策略一致。
            try:
                files = case_io.prepare_case_files(
                    work_dir, index, input_path, io_config,
                    drop=drop, run_uid=run_uid, run_gid=run_gid)
            except OSError as exc:
                verdicts.append("SE")
                print(f"  #{index:<3} {name:<12} {'SE':<6}   准备测试点目录失败：{exc}")
                continue
            # 准备成功立即进入清理边界，中间不插入可能失败的操作。
            try:
                result = judge_case(
                    run_argv, files.stdin_path, expected_path, limits,
                    work_dir=work_dir, index=index, checker=checker,
                    cgroup_root=cgroup_root, isolated=isolated,
                    drop_privileges=drop, cwd=files.cwd,
                    output_path=files.stdout_path, stderr_path=files.stderr_path,
                    answer_output_path=files.answer_path,
                    run_uid=run_uid, run_gid=run_gid,
                )
            finally:
                # 进程/cgroup 清理失败时不删目录，保留以免丢失诊断。
                if not keep_work_dir:
                    shutil.rmtree(files.cwd, ignore_errors=True)
            verdicts.append(result.verdict.value)
            print(format_case_line(index, name, result))

        # 计时口径保持为“测试点执行阶段”，不含编译时间。
        elapsed = time.monotonic() - started
        passed = verdicts.count("AC")
        # 汇总取第一个非 AC 的结果，便于一眼看到“卡在哪一步”。
        overall = next((v for v in verdicts if v != "AC"), "AC")
        print()
        print(f"结果：{overall}  通过 {passed}/{len(cases)}  用时 {elapsed:.2f}s")
        if keep_work_dir:
            print(f"工作目录：{work_dir}")
        return 0 if overall == "AC" else 1
    finally:
        # 与 judge_case 的进程/cgroup 清理各管一类资源。
        if not keep_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: Optional[list[str]] = None) -> int:
    # ── step 01 · 运行 CLI：把一份提交交给本地评测 ────────────────────────
    #    文章「第一步」那条命令背后执行的，就是这个函数。
    raw_args = list(sys.argv[1:] if argv is None else argv)

    # `--in-scope` 的 payload 是一条任意命令（探测时是 `python -c print(...)`，
    # 真实重跑时是 `python judge.py <原参数>`），不能用本 CLI 的 parser 去解析，
    # 否则第一个 argparse 不认识的开关就会在进 in-scope 分支之前报错。
    if "--in-scope" in raw_args:
        return run_in_scope(raw_args)

    parser = build_parser()
    args = parser.parse_args(argv)

    # ── step 02 · 解析参数、定位测试数据、读题目限制 ──────────────────────
    testdata_root, tried = resolve_testdata(args.testdata)
    if args.list:
        return list_problems(testdata_root, tried) if testdata_root else report_missing_testdata(tried)

    if not args.pid:
        parser.error("请用 --pid 指定题目编号，或先用 --list 查看可用题目")
    if args.source is None:
        parser.error("请提供提交源文件，例如：python3 judge.py --pid 1000 solution.cpp")

    source = args.source.resolve()
    if not source.is_file():
        print(f"提交文件不存在：{source}", file=sys.stderr)
        return 2
    lang, error = detect_language(source, args.lang)
    if lang is None:
        print(error, file=sys.stderr)
        return 2

    # 查找失败会返回 None；先给出尝试过的目录，再退出，不能直接拼接题号。
    if testdata_root is None:
        return report_missing_testdata(tried)

    problem_dir = testdata_root / args.pid
    data_dir = problem_dir / "data"
    if not data_dir.is_dir():
        print(f"找不到测试数据：{data_dir}", file=sys.stderr)
        print(f"可用题目：{', '.join(p.name for p in sorted(testdata_root.iterdir()))}"
              if testdata_root.is_dir() else "", file=sys.stderr)
        return 2
    cases = load_cases(data_dir)
    if not cases:
        print(f"{data_dir} 下没有配对的 .in/.out 测试点", file=sys.stderr)
        return 2

    title, time_ms, memory_mb = "", 1000, 128
    try:
        title, time_ms, memory_mb, io_config = load_problem_config(problem_dir)
    except case_io.IOConfigError as exc:
        # 配置错误在编译前退出：不浪费一次编译，也不产生测试点输出。
        print(f"题目配置错误（{problem_dir / 'config.json'}）：{exc}", file=sys.stderr)
        return 2
    if args.time is not None:
        time_ms = args.time
    if args.memory is not None:
        memory_mb = args.memory
    limits = Limits(time_ms=time_ms, memory_kb=memory_mb * 1024)

    # ── step 06 · 比对规则：这里只决定用哪种比较器 ────────────────────────
    #    具体规则在「输出比对」那一段（normalize_lines / compare_output）。
    checker, checker_note = resolve_checker(args.checker)

    # ── step 04 · 决定执行模式 ────────────────────────────────────────────
    #    注意：这一步排在 step 03（编译）之前，与文章编号顺序相反。
    #    因为它可能用 run_delegated() 重启整个 CLI，后面的代码根本不会跑到。
    # 决定执行模式：cgroup 隔离 -> 自动委派重跑 -> 降级执行。
    cgroup_root = Path(args.cgroup_root or os.environ.get("ROJ_JUDGE_CGROUP_ROOT", DEFAULT_CGROUP_ROOT))
    isolated, reason = (False, "已用 --no-cgroup 禁用") if args.no_cgroup else check_cgroup_root(cgroup_root)
    # 显式传 --cgroup-root 时尊重用户的选择，不再自动换一个 scope 去委派。
    may_delegate = (not isolated and not args.no_cgroup
                    and args.cgroup_root is None and args.delegate != "never")
    if may_delegate:
        delegated, delegate_reason = try_auto_delegate()
        if delegated:
            # run_delegated 用 execvp 换掉本进程，正常情况下不会返回。
            return run_delegated(raw_args)
        if args.delegate == "always":
            print(f"无法建立 cgroup 委派：{delegate_reason}", file=sys.stderr)
            return 2
        reason = f"{reason}；自动委派不可用（{delegate_reason}）"

    print(f"题目 {args.pid}" + (f"  {title}" if title else ""))
    print(f"提交 {source.name}（{lang}）")
    print(f"限制 CPU {time_ms}ms / 内存 {memory_mb}MiB / wall {limits.resolved_wall_ms()}ms")
    if io_config.is_file_mode:
        print(f"IO 文件模式：输入 {io_config.input_file} / 输出 {io_config.output_file}")
    else:
        print("IO 标准流模式")
    if isolated:
        print(f"执行 cgroup 隔离，root={cgroup_root}")
    else:
        print(f"执行降级模式：{reason}")
        if os.geteuid() == 0:
            print("  root 想启用隔离：先准备一个委派父目录（详见 README「构建与权限」），例如")
            print(f"    mkdir -p {DEFAULT_CGROUP_ROOT} && "
                  f"echo +memory > {DEFAULT_CGROUP_ROOT}/cgroup.subtree_control")
        print("  ⚠ 无内存隔离，MLE 无法判定；TLE 依赖 wall 与 RLIMIT_CPU（整秒）")
    print(f"比较 {checker_note}")
    print()

    # ── step 03 · 编译并逐点评测；工作目录与清理由 judge_submission 管理 ──
    return judge_submission(
        source, lang, cases, limits, checker=checker,
        cgroup_root=cgroup_root, isolated=isolated,
        keep_work_dir=args.keep_work_dir, io_config=io_config,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt as exc:
        # 取消：终止整次评测，退出码 130。
        # 如果清理失败，诊断已附在异常上（MemoryCgroup.__exit__）；
        # 用户应当知道可能残留的 cgroup 或进程，否则无从排查。
        # 普通取消（无清理失败）不输出任何额外信息。
        notes = getattr(exc, "cleanup_notes", None) or []
        if notes:
            print("取消时清理失败：", file=sys.stderr)
            for note in notes:
                print(f"  {note}", file=sys.stderr)
        sys.exit(130)
