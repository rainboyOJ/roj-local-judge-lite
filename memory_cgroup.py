"""每次执行一个独立的 cgroup v2：设置保护上限、读取峰值与 OOM、清理后代。

调用者提供已经委派且启用 memory controller 的父目录。本模块只操作自己创建的
随机子目录，不修改系统其他 cgroup。内存计量包含进程树、文件缓存和部分内核内存。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path


def read_counters(path: Path) -> dict[str, int]:
    return {name: int(value) for name, value in
            (line.split() for line in path.read_text().splitlines())}


class MemoryCgroup:
    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.path = root / ("case-" + uuid.uuid4().hex)
        self.max_bytes = max_bytes

    def __enter__(self):
        enabled = (self.root / "cgroup.subtree_control").read_text().split()
        if "memory" not in enabled:
            raise OSError(f"{self.root} 尚未为子组启用 memory controller")
        self.path.mkdir()
        try:
            # 判定阈值由 runner 保存；这里的 memory.max 是更宽的保护上限。
            (self.path / "memory.max").write_text(str(self.max_bytes) if self.max_bytes else "max")
            (self.path / "memory.swap.max").write_text("0")
            (self.path / "memory.oom.group").write_text("1")
            # 提前检查所需接口，禁止悄悄退回 RSS 采样或无内存限制的运行。
            for name in ("memory.peak", "memory.events", "cgroup.kill"):
                if not (self.path / name).exists():
                    raise OSError(f"内核缺少 cgroup 接口 {name}")
        except BaseException:
            self.path.rmdir()
            raise
        return self

    @property
    def procs_path(self) -> Path:
        return self.path / "cgroup.procs"

    def stop(self) -> None:
        # cgroup.kill 包含已 setsid 的后代；只有进程组 kill 无法覆盖这种情况。
        (self.path / "cgroup.kill").write_text("1")
        deadline = time.monotonic() + 5
        while read_counters(self.path / "cgroup.events")["populated"]:
            if time.monotonic() >= deadline:
                raise OSError(f"cgroup 中的进程未退出，保留目录便于排查: {self.path}")
            time.sleep(0.001)

    def memory_result(self) -> dict[str, int]:
        # 每次创建新组，计数从零开始；程序退出后，峰值和 OOM 事件仍然可以读。
        peak = int((self.path / "memory.peak").read_text())
        events = read_counters(self.path / "memory.events")
        return {
            "memory_peak_bytes": peak,
            "memory_kb": (peak + 1023) // 1024,
            "oom_events": events["oom"],
            "oom_kills": events["oom_kill"],
        }

    def __exit__(self, exc_type, exc, traceback):
        # 正常、setup 失败、Ctrl+C、helper 故障都必须经过这里。
        self.stop()
        self.path.rmdir()
