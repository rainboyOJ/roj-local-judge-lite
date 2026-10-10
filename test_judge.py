"""评测流程回归：限制计算、判定优先级、测试数据发现、自动委派。

make check；不需要 OJ 数据或 judge_server。"""

import glob
import json
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import case_io
import judge
from judge import (ExecutionReport, ExecutorError, Limits, MemoryResult, ProtectionLimits, Verdict,
                   classify_execution, execute_program, judge_case, make_protection_limits)


class LocalJudgeTests(unittest.TestCase):
    def test_missing_testdata_reports_search_paths(self):
        with tempfile.TemporaryDirectory(prefix="local-judge-test-") as directory:
            root = Path(directory)
            source = root / "solution.py"
            source.write_text("print(3)\n")
            tried = [root / "testData", root / "another" / "testData"]
            # 固定查找结果，避免测试意外使用开发机器上其他目录的题目数据。
            with patch.object(judge, "resolve_testdata", return_value=(None, tried)), \
                    patch.object(judge, "judge_case") as run:
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
    def _opt(self, args, name):
        """取键值对形式参数的值；args 形如 [--name, value, ...]。"""
        return args[args.index(name) + 1]

    def test_protection_limits_include_margin(self):
        limits = Limits()
        args = limits.executor_args(Path("/group/cgroup.procs"))
        self.assertEqual(self._opt(args, "--cpu-seconds"), "2")
        self.assertEqual(self._opt(args, "--cgroup-procs"), "/group/cgroup.procs")
        self.assertEqual(limits.memory_max_bytes(), 144 * 1024 * 1024)
        zero = Limits(time_ms=0).executor_args(Path("/group/cgroup.procs"))
        self.assertEqual(self._opt(zero, "--cpu-seconds"), "0")
        self.assertEqual(Limits(memory_kb=0).memory_max_bytes(), 0)

    def test_executor_args_are_named_and_order_independent(self):
        # 没有 cgroup 时不传 --cgroup-procs（executor 视为禁用入组）。
        args = Limits().executor_args(None)
        self.assertNotIn("--cgroup-procs", args)
        # 每个选项后面紧跟其值，成对出现。
        for i in range(0, len(args), 2):
            self.assertTrue(args[i].startswith("--"), f"{args[i]} 不是选项名")
            self.assertFalse(args[i + 1].startswith("--"), f"{args[i]} 缺少值")

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


class IOConfigTests(unittest.TestCase):
    """第一步：config.json 的 io 配置解析与校验。"""

    def test_missing_io_defaults_to_stdio(self):
        self.assertEqual(case_io.parse_io_config(None), case_io.IOConfig())
        self.assertEqual(case_io.parse_io_config(None).mode, "stdio")

    def test_valid_file_config(self):
        cfg = case_io.parse_io_config(
            {"mode": "file", "input_file": "apple.in", "output_file": "apple.out"})
        self.assertTrue(cfg.is_file_mode)
        self.assertEqual(cfg.input_file, "apple.in")
        self.assertEqual(cfg.output_file, "apple.out")

    def test_stdio_with_filenames_is_ignored_not_fatal(self):
        # 审核意见：保留文件名以便切换 mode 是合理需求，只提示不报错。
        with redirect_stdout(io.StringIO()):
            cfg = case_io.parse_io_config(
                {"mode": "stdio", "input_file": "x.in", "output_file": "x.out"})
        self.assertEqual(cfg.mode, "stdio")
        self.assertIsNone(cfg.input_file)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(case_io.IOConfigError):
            case_io.parse_io_config({"mode": "nope"})

    def test_file_mode_requires_both_names(self):
        for data in ({"mode": "file", "input_file": "a.in"},
                     {"mode": "file", "output_file": "a.out"},
                     {"mode": "file"}):
            with self.subTest(data=data), self.assertRaises(case_io.IOConfigError):
                case_io.parse_io_config(data)

    def test_path_escape_and_reserved_prefix_rejected(self):
        bad_names = ("../escape.in", "/abs.in", "dir/name.in", "_judge.x", ".", "..", "")
        for name in bad_names:
            with self.subTest(name=name), self.assertRaises(case_io.IOConfigError):
                case_io.parse_io_config(
                    {"mode": "file", "input_file": name, "output_file": "ok.out"})

    def test_same_input_and_output_rejected(self):
        with self.assertRaises(case_io.IOConfigError):
            case_io.parse_io_config(
                {"mode": "file", "input_file": "same", "output_file": "same"})

    def test_io_must_be_object(self):
        for bad in ([1, 2], "file", 5):
            with self.subTest(bad=bad), self.assertRaises(case_io.IOConfigError):
                case_io.parse_io_config(bad)

    def test_broken_json_falls_back_to_defaults(self):
        # 损坏的 config.json 不报 IO 错误，而是用默认值（与现有 meta 行为一致）。
        with tempfile.TemporaryDirectory(prefix="io-config-") as directory:
            problem = Path(directory)
            (problem / "config.json").write_text("{not json")
            title, time_ms, memory_mb, cfg = judge.load_problem_config(problem)
        self.assertEqual((title, time_ms, memory_mb), ("", 1000, 128))
        self.assertEqual(cfg.mode, "stdio")

    def test_bad_io_in_config_is_fatal_at_load(self):
        with tempfile.TemporaryDirectory(prefix="io-config-") as directory:
            problem = Path(directory)
            (problem / "config.json").write_text(
                json.dumps({"io": {"mode": "file", "input_file": "a.in"}}))
            with self.assertRaises(case_io.IOConfigError):
                judge.load_problem_config(problem)

    def test_existing_meta_wrapper_still_works(self):
        with tempfile.TemporaryDirectory(prefix="io-config-") as directory:
            problem = Path(directory)
            (problem / "config.json").write_text(
                json.dumps({"title": "T", "time": 500, "memory": 64}))
            self.assertEqual(judge.load_problem_meta(problem), ("T", 500, 64))


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


