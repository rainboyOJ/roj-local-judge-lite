"""资源执行器的行为回归测试：make check；不需要 OJ 数据或 judge_server。"""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from runner import CaseResult, Limits, Verdict, _set_verdict, run_case


class ExecutionTestsMixin:
    """同一组黑盒测试在两种模式各跑一次，防止执行行为再次分叉。"""

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="runner-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.output = self.root / "output"
        self.input.write_bytes(b"3 4\n")

    def run_case(self, *args, **kwargs):
        return run_case(*args, use_cgroup=self.use_cgroup, **kwargs)

    def cli_mode_args(self):
        return [] if self.use_cgroup else ["--no-cgroup"]

    def run_python(self, source, limits=None, **kwargs):
        return self.run_case(
            [sys.executable, "-c", source], self.input, self.output,
            limits or Limits(), drop_privileges=False, **kwargs,
        )

    def assert_stopped(self, pid):
        # 进程组中的孤儿由系统 reaper 回收，短时间内可处于 Z 状态，但不能继续运行。
        status = Path(f"/proc/{pid}/status")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = next(line for line in status.read_text().splitlines() if line.startswith("State:"))
            except FileNotFoundError:
                return
            if state.split()[1] == "Z":
                return
            time.sleep(0.01)
        self.fail(f"process {pid} is still running")

    def test_redirects_and_arguments(self):
        result = self.run_case(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); "
             "sys.stderr.write(sys.argv[1])", "literal $(not-a-shell)"],
            self.input, self.output, Limits(),
            stderr_path=self.root / "error", drop_privileges=False,
        )
        self.assertEqual(result.verdict, Verdict.OK, result.message)
        self.assertEqual(self.output.read_bytes(), self.input.read_bytes())
        self.assertEqual((self.root / "error").read_text(), "literal $(not-a-shell)")

    def test_relative_executable_uses_cwd(self):
        shutil.copyfile(shutil.which("true"), self.root / "solution")
        (self.root / "solution").chmod(0o755)
        result = self.run_case(["./solution"], self.input, self.output,
                               cwd=self.root, drop_privileges=False)
        self.assertEqual(result.verdict, Verdict.OK, result.message)

    def test_relative_path_entry_uses_submission_cwd(self):
        # PATH 查找已交给 execvp；相对 PATH 项仍应从提交的 cwd 开始查找。
        (self.root / "bin").mkdir()
        executable = self.root / "bin" / "solution"
        shutil.copyfile(shutil.which("true"), executable)
        executable.chmod(0o755)
        with patch.dict(os.environ, {"PATH": "bin"}):
            result = self.run_case(["solution"], self.input, self.output,
                                   cwd=self.root, drop_privileges=False)
        self.assertEqual(result.verdict, Verdict.OK, result.message)

    def test_cli_reports_json_and_keeps_program_output_separate(self):
        process = subprocess.run([
            sys.executable, str(Path(__file__).with_name("runner.py")),
            "--input", str(self.input), "--output", str(self.output),
            "--no-drop-privileges", *self.cli_mode_args(), "--", sys.executable, "-c", "print('hello')",
        ], capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["verdict"], "OK")
        if self.use_cgroup:
            self.assertGreater(report["memory_kb"], 0)
        else:
            self.assertEqual(report["memory_kb"], 0)
        self.assertEqual(self.output.read_text(), "hello\n")

    def test_wall_timeout(self):
        result = self.run_python("import time; time.sleep(10)",
                                 Limits(wall_time_ms=80))
        self.assertEqual(result.verdict, Verdict.TLE)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.signal, signal.SIGKILL)
        self.assertLess(result.real_time_ms, 2000)

    def test_wall_timeout_covers_setup_before_exec(self):
        # 没有写端的 FIFO 会让 helper 子进程阻塞在打开 stdin，而非用户代码。
        # 看门狗必须在 setup 阶段已经运行，不能等 exec 成功后才开始计时。
        self.input.unlink()
        os.mkfifo(self.input)
        result = self.run_python("pass", Limits(wall_time_ms=100))
        self.assertEqual(result.verdict, Verdict.TLE, result.message)
        self.assertTrue(result.timed_out)
        self.assertLess(result.real_time_ms, 2000)

    def test_resource_limits_reach_submission(self):
        result = self.run_python(
            "import json, resource; print(json.dumps([resource.getrlimit(r) for r in "
            "(resource.RLIMIT_STACK, resource.RLIMIT_FSIZE, resource.RLIMIT_NPROC, "
            "resource.RLIMIT_CORE)]))",
            Limits(stack_mb=32, output_limit_mb=1, nproc=4096),
        )
        self.assertEqual(result.verdict, Verdict.OK, result.message)
        self.assertEqual(json.loads(self.output.read_text()), [
            [32 * 1024 * 1024] * 2, [1024 * 1024] * 2, [4096] * 2, [0, 0],
        ])

    def test_cpu_timeout(self):
        result = self.run_python("while True: pass",
                                 Limits(time_ms=1000, wall_time_ms=5000))
        self.assertEqual(result.verdict, Verdict.TLE)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.signal, signal.SIGXCPU)

    def test_cpu_margin_allows_finish_before_judging(self):
        result = self.run_python(
            "import time\nend = time.process_time() + 1.1\n"
            "while time.process_time() < end: pass\n",
            Limits(time_ms=1000, wall_time_ms=5000),
        )
        self.assertEqual(result.verdict, Verdict.TLE)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.signal, 0)
        self.assertFalse(result.timed_out)
        self.assertGreater(result.cpu_time_us, 1000000)

    def test_output_file_limit(self):
        result = self.run_python(
            "import os, sys, errno\ntry:\n"
            " while True: os.write(1, b'x' * 65536)\n"
            "except OSError as e: sys.exit(23 if e.errno == errno.EFBIG else 24)\n",
            Limits(output_limit_mb=1),
        )
        self.assertEqual(result.verdict, Verdict.RE)
        self.assertEqual(result.exit_code, 23)
        self.assertEqual(self.output.stat().st_size, 1024 * 1024)

    def test_missing_input_is_setup_error(self):
        self.input.unlink()
        result = self.run_python("pass")
        self.assertEqual(result.verdict, Verdict.SYSTEM_ERROR)
        self.assertIn("redirect stdin", result.message)

    def test_failed_exec_is_setup_error(self):
        result = self.run_case([str(self.root / "missing")], self.input, self.output,
                               drop_privileges=False)
        self.assertEqual(result.verdict, Verdict.SYSTEM_ERROR)
        self.assertIn("exec", result.message)

    def test_user_exit_codes_and_stderr_are_not_setup_or_mle(self):
        for code in (1, 126, 127):
            with self.subTest(code=code):
                result = self.run_python(f"import sys; sys.stderr.write('MemoryError'); sys.exit({code})")
                self.assertEqual(result.verdict, Verdict.RE)
                self.assertEqual(result.exit_code, code)

    def test_signals_are_reported_without_guessing_mle(self):
        for signo in (signal.SIGSEGV, signal.SIGKILL):
            with self.subTest(signal=signo):
                result = self.run_python(f"import os; os.kill(os.getpid(), {int(signo)})")
                self.assertEqual(result.verdict, Verdict.RE)
                self.assertEqual(result.signal, signo)

    def test_missing_helper_is_system_error(self):
        result = self.run_python("pass", helper_path=self.root / "no-helper")
        self.assertEqual(result.verdict, Verdict.SYSTEM_ERROR)
        self.assertIn("make", result.message)

    def test_timeout_kills_descendants(self):
        pidfile = self.root / "grandchild.pid"
        result = self.run_python(
            f"import os, time\np = os.fork()\n"
            f"if p: open({str(pidfile)!r}, 'w').write(str(p))\n"
            "time.sleep(10)\n", Limits(wall_time_ms=250),
        )
        self.assertEqual(result.verdict, Verdict.TLE)
        self.assert_stopped(int(pidfile.read_text()))

    def test_normal_exit_kills_background_descendants(self):
        pidfile = self.root / "background.pid"
        result = self.run_python(
            "import os, time\np = os.fork()\n"
            f"if p: open({str(pidfile)!r}, 'w').write(str(p))\n"
            "else: time.sleep(10)\n",
        )
        self.assertEqual(result.verdict, Verdict.OK, result.message)
        self.assert_stopped(int(pidfile.read_text()))

    def test_interrupt_cleans_up_submission_group(self):
        pidfile = self.root / "pids"
        source = (
            "import os, time\np = os.fork()\n"
            f"if p: open({str(pidfile)!r}, 'w').write(str(os.getpid()) + ' ' + str(p))\n"
            "time.sleep(10)\n"
        )
        process = subprocess.Popen([
            sys.executable, str(Path(__file__).with_name("runner.py")),
            "--input", str(self.input), "--output", str(self.output),
            "--time", "0", "--no-drop-privileges", *self.cli_mode_args(), "--",
            sys.executable, "-c", source,
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pidfile.exists())
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130)
            for pid in pidfile.read_text().split():
                self.assert_stopped(int(pid))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_zero_time_disables_automatic_wall_limit(self):
        self.assertEqual(Limits(time_ms=0).resolved_wall_ms(), 0)
        self.assertEqual(Limits(time_ms=0, wall_time_ms=30).resolved_wall_ms(), 30)

    def test_invalid_limits_and_stream_aliases(self):
        for limits in (Limits(time_ms=-1), Limits(cpu_slack_ms=-1), Limits(memory_slack_kb=-1)):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.run_python("pass", limits)
        os.link(self.input, self.output)
        with self.assertRaises(ValueError):
            self.run_python("pass")
        self.assertEqual(self.input.read_bytes(), b"3 4\n")

    @unittest.skipUnless(os.geteuid() == 0, "降权路径需要 root")
    def test_drop_privileges(self):
        self.root.chmod(0o755)
        result = self.run_case(
            [sys.executable, "-c", "import os; print(os.getuid(), os.getgid(), os.getgroups())"],
            self.input, self.output, Limits(), cwd=self.root,
        )
        self.assertEqual(result.verdict, Verdict.OK, result.message)
        self.assertEqual(self.output.read_text().strip(), "65534 65534 []")


