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


def _attach_cleanup_note(exc: BaseException, cleanup_error: OSError) -> None:
    """把清理失败诊断挂到正在传播的异常上（Python 3.8 没有 add_note）。

    judge 在把异常转成 SE 时读取本属性，就能同时展示执行原因与清理原因。
    多条清理失败累加，不覆盖。
    """
    notes = getattr(exc, "cleanup_notes", None)
    if notes is None:
        notes = []
        try:
            exc.cleanup_notes = notes
        except AttributeError:  # 极少数异常禁止设属性；退化为不附加。
            return
    notes.append(str(cleanup_error))


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
            # 判定阈值由 judge 保存；这里的 memory.max 是更宽的保护上限。
            (self.path / "memory.max").write_text(str(self.max_bytes) if self.max_bytes else "max")
            (self.path / "memory.swap.max").write_text("0")
            (self.path / "memory.oom.group").write_text("1")
            # 提前检查所需接口，禁止悄悄退回 RSS 采样或无内存限制的运行。
            for name in ("memory.peak", "memory.events", "cgroup.kill"):
                if not (self.path / name).exists():
                    raise OSError(f"内核缺少 cgroup 接口 {name}")
        except BaseException as exc:
            # 回滚失败不能掩盖原始原因：原始异常继续传播，回滚诊断附加其上。
            try:
                self.path.rmdir()
            except OSError as rollback_error:
                _attach_cleanup_note(exc, rollback_error)
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
        # 正常、setup 失败、Ctrl+C、executor 故障都必须经过这里。
        #
        # 关键约定：清理失败不能吞掉正在传播的异常。
        #  - 无原始异常：把清理错误直接抛出，由调用方映射为 SE。
        #  - 有原始异常（含 KeyboardInterrupt）：保留原异常继续传播，
        #    把清理诊断附加到它上面（注意保持取消语义），不另抛新异常。
        # Python 3.8 没有 add_note()，用自定义属性附加诊断。
        try:
            self.stop()
        except OSError as cleanup_error:
            if exc is not None:
                _attach_cleanup_note(exc, cleanup_error)
                return False  # 让原异常继续传播
            raise
        try:
            self.path.rmdir()
        except OSError as cleanup_error:
            if exc is not None:
                _attach_cleanup_note(exc, cleanup_error)
                return False
            raise
        return False
