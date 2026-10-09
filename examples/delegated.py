#!/usr/bin/env python3
"""在 systemd 委派的专用 scope 内准备测试/执行环境。

供 make check 和教程示例使用。scope 准备规则由 judge.prepare_delegated_scope()
提供，本脚本不再自己实现一份：两处对“什么是合法的独占 scope”的判定必须一致，
所以只留一个定义。

judge.py 的自动委派也不经过本脚本：它在 `--in-scope` 分支里调用同一个函数后
直接换壳重跑，省掉一层解释器启动。本脚本的价值是能包住任意命令（例如 make）。

systemd-run --user --scope -p Delegate=yes -- python3 examples/delegated.py make check
systemd-run --scope -p Delegate=yes -- python3 examples/delegated.py make check   # root
systemd-run --user --scope -p Delegate=yes -- python3 examples/delegated.py <命令> ...

只在这个新建的 scope 内使用。不要在普通登录 session 或其他服务里运行本脚本。
"""
import subprocess
import sys
from pathlib import Path

# 通过脚本位置定位仓库根目录，不依赖调用者的当前目录。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from judge import prepare_delegated_scope  # noqa: E402

ok, reason = prepare_delegated_scope()
if not ok:
    sys.exit(reason)
if len(sys.argv) == 1:
    sys.exit("请指定要运行的命令，例如 make check")
sys.exit(subprocess.call(sys.argv[1:]))