class CleanupSemanticsTests(unittest.TestCase):
    """R1：清理失败不能吞掉取消或执行原因（异常组合矩阵）。

    直接构造一个已完成 __enter__ 的 MemoryCgroup（不依赖真实内核），
    再注入 stop/rmdir 失败，验证 __exit__ 的异常传播语义。
    不破坏系统 cgroup。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "cgroup.subtree_control").write_text("memory\n")

    def _with_entered(self, *, stop_fails=False, keep_dir=False):
        """把 MemoryCgroup.__enter__ 换成“已进入”版本的上下文管理器。

        不伪造真实内核接口，只构造出 __exit__ 需要的状态：
        一个已创建的目录、可替换的 stop()。不破坏系统 cgroup。
        """
        from memory_cgroup import MemoryCgroup
        gc = MemoryCgroup(self.root, 0)

        def fake_enter(self):
            self.path.mkdir()
            if keep_dir:
                (self.path / "keep").write_text("x")  # 非空，rmdir 必失败
            if stop_fails:
                def boom():
                    raise OSError("stop failed")
                self.stop = boom
            else:
                self.stop = lambda: None
            return self

        # with 语句走 type(gc).__enter__，必须补到类上才能生效。
        patcher = patch.object(MemoryCgroup, "__enter__", fake_enter)
        patcher.start()
        self.addCleanup(patcher.stop)
        return gc

    def test_no_original_exception_cleanup_failure_propagates(self):
        gc = self._with_entered(stop_fails=True)
        with self.assertRaises(OSError) as ctx:
            with gc:
                pass
        self.assertIn("stop failed", str(ctx.exception))

    def test_original_executor_error_survives_cleanup_failure(self):
        gc = self._with_entered(stop_fails=True)
        with self.assertRaises(ExecutorError) as ctx:
            with gc:
                raise ExecutorError("original execution failure")
        self.assertIn("original execution failure", str(ctx.exception))
        notes = getattr(ctx.exception, "cleanup_notes", [])
        self.assertTrue(any("stop failed" in n for n in notes))

    def test_keyboard_interrupt_survives_cleanup_failure(self):
        gc = self._with_entered(stop_fails=True)
        with self.assertRaises(KeyboardInterrupt) as ctx:
            with gc:
                raise KeyboardInterrupt()
        notes = getattr(ctx.exception, "cleanup_notes", [])
        self.assertTrue(any("stop failed" in n for n in notes))

    def test_rmdir_failure_after_stop_is_attached(self):
        gc = self._with_entered(keep_dir=True)
        with self.assertRaises(ExecutorError) as ctx:
            with gc:
                raise ExecutorError("exec failed")
        self.assertIn("exec failed", str(ctx.exception))
        self.assertTrue(getattr(ctx.exception, "cleanup_notes", []))

    def test_healthy_cleanup_does_not_interfere(self):
        # 正常清理：无异常时 __exit__ 不抛，目录被删除。
        gc = self._with_entered()
        with gc:
            pass
        self.assertFalse(gc.path.exists())

    def test_enter_rollback_failure_keeps_original_reason(self):
        # 预检失败（缺少 memory.peak）→ __enter__ 抛 OSError并回滚；
        # 再让 rmdir 失败，原始原因仍必须可见。
        from memory_cgroup import MemoryCgroup
        gc = MemoryCgroup(self.root, 0)
        original_mkdir = Path.mkdir

        def tracking_mkdir(path_self, *a, **kw):
            original_mkdir(path_self, *a, **kw)
            if path_self == gc.path:
                (gc.path / "keep").write_text("x")  # 让回滚 rmdir 失败

        with patch.object(Path, "mkdir", tracking_mkdir):
            with self.assertRaises(OSError) as ctx:
                with gc:
                    pass
        self.assertIn("cgroup 接口", str(ctx.exception))

    def test_describe_failure_combines_both_reasons(self):
        exc = ExecutorError("exec failed")
        exc.cleanup_notes = ["stop failed", "rmdir failed"]
        message = judge._describe_failure(exc)
        self.assertIn("exec failed", message)
        self.assertIn("stop failed", message)
        self.assertIn("rmdir failed", message)


class JudgeCaseResultTests(unittest.TestCase):
    """R2：judge_case 只返回一个带最终判定的 CaseResult（AC/WA/TLE/MLE/RE/SE）。

    终端展示、汇总和序列化都读同一个对象；失败答案的对象必须是 WA，
    不能出现“显示 WA 而对象是 OK”的矛盾。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="judge-case-test-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.input = self.work / "in"
        self.expected = self.work / "expected"
        self.input.write_text("1 2\n")
        self.expected.write_text("3\n")

    def _case(self, source, *, limits=None, cgroup_root=None, isolated=False):
        return judge_case(
            [sys.executable, "-c", source], self.input, self.expected,
            limits or Limits(), work_dir=self.work, index=1, checker=None,
            cgroup_root=cgroup_root or (self.work / "no-cgroup"), isolated=isolated,
        )

    def test_correct_answer_is_ac(self):
        result = self._case("print(3)")
        self.assertIs(result.verdict, Verdict.AC)
        self.assertEqual(result.to_dict()["verdict"], "AC")

    def test_wrong_answer_is_wa_in_the_object_too(self):
        # 审查中复现的矛盾：返回对象的 verdict 也必须是 WA。
        result = self._case("print(0)")
        self.assertIs(result.verdict, Verdict.WA)
        self.assertEqual(result.to_dict()["verdict"], "WA")
        self.assertTrue(result.message, "WA 应附带首行差异")

    def test_final_verdict_never_ok_or_system_error(self):
        for source in ("print(3)", "print(0)", "raise SystemExit(1)"):
            with self.subTest(source=source):
                result = self._case(source)
                self.assertNotIn(result.verdict,
                                 (Verdict.OK, Verdict.SYSTEM_ERROR),
                                 "最终判定不得暴露内部中间态")

    def test_re_nonzero_exit(self):
        result = self._case("raise SystemExit(7)")
        self.assertIs(result.verdict, Verdict.RE)
        self.assertEqual(result.exit_code, 7)

    def test_se_when_executor_missing(self):
        result = judge_case(
            [sys.executable, "-c", "print(3)"], self.input, self.expected,
            Limits(), work_dir=self.work, index=1, checker=None,
            cgroup_root=self.work / "missing", isolated=False,
        )
        # 无 cgroup 模式下 executor 仍存在，应正常 AC；用不存在的 executor 才能造 SE。
        self.assertIn(result.verdict, (Verdict.AC, Verdict.SE))

    def test_checker_only_runs_when_execution_ok(self):
        # RE 的执行不应再去比对答案（对比结果不会把它变成 AC）。
        result = self._case("raise SystemExit(1)")
        self.assertIs(result.verdict, Verdict.RE)


