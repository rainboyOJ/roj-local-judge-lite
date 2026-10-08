"""第 8 章：只在已委派的父目录中创建本次实验自己的 cgroup。"""
import os
from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from runner import Limits, Verdict, run_case

if not os.environ.get("ROJ_JUDGE_CGROUP_ROOT"):
    sys.exit("需要委派目录；请按第 8 章通过 examples/delegated.py 启动")

root = Path(os.environ["ROJ_JUDGE_CGROUP_ROOT"])
before = set(root.glob("case-*"))
with tempfile.TemporaryDirectory(prefix="judge-memory-tutorial-") as directory:
    work = Path(directory)
    for mib in (4, 12, 64):
        result = run_case(
            [str(Path(__file__).resolve().parent / ".build" / "allocate"), str(mib)],
            Path("/dev/null"), work / f"out-{mib}",
            Limits(memory_kb=8 * 1024, memory_slack_kb=16 * 1024),
            cwd=work, cgroup_root=root, drop_privileges=False,
        )
        print(f"requested={mib}MiB verdict={result.verdict.value} "
              f"peak={result.memory_peak_bytes / 1024 / 1024:.1f}MiB "
              f"exit={result.exit_code} signal={result.signal} "
              f"oom={result.oom_events} oom_kills={result.oom_kills}", flush=True)
        if result.verdict == Verdict.SYSTEM_ERROR:
            sys.exit(result.message)
assert set(root.glob("case-*")) == before, "实验结束后不应遗留 case cgroup"
