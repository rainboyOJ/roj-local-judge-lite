"""验证教程中声称的实验现象；每个外部命令都有超时，cgroup 需要显式委派。"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

EXAMPLES = Path(__file__).resolve().parent
REPO = EXAMPLES.parents[2]
BUILD = EXAMPLES / ".build"


def run(*args, input=None):
    result = subprocess.run([str(arg) for arg in args], cwd=REPO, input=input,
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout


assert run(BUILD / "sum", input="2 3\n") == "5\n"
assert f"cwd={REPO}" in run(BUILD / "process")
print("PASS A+B and process identity", flush=True)

output = run(BUILD / "fork_exec")
pids = re.findall(r"(?:before|after) exec: pid=(\d+)", output)
assert len(pids) == 2 and pids[0] == pids[1], output
assert "parent value=7" in output and "exit=7" in output, output
print("PASS fork memory and exec identity", flush=True)

with tempfile.TemporaryDirectory(prefix="judge-tutorial-check-") as directory:
    root = Path(directory)
    source = root / "input"
    target = root / "output"
    source.write_text("2 3\n")
    output = run(BUILD / "redirect", BUILD / "sum", source, target)
    assert "parent stdout still points to the terminal" in output, output
    assert target.read_text() == "5\n"
    output = run(BUILD / "error_pipe", root / "missing-program")
    assert "setup errno=" in output and "exit=127" in output, output
output = run(BUILD / "error_pipe", "/bin/sh", "-c", "exit 127")
assert "no setup error record" in output and "exit=127" in output, output
print("PASS redirection and setup error channel", flush=True)

output = run(BUILD / "usage", "sleep")
cpu, wall = map(float, re.search(r"cpu_ms=([\d.]+) wall_ms=([\d.]+)", output).groups())
assert "exit=0" in output and wall >= 900 and cpu < wall / 2, output
output = run(BUILD / "usage", "cpu")
assert f"signal={int(signal.SIGXCPU)}" in output, output
output = run(BUILD / "group")
groups = re.search(r"parent group=(\d+) child group=(\d+)", output)
assert groups and groups[1] != groups[2], output
assert f"signal={int(signal.SIGKILL)}" in output, output
print("PASS CPU/wall measurement and process group cleanup", flush=True)

assert "touched_mib=4" in run(BUILD / "allocate", "4")
output = run(sys.executable, EXAMPLES / "api_demo.py")
report, user_output = output.split("\nuser output:", 1)
assert json.loads(report)["verdict"] == "OK", output
assert user_output.strip() == "5", output
print("PASS allocation and Python API", flush=True)

if os.environ.get("ROJ_JUDGE_CGROUP_ROOT"):
    output = run(sys.executable, EXAMPLES / "cgroup_demo.py")
    print(output, end="", flush=True)
    assert "requested=4MiB verdict=OK" in output, output
    normal_mle = next(line for line in output.splitlines() if "requested=12MiB" in line)
    assert "verdict=MLE" in normal_mle and "exit=0 signal=0 oom=0" in normal_mle, output
    oom_mle = next(line for line in output.splitlines() if "requested=64MiB" in line)
    assert "verdict=MLE" in oom_mle and re.search(r"oom=[1-9]\d*", oom_mle), output
    print("PASS cgroup peak, OOM and cleanup", flush=True)
else:
    print("SKIP cgroup: use examples/delegated.py or set ROJ_JUDGE_CGROUP_ROOT", flush=True)