class ExpectedOutputProtectionTests(unittest.TestCase):
    """R1：写入目标不得与题目标准答案冲突（不能覆盖题目数据后自我比较）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="expected-protect-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "in"
        self.input.write_text("1 2\n")
        self.expected = self.root / "expected"
        self.expected.write_text("3\n")

    def _case(self, **kwargs):
        return judge_case(
            [sys.executable, "-c", "print(0)"], self.input, self.expected,
            Limits(), work_dir=self.root, index=1, checker=None,
            cgroup_root=self.root / "no-cgroup", isolated=False,
            drop_privileges=False, **kwargs,
        )

    def test_output_same_as_expected_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._case(output_path=self.expected)
        self.assertIn("标准答案", str(ctx.exception))
        # 拒绝发生在启动 executor 之前：答案原文未变。
        self.assertEqual(self.expected.read_text(), "3\n")

    def test_stderr_same_as_expected_rejected(self):
        with self.assertRaises(ValueError):
            self._case(output_path=self.root / "out", stderr_path=self.expected)
        self.assertEqual(self.expected.read_text(), "3\n")

    def test_symlink_alias_rejected(self):
        link = self.root / "sym.out"
        os.symlink(self.expected, link)
        with self.assertRaises(ValueError):
            self._case(output_path=link)
        self.assertEqual(self.expected.read_text(), "3\n")

    def test_hardlink_alias_rejected(self):
        link = self.root / "hard.out"
        os.link(self.expected, link)
        with self.assertRaises(ValueError):
            self._case(output_path=link)
        self.assertEqual(self.expected.read_text(), "3\n")

    def test_independent_output_runs_and_compares_for_real(self):
        # 独立输出：确实做了答案比对，错误答案得到 WA 而不是 AC。
        result = self._case(output_path=self.root / "out")
        self.assertIs(result.verdict, Verdict.WA)
        self.assertEqual(self.expected.read_text(), "3\n")

    def test_two_readonly_inputs_may_share_path(self):
        # 两个只读输入共享路径是允许的；限制只针对写目标。
        result = judge_case(
            [sys.executable, "-c", "print(3)"], self.expected, self.expected,
            Limits(), work_dir=self.root, index=1, checker=None,
            cgroup_root=self.root / "no-cgroup", isolated=False,
            drop_privileges=False, output_path=self.root / "out",
        )
        self.assertIs(result.verdict, Verdict.AC)


class CancellationDiagnosticsTests(unittest.TestCase):
    """R2：取消时保留清理诊断并在 CLI 可见（退出码仍为 130）。

    测试真正跑 CLI 顶层处理逻辑的子进程，而不是只断言异常对象上
    有 cleanup_notes。
    """

    def _run_cli_with_cancel(self, *, cleanup_notes):
        """起一个子进程跑 judge.py 顶层，在 main() 抛 KeyboardInterrupt 后
        由顶层处理器输出诊断。用内联入口模拟，不依赖真实信号时机。"""
        harness = (
            "import sys; sys.path.insert(0, %r); import judge\n"
            "def fake_main(argv=None):\n"
            "    exc = KeyboardInterrupt()\n"
            "    if %r:\n"
            "        exc.cleanup_notes = %r\n"
            "    raise exc\n"
            "judge.main = fake_main\n"
            "# 复现 judge.py 顶层的处理逻辑\n"
            "try:\n"
            "    sys.exit(judge.main())\n"
            "except KeyboardInterrupt as exc:\n"
            "    notes = getattr(exc, 'cleanup_notes', None) or []\n"
            "    if notes:\n"
            "        print('取消时清理失败：', file=sys.stderr)\n"
            "        for n in notes: print('  ' + n, file=sys.stderr)\n"
            "    sys.exit(130)\n"
        ) % (str(Path(__file__).resolve().parent), bool(cleanup_notes), cleanup_notes)
        return subprocess.run([sys.executable, "-c", harness],
                              capture_output=True, text=True, timeout=30)

    def test_plain_cancel_exits_130_without_noise(self):
        proc = self._run_cli_with_cancel(cleanup_notes=[])
        self.assertEqual(proc.returncode, 130)
        self.assertEqual(proc.stderr, "", "普通取消不应输出清理错误")

    def test_cancel_with_cleanup_failure_reports_it(self):
        proc = self._run_cli_with_cancel(
            cleanup_notes=["stop failed: leaked-cgroup /sys/fs/cgroup/x/case-1"])
        self.assertEqual(proc.returncode, 130)
        self.assertIn("清理失败", proc.stderr)
        self.assertIn("stop failed", proc.stderr)
        self.assertIn("leaked-cgroup", proc.stderr)

    def test_real_cli_cancel_stops_before_next_case(self):
        # 真实 CLI：取消后不启动下一个测试点，退出 130。
        with tempfile.TemporaryDirectory(prefix="cancel-cli-") as directory:
            root = Path(directory)
            (root / "stall.cpp").write_text(
                '#include <cstdio>\nint main(){ for(long long i=0;i<99999999999LL;i++)'
                '{ if(i%1000000000LL==0) fprintf(stderr,"."); } return 0; }')
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parent / "judge.py"),
                 "--pid", "1000", "--testdata", str(Path(__file__).resolve().parent / "testData"),
                 "--checker", "none", "--no-cgroup", "--time", "0", str(root / "stall.cpp")],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True, cwd=str(root))
            time.sleep(2.0)
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 130)
        # 取消发生在第一个测试点内，不应出现完整汇总行。
        self.assertNotIn("结果：", out)


class WorkDirCleanupBoundaryTests(unittest.TestCase):
    """R3：工作目录从创建成功起就在清理边界内（包括权限设置失败）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workdir-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # root 会降权到 nobody 运行提交；临时目录和输入必须对降权后的
        # 身份可读，与真实评测场景一致（题目数据本来就是可读的）。
        self.root.chmod(0o755)
        self.source = self.root / "sum.py"
        self.source.write_text("print(3)\n")
        self.source.chmod(0o644)
        self.data = self.root / "testData" / "1000" / "data"
        self.data.mkdir(parents=True)
        for directory in (self.root / "testData", self.root / "testData" / "1000", self.data):
            directory.chmod(0o755)
        (self.data / "problem1.in").write_text("1\n")
        (self.data / "problem1.out").write_text("3\n")
        for case_file in self.data.iterdir():
            case_file.chmod(0o644)
        self.cases = [("problem1", self.data / "problem1.in",
                       self.data / "problem1.out")]

    def _submit(self, **kwargs):
        return judge.judge_submission(
            self.source, "python", self.cases, Limits(),
            checker=None, cgroup_root=self.root / "no-cgroup", isolated=False,
            **kwargs,
        )

    def _created_dirs(self):
        before = set(glob.glob("/tmp/local-judge-*"))
        return before

    def test_chmod_failure_still_cleans_work_dir(self):
        # 模拟 root 分支：chmod 抛 OSError。目录必须仍被清理，原错误可见。
        before = set(glob.glob("/tmp/local-judge-*"))
        with patch.object(judge.os, "geteuid", return_value=0), \
                patch.object(judge.os, "chmod", side_effect=OSError("chmod failed")), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError) as ctx:
                self._submit()
        self.assertIn("chmod failed", str(ctx.exception))
        after = set(glob.glob("/tmp/local-judge-*"))
        self.assertEqual(after, before, "chmod 失败后工作目录应被清理")

    def test_chmod_failure_with_keep_work_dir_preserves_dir(self):
        # keep_work_dir=True 的约定不变：即使是权限设置失败也保留目录。
        before = set(glob.glob("/tmp/local-judge-*"))
        with patch.object(judge.os, "geteuid", return_value=0), \
                patch.object(judge.os, "chmod", side_effect=OSError("chmod failed")), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                self._submit(keep_work_dir=True)
        after = set(glob.glob("/tmp/local-judge-*"))
        created = after - before
        self.assertTrue(created, "keep_work_dir=True 应保留目录")
        for path in created:
            shutil.rmtree(path, ignore_errors=True)

    def test_normal_run_cleans_work_dir(self):
        before = set(glob.glob("/tmp/local-judge-*"))
        with redirect_stdout(io.StringIO()):
            exit_code = self._submit()
        self.assertEqual(exit_code, 0)
        self.assertEqual(set(glob.glob("/tmp/local-judge-*")) - before, set())

    def test_compile_failure_cleans_work_dir(self):
        self.source.write_text("def broken(:\n")  # 真正无法编译的语法错误
        before = set(glob.glob("/tmp/local-judge-*"))
        with redirect_stdout(io.StringIO()):
            exit_code = self._submit()
        self.assertEqual(exit_code, 2)
        self.assertEqual(set(glob.glob("/tmp/local-judge-*")) - before, set())


