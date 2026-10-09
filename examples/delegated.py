#!/usr/bin/env python3
"""在 systemd 委派的专用 scope 内准备测试/执行环境。

供 runner.py、make check 和教程示例使用。local_judge.py 的自动委派不经过本脚本：
它把同一套准备逻辑内联成 prepare_delegated_scope()，用 `--in-scope` 直接换壳重跑，
省掉一层解释器启动。两处对“什么是合法的独占 scope”的判定必须保持一致。

systemd-run --user --scope -p Delegate=yes -- python3 examples/delegated.py make check
systemd-run --scope -p Delegate=yes -- python3 examples/delegated.py make check   # root
systemd-run --user --scope -p Delegate=yes -- python3 examples/delegated.py python3 runner.py ...

只在这个新建的 scope 内使用。不要在普通登录 session 或其他服务里运行本脚本。
"""
import os
from pathlib import Path
import subprocess
import sys

scope = next(line.split("::", 1)[1].strip() for line in
             Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
root = Path("/sys/fs/cgroup") / scope.lstrip("/")
if not root.name.endswith(".scope") or not os.access(root, os.W_OK):
    sys.exit("请通过 systemd-run [--user] --scope -p Delegate=yes 启动本脚本")
if set((root / "cgroup.procs").read_text().split()) != {str(os.getpid())}:
    sys.exit("只允许准备本脚本独占的新 scope，不能修改包含其他进程的 scope")
if "memory" not in (root / "cgroup.controllers").read_text().split():
    sys.exit("当前 scope 没有可委派的 memory controller")

# domain controller 要求管理目录本身没有进程。先把执行器放到独立的管理叶子组，
# 再为父目录启用 memory；之后的每个提交都是管理叶子组的兄弟组。
manager = root / "manager"
manager.mkdir()
(manager / "cgroup.procs").write_text(str(os.getpid()))
(root / "cgroup.subtree_control").write_text("+memory")
os.environ["ROJ_JUDGE_CGROUP_ROOT"] = str(root)
if len(sys.argv) == 1:
    sys.exit("请指定要运行的命令，例如 make check")
sys.exit(subprocess.call(sys.argv[1:]))
