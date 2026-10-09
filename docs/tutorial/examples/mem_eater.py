"""一个会占内存的小程序，用来观察 cgroup 的计量和限制。

用法：
    python3 mem_eater.py 申请多少MB [保持多少秒]

第一个参数决定申请多少 MB。第二个参数是可选的：申请后保持这么多秒再退出，
方便你有时间在另一个地方观察 memory.current 的变化。

例子：
    python3 mem_eater.py 12 3      # 申请 12MB，保持 3 秒
    python3 mem_eater.py 300       # 申请 300MB，申请完立刻退出
"""
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    mb = int(sys.argv[1])
    hold_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 0

    data = bytearray(mb * 1024 * 1024)
    # bytearray 申请出来的页可能还没真的占用物理内存。每页写一个字节，
    # 强制内核真正分配内存，这样 cgroup 才能统计到这部分用量。
    for i in range(0, len(data), 4096):
        data[i] = 1
    print(f"已申请并触碰 {mb} MB", flush=True)

    if hold_seconds:
        time.sleep(hold_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