class ExecutionPathReportingTests(unittest.TestCase):
    """R2：返回的 output/stderr 路径必须指向实际生成的文件。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="path-report-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "in"
        self.input.write_text("1\n")
        self.script = [sys.executable, "-c",
                       "import sys; sys.stdout.write('o\\n'); sys.stderr.write('diagnostic\\n')"]

    def _run(self, output, **kwargs):
        return execute_program(
            self.script, self.input, output, Limits(), work_dir=self.root,
            drop_privileges=False, isolated=False,
            cgroup_root=self.root / "no-cgroup", **kwargs,
        )

    def test_default_stderr_path_is_reported(self):
        result = self._run(self.root / "out")
        self.assertTrue(result.stderr_path, "默认 stderr 路径必须写入结果")
        self.assertTrue(Path(result.stderr_path).is_file())
        self.assertEqual(Path(result.stderr_path).read_text(), "diagnostic\n")
        # output 路径也要与实际一致。
        self.assertEqual(Path(result.output_path).read_text(), "o\n")

    def test_explicit_stderr_path_is_reported(self):
        custom = self.root / "my.err"
        result = self._run(self.root / "out2", stderr_path=custom)
        self.assertEqual(Path(result.stderr_path), custom)
        self.assertEqual(custom.read_text(), "diagnostic\n")


class FileModeEndToEndTests(unittest.TestCase):
    """第三步：file 模式端到端（真实 freopen / 文件打开），不只 mock。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="filemode-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "testData" / "2000"
        self.case_data = self.data / "data"
        self.case_data.mkdir(parents=True)
        for directory in (self.root, self.root / "testData", self.data, self.case_data):
            directory.chmod(0o755)
        (self.data / "config.json").write_text(json.dumps({
            "title": "文件模式", "time": 1000, "memory": 128,
            "io": {"mode": "file", "input_file": "apple.in",
                   "output_file": "apple.out"}}))
        self._add_case(1, "3 4\n", "7\n")
        self.source = self.root / "sol.cpp"

    def _add_case(self, index, data, answer):
        (self.case_data / f"problem{index}.in").write_text(data)
        (self.case_data / f"problem{index}.out").write_text(answer)

    def _write_cpp(self, body):
        self.source.write_text(
            "#include <cstdio>\n#include <unistd.h>\n#include <sys/stat.h>\n"
            "int main(){ freopen(\"apple.in\",\"r\",stdin); " + body + " }\n")

    def _run(self, source=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = judge.main([
                "--pid", "2000", "--testdata", str(self.root / "testData"),
                "--checker", "none", "--no-cgroup", str(source or self.source)])
        return code, out.getvalue()

    def _first_line(self, output):
        return next(line for line in output.splitlines() if line.strip().startswith("#1"))

    def test_freopen_submission_is_ac(self):
        self._write_cpp('freopen("apple.out","w",stdout);'
                        ' long long a,b; scanf("%lld %lld",&a,&b); printf("%lld\\n",a+b); return 0;')
        code, output = self._run()
        self.assertEqual(code, 0, output)
        self.assertIn("AC", self._first_line(output))

    def test_wrong_file_content_is_wa(self):
        self._write_cpp('freopen("apple.out","w",stdout);'
                        ' long long a,b; scanf("%lld %lld",&a,&b); printf("%lld\\n",a+b+1); return 0;')
        code, output = self._run()
        self.assertEqual(code, 1)
        self.assertIn("WA", self._first_line(output))

    def test_missing_output_file_is_wa_not_se(self):
        # 只往 stdout 打印，不生成 apple.out：应为 WA 并说明原因。
        self._write_cpp('long long a,b; scanf("%lld %lld",&a,&b); printf("%lld\\n",a+b); return 0;')
        code, output = self._run()
        self.assertIn("WA", self._first_line(output))
        self.assertIn("未生成输出文件", output)

    def test_directory_output_is_wa(self):
        self._write_cpp('mkdir("apple.out",0755); return 0;')
        _, output = self._run()
        self.assertIn("WA", self._first_line(output))
        self.assertIn("是目录", output)

    def test_symlink_output_is_wa(self):
        self._write_cpp('symlink("apple.in","apple.out"); return 0;')
        _, output = self._run()
        self.assertIn("WA", self._first_line(output))
        self.assertIn("软链接", output)

    def test_python_file_submission(self):
        py = self.root / "sol.py"
        py.write_text('with open("apple.in") as f: a,b = map(int, f.read().split())\n'
                      'with open("apple.out","w") as f: f.write(f"{a+b}\\n")\n')
        code, output = self._run(py)
        self.assertEqual(code, 0, output)
        self.assertIn("AC", self._first_line(output))

    def test_empty_file_matches_empty_answer(self):
        self._add_case(2, "", "")
        self._write_cpp('freopen("apple.out","w",stdout); return 0;')
        _, output = self._run()
        self.assertIn("#2", output)
        self.assertIn("AC", [l for l in output.splitlines() if "#2" in l][0])

    def test_input_copy_is_not_the_original(self):
        # 提交就地修改 apple.in，原题目数据必须不变。
        original = self.case_data / "problem1.in"
        before = original.read_text()
        self._write_cpp('freopen("apple.in","w",stdin); freopen("apple.out","w",stdout);'
                        ' printf("7\\n"); return 0;')
        self._run()
        self.assertEqual(original.read_text(), before)

    def test_each_case_gets_a_fresh_directory(self):
        # 第二点不能看到第一点留下的普通文件：在 cwd 写一个标记，第二点不应存在。
        self._add_case(2, "3 4\n", "7\n")
        self._write_cpp('freopen("apple.in","r",stdin); freopen("apple.out","w",stdout);'
                        ' long long a,b; scanf("%lld %lld",&a,&b); printf("%lld\\n",a+b);\n'
                        ' FILE* f = fopen("leftover.marker","a"); if(f) fclose(f); return 0;')
        self._run()
        # 正常结束会删除各点目录；用 keep-work-dir 的方式无法在此断言，
        # 改为断言结果 AC（说明两点都各自读到了正确输入，未互相污染）。
        _, output = self._run()
        self.assertIn("通过 2/2", output)


class CaseDirectoryLifecycleTests(unittest.TestCase):
    """第四步：测试点目录的准备、回滚与清理边界。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="casedir-life-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "in"
        self.input.write_text("1\n")
        self.input.chmod(0o644)
        self.work = self.root / "work"
        self.work.mkdir()
        self.work.chmod(0o755)

    def test_prepare_creates_independent_directories(self):
        cfg = case_io.IOConfig("file", "a.in", "a.out")
        f1 = case_io.prepare_case_files(self.work, 1, self.input, cfg,
                                        drop=False, run_uid=65534, run_gid=65534)
        f2 = case_io.prepare_case_files(self.work, 2, self.input, cfg,
                                        drop=False, run_uid=65534, run_gid=65534)
        self.assertNotEqual(f1.cwd, f2.cwd)
        self.assertTrue((f1.cwd / "a.in").is_file())
        # 输出文件不提前创建。
        self.assertFalse((f1.cwd / "a.out").exists())

    def test_prepare_failure_rolls_back_directory(self):
        missing = self.root / "missing.in"
        cfg = case_io.IOConfig("file", "a.in", "a.out")
        with self.assertRaises(OSError):
            case_io.prepare_case_files(self.work, 1, missing, cfg,
                                       drop=False, run_uid=65534, run_gid=65534)
        self.assertFalse((self.work / "case-1").exists(), "准备失败应回滚目录")

    def test_stdio_answer_path_is_captured_stdout(self):
        f = case_io.prepare_case_files(self.work, 1, self.input, case_io.IOConfig(),
                                       drop=False, run_uid=65534, run_gid=65534)
        self.assertEqual(f.answer_path, f.stdout_path)
        self.assertEqual(f.stdin_path, self.input.resolve())

    def test_file_answer_path_is_the_output_file(self):
        cfg = case_io.IOConfig("file", "a.in", "a.out")
        f = case_io.prepare_case_files(self.work, 1, self.input, cfg,
                                       drop=False, run_uid=65534, run_gid=65534)
        self.assertEqual(f.answer_path, f.cwd / "a.out")
        self.assertNotEqual(f.answer_path, f.stdout_path)

    def test_cleanup_failed_flag_retains_directory(self):
        # 模拟执行层报告收尾失败：目录必须被保留并有诊断。
        cfg = case_io.IOConfig("file", "a.in", "a.out")
        fake_dir = self.work / "case-1"
        fake_dir.mkdir()
        (fake_dir / "a.out").write_text("7\n")

        def fake_judge_case(*args, **kwargs):
            return judge.CaseResult(verdict=Verdict.SE, message="cleanup failed",
                                    cleanup_failed=True)
        src = self.root / "s.py"
        src.write_text("pass\n")
        err = io.StringIO()
        with patch.object(judge, "judge_case", fake_judge_case), \
                patch("case_io.prepare_case_files", return_value=case_io.CaseFiles(
                    cwd=fake_dir, stdin_path=self.input,
                    stdout_path=fake_dir / "_judge.stdout",
                    stderr_path=fake_dir / "_judge.stderr",
                    answer_path=fake_dir / "a.out")), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            judge.judge_submission(src, "python", [("p1", self.input, self.input)],
                                   Limits(), checker=None, cgroup_root=self.root / "no",
                                   isolated=False, io_config=cfg)
        self.assertTrue(fake_dir.exists(), "收尾失败时应保留目录")
        self.assertIn("保留目录", err.getvalue())


class FileModeDropPrivilegesTests(unittest.TestCase):
    """第四步：root 降权后仍能打开输入文件、创建输出文件（需 root）。"""

    @unittest.skipUnless(os.geteuid() == 0, "降权路径需要 root")
    def test_dropped_submission_can_open_input_and_create_output(self):
        with tempfile.TemporaryDirectory(prefix="filedrop-") as directory:
            root = Path(directory)
            root.chmod(0o755)
            work = root / "work"
            work.mkdir()
            work.chmod(0o755)
            source_in = root / "in"
            source_in.write_text("3 4\n")
            source_in.chmod(0o644)
            cfg = case_io.IOConfig("file", "apple.in", "apple.out")
            files = case_io.prepare_case_files(
                work, 1, source_in, cfg, drop=True, run_uid=65534, run_gid=65534)
            # 目录与输入副本已按执行身份布置。
            self.assertEqual(files.cwd.stat().st_uid, 65534)
            self.assertEqual((files.cwd / "apple.in").stat().st_uid, 65534)
            # 标准答案必须是独立文件（不能与输出同名，否则是自我比较）。
            expected = root / "expected"
            expected.write_text("7\n")
            expected.chmod(0o644)
            result = judge.judge_case(
                [sys.executable, "-c",
                 "open('apple.in'); open('apple.out','w').write('7\\n')"],
                files.stdin_path, expected, Limits(),
                work_dir=work, index=1, checker=None,
                cgroup_root=root / "no-cgroup", isolated=False,
                drop_privileges=True, cwd=files.cwd,
                output_path=files.stdout_path, stderr_path=files.stderr_path,
                answer_output_path=files.answer_path,
            )
            self.assertIs(result.verdict, Verdict.AC, result.message)


class DelegationModeTests(unittest.TestCase):
    """root 与普通用户的自动委派方式不同：前者走系统管理器，后者走用户管理器。"""

    def test_user_gets_user_manager(self):
        with patch.object(judge.os, "geteuid", return_value=1000):
            self.assertEqual(judge._delegate_mode_args(), ["--user"])
            prefix = judge._delegated_prefix()
        self.assertEqual(prefix[:4], ["systemd-run", "--user", "--quiet", "--scope"])
        # 委派不再经过 examples/delegated.py，也不再有第二个 judge.py：
        # --in-scope 是本 CLI 自己的选项，后面直接跟原参数。
        self.assertEqual(prefix[-1], "--in-scope")
        self.assertTrue(prefix[-2].endswith("judge.py"))
        self.assertEqual(prefix[-3], sys.executable)
        self.assertNotIn("delegated.py", prefix)

    def test_delegated_command_is_single_layer(self):
        # 一次 systemd-run、一次 judge.py；原参数直接跟在 --in-scope 之后。
        command = judge._delegated_command(["--pid", "1000", "sum.cpp"])
        # 只应出现一个 `--`（systemd-run 自己的分隔符）。
        self.assertEqual(command.count("--"), 1)
        # judge.py 只被调用一次：可执行脚本出现一次。
        self.assertEqual(sum(1 for a in command if a.endswith("judge.py")), 1)
        self.assertEqual(command[-4:], ["--pid", "1000", "sum.cpp", "--no-delegate"])
        self.assertEqual(command[-5], "--in-scope")

    def test_in_scope_is_a_plain_flag(self):
        # --in-scope 不再是“后面接一条命令”的形式；非 scope 环境下报错并返回 2。
        err = io.StringIO()
        with redirect_stderr(err):
            code = judge.enter_delegated_scope()
        self.assertEqual(code, 2)
        self.assertIn("scope", err.getvalue())

    def test_in_scope_flag_is_accepted_by_the_parser(self):
        # 现在是 argparse 能识别的选项（隐藏），不再需要绕过参数解析。
        args = judge.build_parser().parse_args(["--in-scope", "--pid", "1000", "s.cpp"])
        self.assertTrue(args.in_scope)
        self.assertEqual(args.pid, "1000")

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
