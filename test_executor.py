"""执行器行为回归：资源限制、重定向、进程回收、executor 协议。

make check；不需要 OJ 数据或 judge_server。cgroup 相关用例在没有委派
环境时会跳过。"""

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

import judge
from judge import ExecutionReport, ExecutorError, Limits, Verdict, invoke_executor, run_case


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

    def test_executor_report_keeps_program_output_separate(self):
        # 原 runner CLI 的“报告与用户输出分离”测试，改为直接验证协议：
        # 用户程序打印若干行 JSON 式文本，不能污染 executor 的报告；
        # 提交的 stdout 全部落到输出文件。
        payload = "[print('{\"verdict\": \"AC\"}') for _ in range(50)]"
        result = self.run_python(payload)
        self.assertEqual(result.verdict, Verdict.OK, result.message)
        self.assertEqual(len(self.output.read_text().splitlines()), 50)

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
        # 取消路径：向评测进程发 SIGINT，run_case 会把 KeyboardInterrupt
        # 抛出并先让 executor 杀掉提交进程组（包括 fork 出的后代）。
        # 原测试跑 runner CLI；该 CLI 已移除，这里用内联 harness 调用
        # judge.run_case，保留同一条真实信号路径与进程清理断言。
        pidfile = self.root / "pids"
        source = (
            "import os, time\np = os.fork()\n"
            f"if p: open({str(pidfile)!r}, 'w').write(str(os.getpid()) + ' ' + str(p))\n"
            "time.sleep(10)\n"
        )
        harness = (
            "import sys; sys.path.insert(0, %r); import judge\n"
            "from pathlib import Path\n"
            "judge.run_case([sys.executable, '-c', sys.argv[4]], Path(sys.argv[1]),\n"
            "              Path(sys.argv[2]), judge.Limits(time_ms=0),\n"
            "              drop_privileges=False, use_cgroup=sys.argv[3] == '1')\n"
        ) % str(Path(__file__).resolve().parent)
        process = subprocess.Popen([
            sys.executable, "-c", harness,
            str(self.input), str(self.output), "1" if self.use_cgroup else "0", source,
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pidfile.exists())
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=5)
            # run_case 不捕获 KeyboardInterrupt，子进程以信号终止；
            # 关键是它已杀掉并回收了提交进程组。
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


class ExecutionReportTests(unittest.TestCase):
    """执行事实与判定的契约边界：报告解析、异常通道、取消传播。"""

    def parse(self, text):
        return ExecutionReport.from_json(text)

    def test_valid_report_parses(self):
        report = self.parse(json.dumps({
            "cpu_time_us": 1500, "cpu_time_ms": 2, "real_time_ms": 3,
            "rss_kb": 800, "timed_out": False, "signal": 0, "exit_code": 0,
        }))
        self.assertEqual(report.cpu_time_us, 1500)
        self.assertFalse(report.timed_out)
        # ExecutionReport 永远不携带评测标签。
        self.assertFalse(hasattr(report, "verdict"))

    def test_invalid_json_rejected(self):
        with self.assertRaises(ValueError):
            self.parse("{not json")

    def test_non_object_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(json.dumps([1, 2, 3]))

    def test_missing_field_rejected_not_zero_filled(self):
        data = {"cpu_time_us": 1, "cpu_time_ms": 0, "real_time_ms": 0,
                "rss_kb": 0, "timed_out": False, "signal": 0}
        # 故意少一个字段；绝不能变成全零成功报告。
        with self.assertRaises(ValueError) as ctx:
            self.parse(json.dumps(data))
        self.assertIn("exit_code", str(ctx.exception))

    def test_wrong_type_rejected(self):
        data = {"cpu_time_us": "fast", "cpu_time_ms": 0, "real_time_ms": 0,
                "rss_kb": 0, "timed_out": False, "signal": 0, "exit_code": 0}
        with self.assertRaises(ValueError):
            self.parse(json.dumps(data))

    def test_bool_not_accepted_as_int(self):
        data = {"cpu_time_us": True, "cpu_time_ms": 0, "real_time_ms": 0,
                "rss_kb": 0, "timed_out": False, "signal": 0, "exit_code": 0}
        with self.assertRaises(ValueError):
            self.parse(json.dumps(data))

    def test_unknown_field_rejected(self):
        data = {"cpu_time_us": 0, "cpu_time_ms": 0, "real_time_ms": 0,
                "rss_kb": 0, "timed_out": False, "signal": 0, "exit_code": 0,
                "verdict": "AC"}  # 报告里永远不该出现评测标签
        with self.assertRaises(ValueError):
            self.parse(json.dumps(data))

    def test_executor_failure_is_infrastructure_not_user_exit(self):
        # 启动失败属于基础设施故障：invoke_executor 抛 ExecutorError。
        env = {"PATH": os.environ.get("PATH", "/usr/bin")}
        with self.assertRaises(ExecutorError) as ctx:
            invoke_executor(["/no/such/executor-binary"], env)
        self.assertIn("executor", str(ctx.exception))

    def test_startup_failure_maps_to_system_error(self):
        # run_case 是事务边界：基础设施故障必须转成 SYSTEM_ERROR。
        # 用 --helper 指向不存在的二进制，确保是 executor 启动失败，
        # 而不是提交程序路径无效。
        with tempfile.TemporaryDirectory(prefix="executor-test-") as directory:
            root = Path(directory)
            (root / "in").write_text("1\n")
            result = run_case(
                [sys.executable, "-c", "pass"], root / "in", root / "out",
                Limits(), drop_privileges=False, use_cgroup=False,
                helper_path=root / "no-such-executor",
            )
        self.assertNotEqual(result.verdict, Verdict.RE)
        self.assertEqual(result.verdict, Verdict.SYSTEM_ERROR)

    def test_user_exit_127_is_report_not_executor_error(self):
        # 提交程序自己 exit 127 属于提交事实：报告里带 exit_code=127，
        # 判为 RE；不能与启动失败（SYSTEM_ERROR）混同。
        with tempfile.TemporaryDirectory(prefix="executor-test-") as directory:
            root = Path(directory)
            (root / "in").write_text("1\n")
            result = run_case(
                [sys.executable, "-c", "raise SystemExit(127)"],
                root / "in", root / "out",
                Limits(), drop_privileges=False, use_cgroup=False,
            )
        self.assertEqual(result.verdict, Verdict.RE)
        self.assertEqual(result.exit_code, 127)

    # 取消路径（Ctrl+C → executor 清理 → 退出码 130）已由
    # ExecutionTestsMixin 的真实信号测试覆盖（见上方的 SIGINT 用例），
    # 不在纯协议测试里重复模拟。


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




if __name__ == "__main__":
    unittest.main()