class NoCgroupRunnerTests(ExecutionTestsMixin, unittest.TestCase):
    use_cgroup = False

    def test_default_api_does_not_silently_fall_back(self):
        result = run_case(
            [sys.executable, "-c", "print('must not run')"], self.input, self.output,
            cgroup_root=self.root / "missing-cgroup", drop_privileges=False,
        )
        self.assertEqual(result.verdict, Verdict.SYSTEM_ERROR)
        self.assertFalse(self.output.exists())

    def test_disabled_cgroup_is_not_accessed_and_rss_does_not_cause_mle(self):
        # 故意给一个不存在的根目录和远小于实际 RSS 的内存阈值。
        # 显式禁用 cgroup 后应照常运行，且不能把缺失的内存计量补成 RSS。
        result = self.run_python(
            "a = bytearray(8 * 1024 * 1024)", Limits(memory_kb=1),
            cgroup_root=self.root / "missing-cgroup",
        )
        self.assertEqual(result.verdict, Verdict.OK, result.message)
        self.assertGreater(result.rss_kb, 1024)
        self.assertEqual(result.memory_peak_bytes, 0)
        self.assertEqual(result.memory_kb, 0)
        self.assertEqual(result.oom_events, 0)
        self.assertEqual(result.oom_kills, 0)


