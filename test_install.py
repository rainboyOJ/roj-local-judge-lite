"""离线验证安装流程：替换下载与构建命令，只操作测试自己的临时目录。"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class InstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="judge-install-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.package = Path(__file__).resolve().parent
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("local_judge.py", "runner.py", "memory_cgroup.py"):
            shutil.copyfile(self.package / name, self.source / name)
        self.destination = self.root / "installed"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.launcher = self.bin_dir / "roj-local-judge-lite"
        self.stage_dir = self.root / "staging"
        self.stage_dir.mkdir()
        commands = self.root / "commands"
        commands.mkdir()
        # git 只模拟 clone 的结果，绝不访问网络；make 模拟构建成功及可控的测试结果。
        scripts = {
            "git": """#!/bin/bash
set -eu
[ "$1" = clone ] || exit 90
cp -a "$ROJ_TEST_INSTALL_SOURCE" "${@: -1}"
""",
            "make": """#!/bin/sh
set -eu
case "$1" in
    -C)
        printf '#!/bin/sh\nexit 0\n' > "$2/runner_helper"
        chmod +x "$2/runner_helper"
        ;;
    check)
        echo 'installer regression: check executed'
        echo 'Ran 1 test'
        exit "$ROJ_TEST_CHECK_EXIT"
        ;;
    *) exit 91 ;;
esac
""",
        }
        for name, text in scripts.items():
            command = commands / name
            command.write_text(text)
            command.chmod(0o755)
        self.env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                        TMPDIR=str(self.stage_dir), ROJ_TEST_INSTALL_SOURCE=str(self.source),
                        ROJ_TEST_CHECK_EXIT="42")

    def install(self, *options):
        return subprocess.run([
            "bash", str(self.package / "install.sh"), "--no-mirror",
            "--dir", str(self.destination), "--bin-dir", str(self.bin_dir), *options,
        ], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)

    def test_failed_check_preserves_existing_installation(self):
        # 即使 --force，也必须等检查成功后才允许替换原安装和启动器。
        self.destination.mkdir()
        sentinel = self.destination / "old-version"
        sentinel.write_text("keep old installation")
        self.launcher.write_text("keep old launcher")
        result = self.install("--force")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("installer regression: check executed", result.stderr)
        self.assertIn("make check 未通过", result.stderr)
        self.assertEqual(sentinel.read_text(), "keep old installation")
        self.assertEqual(self.launcher.read_text(), "keep old launcher")
        self.assertEqual(list(self.destination.iterdir()), [sentinel])
        self.assertEqual(list(self.stage_dir.iterdir()), [])

    def test_successful_check_installs_package(self):
        self.env["ROJ_TEST_CHECK_EXIT"] = "0"
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("单元测试通过", result.stdout)
        self.assertTrue((self.destination / "runner_helper").is_file())
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertEqual(list(self.stage_dir.iterdir()), [])

    def test_explicit_no_smoke_skips_failing_check(self):
        result = self.install("--no-smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("单元测试通过", result.stdout)
        self.assertTrue((self.destination / "runner_helper").is_file())
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertEqual(list(self.stage_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
