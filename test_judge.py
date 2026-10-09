"""评测流程回归：限制计算、判定优先级、测试数据发现、自动委派。

make check；不需要 OJ 数据或 judge_server。"""

import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import judge
from judge import (ExecutionReport, Limits, MemoryResult, ProtectionLimits, Verdict,
                   classify_execution, judge_case, make_protection_limits, run_case)


class LocalJudgeTests(unittest.TestCase):
    def test_missing_testdata_reports_search_paths(self):
        with tempfile.TemporaryDirectory(prefix="local-judge-test-") as directory:
            root = Path(directory)
            source = root / "solution.py"
            source.write_text("print(3)\n")
            tried = [root / "testData", root / "another" / "testData"]
            # 固定查找结果，避免测试意外使用开发机器上其他目录的题目数据。
            with patch.object(judge, "resolve_testdata", return_value=(None, tried)), \
                    patch.object(judge, "run_case") as run:
                for args in (["--pid", "1000", str(source)], ["--list"]):
                    with self.subTest(args=args):
                        error = io.StringIO()
                        with redirect_stderr(error):
                            exit_code = judge.main(args)
                        self.assertEqual(exit_code, 2)
                        self.assertIn("找不到测试数据目录", error.getvalue())
                        self.assertIn("--testdata", error.getvalue())
                        for path in tried:
                            self.assertIn(str(path), error.getvalue())
                run.assert_not_called()

    def test_degraded_cli_verdicts_and_automatic_fallback(self):
        # 用一题一测试点贯通“编译 → 统一执行器 → 比对 → 汇总”。
        # 既检查显式 --no-cgroup，也检查指定的 cgroup 不可用时 CLI 的自动降级。
        with tempfile.TemporaryDirectory(prefix="local-judge-test-") as directory:
            root = Path(directory)
            root.chmod(0o755)  # root 运行测试时，降权后的 Python 也需要读取源文件。
            data = root / "testData" / "1000" / "data"
            data.mkdir(parents=True)
            (data / "1.in").write_text("1 2\n")
            (data / "1.out").write_text("3\n")
            source = root / "solution.py"
            cases = [
                ("print(sum(map(int, input().split())))", "AC", 0),
                ("print(0)", "WA", 1),
                ("raise RuntimeError('example')", "RE", 1),
                ("def broken(:", "CE", 2),
                ("import time; time.sleep(10)", "TLE", 1),
            ]
            for code, verdict, exit_code in cases:
                with self.subTest(verdict=verdict):
                    source.write_text(code)
                    process = subprocess.run([
                        sys.executable, str(Path(__file__).with_name("judge.py")),
                        "--pid", "1000", "--testdata", str(root / "testData"),
                        "--checker", "none", "--time", "1000", "--no-cgroup", str(source),
                    ], capture_output=True, text=True, timeout=10)
                    self.assertEqual(process.returncode, exit_code, process.stdout + process.stderr)
                    expected = "编译失败（CE）" if verdict == "CE" else f"结果：{verdict}"
                    self.assertIn(expected, process.stdout)

            source.write_text(cases[0][0])
            process = subprocess.run([
                sys.executable, str(Path(__file__).with_name("judge.py")),
                "--pid", "1000", "--testdata", str(root / "testData"), "--checker", "none",
                "--cgroup-root", str(root / "missing-cgroup"), "--no-delegate", str(source),
            ], capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertIn("执行降级模式", process.stdout)
            self.assertIn("结果：AC", process.stdout)


class LimitsTests(unittest.TestCase):
    def test_protection_limits_include_margin(self):
        limits = Limits()
        self.assertEqual(limits.helper_args(Path("/group/cgroup.procs"))[0], "2")
        self.assertEqual(limits.memory_max_bytes(), 144 * 1024 * 1024)
        self.assertEqual(Limits(time_ms=0).helper_args(Path("/group/cgroup.procs"))[0], "0")
        self.assertEqual(Limits(memory_kb=0).memory_max_bytes(), 0)

    def test_make_protection_limits_is_the_single_calculation_point(self):
        p = make_protection_limits(Limits())
        self.assertIsInstance(p, ProtectionLimits)
        # CPU 阈值 1000ms + 200ms 余量，向上取整到秒。
        self.assertEqual(p.cpu_seconds, 2)
        self.assertEqual(p.memory_max_bytes, 144 * 1024 * 1024)
        self.assertEqual(p.stack_bytes, 64 * 1024 * 1024)
        self.assertEqual(p.output_bytes, 64 * 1024 * 1024)
        # 0 关闭对应限制。
        self.assertEqual(make_protection_limits(Limits(time_ms=0)).cpu_seconds, 0)
        self.assertEqual(make_protection_limits(Limits(memory_kb=0)).memory_max_bytes, 0)
        # 溢出要报错，而不是把一个荒谬的值传给 executor。
        with self.assertRaises(ValueError):
            make_protection_limits(Limits(time_ms=2**64))

    def test_microseconds_avoid_rounding_at_cpu_boundary(self):
        for cpu_us, expected in ((1000000, Verdict.OK), (1000001, Verdict.TLE)):
            report = ExecutionReport(cpu_time_us=cpu_us, cpu_time_ms=1000,
                                     real_time_ms=1000, rss_kb=0,
                                     timed_out=False, signal=0, exit_code=0)
            verdict, _ = classify_execution(report, None, Limits())
            self.assertEqual(verdict, expected)

    def test_memory_uses_exact_bytes_and_oom_evidence(self):
        limit = 128 * 1024 * 1024
        ok = ExecutionReport(cpu_time_us=0, cpu_time_ms=0, real_time_ms=0,
                             rss_kb=0, timed_out=False, signal=0, exit_code=0)
        for peak, expected in ((limit, Verdict.OK), (limit + 1, Verdict.MLE)):
            verdict, _ = classify_execution(
                ok, MemoryResult(peak_bytes=peak), Limits())
            self.assertEqual(verdict, expected)
        # OOM 事件优先于一切其他证据，即使同时有 SIGKILL。
        killed = ExecutionReport(cpu_time_us=0, cpu_time_ms=0, real_time_ms=0,
                                 rss_kb=0, timed_out=False, signal=signal.SIGKILL,
                                 exit_code=0)
        verdict, _ = classify_execution(
            killed, MemoryResult(peak_bytes=0, oom_events=1), Limits())
        self.assertEqual(verdict, Verdict.MLE)

    def _report(self, **overrides):
        base = dict(cpu_time_us=0, cpu_time_ms=0, real_time_ms=0, rss_kb=0,
                    timed_out=False, signal=0, exit_code=0)
        base.update(overrides)
        return ExecutionReport(**base)

    def test_verdict_priority_table(self):
        """混合证据的优先级：OOM → wall → 峰值 → CPU/SIGXCPU → 信号 → 退出码。"""
        limit = Limits()
        cases = [
            # (说明, report, memory, 期望)
            ("CPU 恰好等于阈值", self._report(cpu_time_us=1000 * 1000), None, Verdict.OK),
            ("CPU 超 1 微秒", self._report(cpu_time_us=1000 * 1000 + 1), None, Verdict.TLE),
            ("峰值恰好等于阈值", self._report(), MemoryResult(128 * 1024 * 1024), Verdict.OK),
            ("峰值超 1 字节", self._report(), MemoryResult(128 * 1024 * 1024 + 1), Verdict.MLE),
            ("OOM 与 wall 同时", self._report(timed_out=True),
             MemoryResult(0, oom_kills=1), Verdict.MLE),
            ("wall 与峰值超限、无 OOM", self._report(timed_out=True),
             MemoryResult(200 * 1024 * 1024), Verdict.TLE),
            ("SIGKILL 无其他证据", self._report(signal=signal.SIGKILL), None, Verdict.RE),
            ("SIGXCPU", self._report(signal=signal.SIGXCPU), None, Verdict.TLE),
            ("无 cgroup 时 RSS 很大不判 MLE",
             self._report(rss_kb=10 * 1024 * 1024), None, Verdict.OK),
            ("非零退出码", self._report(exit_code=3), None, Verdict.RE),
        ]
        for name, report, memory, expected in cases:
            with self.subTest(name):
                verdict, _ = classify_execution(report, memory, limit)
                self.assertEqual(verdict, expected)

    def test_zero_limits_disable_judgement(self):
        # 时间/内存阈值为 0 时，即使用量很大也不因此判超限。
        report = self._report(cpu_time_us=10**9)
        verdict, _ = classify_execution(
            report, MemoryResult(10**12), Limits(time_ms=0, memory_kb=0))
        self.assertEqual(verdict, Verdict.OK)


class BundledTestDataTests(unittest.TestCase):
    """仓库自带 testData/1000、1005 作为默认示例数据，要能从包外找到。"""

    def chdir_elsewhere(self):
        """切到一个没有 testData/ 的空目录，并在结束时恢复。"""
        previous = os.getcwd()
        directory = tempfile.TemporaryDirectory(prefix="local-judge-cwd-")
        try:
            os.chdir(directory.name)
            yield Path(directory.name)
        finally:
            os.chdir(previous)
            directory.cleanup()

    def test_bundled_testdata_found_from_other_directory(self):
        # 装到 ~/.local/share 后并没有包上级的 testData/，必须能回退到包内自带的那份。
        for _ in self.chdir_elsewhere():
            root, tried = judge.resolve_testdata(None)
            self.assertIsNotNone(root, f"未找到包内自带测试数据：{tried}")
            self.assertEqual(root, judge.PACKAGE_DIR / "testData")
            self.assertTrue((root / "1000" / "data").is_dir())
            self.assertTrue((root / "1005" / "data").is_dir())

    def test_own_testdata_shadows_bundled(self):
        # 用户自己项目里的 testData/ 优先，包内示例题不能把它顶掉。
        for directory in self.chdir_elsewhere():
            own = directory / "testData" / "2000" / "data"
            own.mkdir(parents=True)
            (own / "problem1.in").write_text("1\n")
            (own / "problem1.out").write_text("1\n")
            root, _ = judge.resolve_testdata(None)
            self.assertEqual(root, directory / "testData")


class DelegationModeTests(unittest.TestCase):
    """root 与普通用户的自动委派方式不同：前者走系统管理器，后者走用户管理器。"""

    def test_user_gets_user_manager(self):
        with patch.object(judge.os, "geteuid", return_value=1000):
            self.assertEqual(judge._delegate_mode_args(), ["--user"])
            prefix = judge._delegated_prefix()
        self.assertEqual(prefix[:4], ["systemd-run", "--user", "--quiet", "--scope"])
        # 委派不再经过 examples/delegated.py，而是让本 CLI 自己带 --in-scope 进场。
        self.assertEqual(prefix[-2:], ["--in-scope", "--"])
        self.assertTrue(prefix[-3].endswith("judge.py"))
        self.assertNotIn("delegated.py", prefix)

    def test_delegated_command_appends_no_delegate_after_args(self):
        # --no-delegate 是 judge.py 自己的参数，必须紧跟在原参数之后、
        # 仍在 `--` 之后（它属于要执行的命令里那一个 CLI 调用）。
        command = judge._delegated_command(["--pid", "1000", "sum.cpp"])
        # 取最后一个 `--` 之后的部分：第一个是 systemd-run 自己的分隔符。
        payload = command[command.index("--", command.index("--") + 1) + 1:]
        self.assertEqual(payload[-1], "--no-delegate")
        self.assertEqual(payload[-4:], ["--pid", "1000", "sum.cpp", "--no-delegate"])
        self.assertEqual(payload[0], sys.executable)
        self.assertTrue(payload[1].endswith("judge.py"))

    def test_in_scope_requires_payload_after_separator(self):
        # `--in-scope` 之后必须跟 `--` 和要执行的命令，否则是内部调用出错。
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(judge.run_in_scope(["--in-scope"]), 2)
            self.assertEqual(judge.run_in_scope(["--in-scope", "--"]), 2)
        self.assertIn("--in-scope", err.getvalue())

    def test_root_uses_system_manager(self):
        # sudo 会清掉 XDG_RUNTIME_DIR，root 又没有用户管理器，所以必须换成系统管理器。
        with patch.object(judge.os, "geteuid", return_value=0):
            self.assertEqual(judge._delegate_mode_args(), [])
            prefix = judge._delegated_prefix()
        self.assertEqual(prefix[:3], ["systemd-run", "--quiet", "--scope"])
        self.assertNotIn("--user", prefix)

    def test_blocker_ignores_missing_runtime_dir_for_root(self):
        with patch.object(judge.shutil, "which", return_value="/usr/bin/systemd-run"):
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(judge.os, "geteuid", return_value=1000):
                self.assertEqual(judge.delegation_blocker(), "缺少 XDG_RUNTIME_DIR")
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(judge.os, "geteuid", return_value=0):
                self.assertEqual(judge.delegation_blocker(), "")

    def test_blocker_without_systemd_run(self):
        with patch.object(judge.shutil, "which", return_value=None):
            self.assertEqual(judge.delegation_blocker(), "未找到 systemd-run")


if __name__ == "__main__":
    unittest.main()