class RunnerTests(ExecutionTestsMixin, unittest.TestCase):
    use_cgroup = True

    def setUp(self):
        if not os.environ.get("ROJ_JUDGE_CGROUP_ROOT"):
            self.skipTest("需委派 cgroup：通过 examples/delegated.py 运行 make check")
        super().setUp()
        self.groups_root = Path(os.environ["ROJ_JUDGE_CGROUP_ROOT"])
        self.original_groups = set(self.groups_root.glob("case-*"))
        self.addCleanup(self.assert_no_remaining_cgroups)

    def assert_no_remaining_cgroups(self):
        self.assertEqual(set(self.groups_root.glob("case-*")), self.original_groups,
                         "执行结束后不应遗留提交 cgroup")

    def test_peak_survives_free_and_immediate_exit(self):
        # 先触碰真实页面，再立刻释放并退出。无需 sleep 给采样器创造机会。
        result = self.run_python("a = bytearray(32 * 1024 * 1024); del a",
                                 Limits(memory_kb=16 * 1024, memory_slack_kb=64 * 1024))
        self.assertEqual(result.verdict, Verdict.MLE, result.message)
        self.assertGreaterEqual(result.memory_kb, 32 * 1024)
        self.assertEqual(result.exit_code, 0)

    def test_large_python_parent_does_not_cause_mle(self):
        source = "a = bytearray(4 * 1024 * 1024)"
        limits = Limits(memory_kb=32 * 1024)
        baseline = self.run_python(source, limits)
        balloon = bytearray(96 * 1024 * 1024)
        self.assertEqual(len(balloon), 96 * 1024 * 1024)
        # 如果误用 helper 自己或原 Python fork 子进程的 rusage，这里将超过 96MiB。
        for _ in range(3):
            result = self.run_python(source, limits)
            self.assertEqual(result.verdict, Verdict.OK, result.message)
            self.assertGreater(result.memory_kb, 4 * 1024)
            self.assertLess(result.memory_kb, 32 * 1024)
            self.assertLess(abs(result.memory_kb - baseline.memory_kb), 8 * 1024)

    def test_cgroup_oom_is_reported_as_mle(self):
        result = self.run_python("a = bytearray(512 * 1024 * 1024)",
                                 Limits(memory_kb=32 * 1024))
        self.assertEqual(result.verdict, Verdict.MLE, result.message)
        self.assertGreater(result.oom_events, 0)
        self.assertGreater(result.oom_kills, 0)
        self.assertEqual(result.signal, signal.SIGKILL)

    def test_memory_margin_allows_finish_before_judging(self):
        # 判定 8MiB，保护 24MiB：超过题目阈值但仍正常退出，随后判 MLE。
        result = self.run_python("a = bytearray(12 * 1024 * 1024)",
                                 Limits(memory_kb=8 * 1024))
        self.assertEqual(result.verdict, Verdict.MLE, result.message)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.signal, 0)
        self.assertEqual(result.oom_events, 0)
        self.assertGreater(result.memory_peak_bytes, 8 * 1024 * 1024)
        self.assertLess(result.memory_peak_bytes, 24 * 1024 * 1024)

    def test_unavailable_cgroup_is_system_error(self):
        result = self.run_python("pass", cgroup_root=self.root / "no-cgroup")
        self.assertEqual(result.verdict, Verdict.SYSTEM_ERROR)

    def test_cleanup_includes_detached_descendants(self):
        pidfile = self.root / "detached.pid"
        result = self.run_python(
            "import os, time\np = os.fork()\n"
            f"if p:\n open({str(pidfile)!r}, 'w').write(str(p))\n time.sleep(0.1)\n"
            "else:\n os.setsid()\n time.sleep(10)\n",
        )
        self.assertEqual(result.verdict, Verdict.OK, result.message)
        self.assert_stopped(int(pidfile.read_text()))

    def test_memory_includes_simultaneous_descendants(self):
        result = self.run_python(
            "import os, time\np = os.fork()\na = bytearray(12 * 1024 * 1024)\n"
            "if p: os.waitpid(p, 0)\nelse: time.sleep(0.05)\n",
            Limits(memory_kb=16 * 1024, memory_slack_kb=32 * 1024),
        )
        self.assertEqual(result.verdict, Verdict.MLE, result.message)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.oom_events, 0)
        self.assertGreaterEqual(result.memory_peak_bytes, 24 * 1024 * 1024)


