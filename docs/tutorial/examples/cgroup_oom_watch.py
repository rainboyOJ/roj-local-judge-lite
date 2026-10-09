"""监听一个 cgroup 里有没有发生 OOM（内存不足被杀）。

用法：
    python3 cgroup_oom_watch.py <cgroup目录> [超时秒数]

例子：
    python3 cgroup_oom_watch.py /sys/fs/cgroup/.../box 30

原理：cgroup v2 会把"内存不足"这件事记在 memory.events 文件里。
我们每隔 0.02 秒读一次 oom_kill 这一项，数字变大了就说明有进程被杀了。

为什么用轮询而不是 inotify？轮询只用 Python 内置功能，不需要装任何库，
而且 cgroup 的这些文件每次读都很快，0.02 秒的间隔完全够用。

注意：本程序应该待在"被监听的 cgroup 之外"运行。如果自己也关在同一个
笼子里，一旦触发整组 OOM，监视者会跟着一起被杀掉。
"""
import sys
import time
from pathlib import Path


def read_counter(box: Path, name: str) -> int:
    """从 memory.events 里读出某一项的次数。"""
    for line in (box / "memory.events").read_text().splitlines():
        key, value = line.split()
        if key == name:
            return int(value)
    return 0


def human(n: int) -> str:
    return f"{n} 字节（{n / 1024 / 1024:.1f} MB）"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    box = Path(sys.argv[1])
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

    if not (box / "memory.events").is_file():
        print(f"这不是一个 cgroup 目录（找不到 memory.events）：{box}")
        return 2

    first_kills = read_counter(box, "oom_kill")
    print(f"开始监听：{box}")
    print(f"  起始 oom_kill = {first_kills}")
    print(f"  起始峰值      = {human(int((box / 'memory.peak').read_text()))}")

    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        kills = read_counter(box, "oom_kill")

        if kills > first_kills:
            peak = int((box / "memory.peak").read_text())
            print(f"  检测到 OOM！用时 {elapsed:.2f} 秒")
            print(f"  oom_kill    ：{first_kills} → {kills}")
            print(f"  oom（总次数）：{read_counter(box, 'oom')}")
            print(f"  最终峰值    ：{human(peak)}")
            return 0

        if elapsed >= timeout:
            print(f"  {timeout:.0f} 秒内没有发生 OOM（peak 仍是 "
                  f"{human(int((box / 'memory.peak').read_text()))}）")
            return 1

        time.sleep(0.02)


if __name__ == "__main__":
    sys.exit(main())
