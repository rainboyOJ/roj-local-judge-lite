"""第 9 章：用 Python API 执行一次 A+B；先 make 和构建本目录实验。"""
import json
from pathlib import Path
import sys
import tempfile

# 文件位于 docs/tutorial/examples/，向上三层是仓库根目录。
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from runner import Limits, run_case

with tempfile.TemporaryDirectory(prefix="judge-tutorial-") as directory:
    work = Path(directory)
    input_path = work / "input"
    output_path = work / "output"
    input_path.write_text("2 3\n")
    result = run_case(
        [str(Path(__file__).resolve().parent / ".build" / "sum")],
        input_path, output_path, Limits(time_ms=1000),
        cwd=work, use_cgroup=False, drop_privileges=False,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if output_path.exists():
        print("user output:", output_path.read_text().strip())