class LocalJudgeTests(unittest.TestCase):
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
                        sys.executable, str(Path(__file__).with_name("local_judge.py")),
                        "--pid", "1000", "--testdata", str(root / "testData"),
                        "--checker", "none", "--time", "1000", "--no-cgroup", str(source),
                    ], capture_output=True, text=True, timeout=10)
                    self.assertEqual(process.returncode, exit_code, process.stdout + process.stderr)
                    expected = "编译失败（CE）" if verdict == "CE" else f"结果：{verdict}"
                    self.assertIn(expected, process.stdout)

            source.write_text(cases[0][0])
            process = subprocess.run([
                sys.executable, str(Path(__file__).with_name("local_judge.py")),
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

    def test_microseconds_avoid_rounding_at_cpu_boundary(self):
        for cpu_us, expected in ((1000000, Verdict.OK), (1000001, Verdict.TLE)):
            result = CaseResult(cpu_time_us=cpu_us, cpu_time_ms=1000)
            _set_verdict(result, Limits())
            self.assertEqual(result.verdict, expected)

    def test_memory_uses_exact_bytes_and_oom_evidence(self):
        limit = 128 * 1024 * 1024
        for peak, expected in ((limit, Verdict.OK), (limit + 1, Verdict.MLE)):
            result = CaseResult(memory_peak_bytes=peak)
            _set_verdict(result, Limits())
            self.assertEqual(result.verdict, expected)
        result = CaseResult(oom_events=1, signal=signal.SIGKILL)
        _set_verdict(result, Limits())
        self.assertEqual(result.verdict, Verdict.MLE)


if __name__ == "__main__":
    unittest.main()
