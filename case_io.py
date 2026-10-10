"""输入输出模式：配置解析、文件名校验、测试点运行目录与文件准备。

两种模式：

- `stdio`（默认）：输入走 stdin，答案取 executor 捕获的 stdout。
- `file`：提交用 `freopen("apple.in", "r", stdin)` 之类自行读写指定文件；
  judge 在每个测试点目录里放输入副本，答案取提交生成的输出文件。

本模块只做文件与权限；不启动 executor、不创建 cgroup、不比较答案。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 评测器自己占用的文件名前缀：捕获的 stdout/stderr 等，不与题目文件重名。
RESERVED_PREFIX = "_judge."

# stdio 模式下 executor 捕获标准输出/错误的文件名（放在测试点目录内）。
STDOUT_NAME = RESERVED_PREFIX + "stdout"
STDERR_NAME = RESERVED_PREFIX + "stderr"

MODES = ("stdio", "file")


@dataclass(frozen=True)
class IOConfig:
    """题目的输入输出模式；默认 stdio。"""

    mode: str = "stdio"
    input_file: Optional[str] = None
    output_file: Optional[str] = None

    @property
    def is_file_mode(self) -> bool:
        return self.mode == "file"


@dataclass(frozen=True)
class CaseFiles:
    """一个测试点的运行目录与相关文件路径（均已在准备时确定）。"""

    cwd: Path
    stdin_path: Path
    """原测试输入（绝对路径）；两种模式下都作为 executor 的 stdin。"""

    stdout_path: Path
    """executor 捕获标准输出的文件。"""

    stderr_path: Path
    """executor 捕获标准错误的文件。"""

    answer_path: Path
    """用于比对答案的文件：stdio 模式同 stdout_path，file 模式是输出文件。"""


class IOConfigError(ValueError):
    """题目 IO 配置不合法；由 CLI 在编译前报错退出。"""


def _check_filename(name: object, field: str) -> str:
    """校验单个文件名：普通文件名，不允许路径与保留前缀。"""
    if not isinstance(name, str) or not name:
        raise IOConfigError(f"io.{field} 必须是非空字符串")
    if any(ch in name for ch in ("/", "\\", "\0")):
        raise IOConfigError(f"io.{field} 不允许包含路径分隔符：{name!r}")
    if name in (".", ".."):
        raise IOConfigError(f"io.{field} 不允许是 {name!r}")
    if name.startswith(RESERVED_PREFIX):
        raise IOConfigError(
            f"io.{field} 不能以 {RESERVED_PREFIX!r} 开头，该前缀留给评测器")
    return name


def parse_io_config(data: object) -> IOConfig:
    """从 config.json 顶层的 `io` 对象解析配置；缺失或 None 表示默认 stdio。"""
    if data is None:
        return IOConfig()
    if not isinstance(data, dict):
        raise IOConfigError("config.json 的 io 必须是一个对象")

    mode = data.get("mode", "stdio")
    if mode not in MODES:
        raise IOConfigError(f"io.mode 只能是 {' 或 '.join(MODES)}，实际是 {mode!r}")

    input_file = data.get("input_file")
    output_file = data.get("output_file")

    if mode == "stdio":
        # 允许写明文件名以便随时切换，但当前模式下不生效，给出提示而非报错。
        if input_file is not None or output_file is not None:
            print("提示：io.mode 为 stdio，input_file/output_file 不生效，已忽略")
        return IOConfig(mode="stdio")

    if input_file is None or output_file is None:
        raise IOConfigError("io.mode 为 file 时必须同时提供 input_file 和 output_file")
    input_name = _check_filename(input_file, "input_file")
    output_name = _check_filename(output_file, "output_file")
    if input_name == output_name:
        raise IOConfigError("io.input_file 与 io.output_file 不能同名")
    return IOConfig(mode="file", input_file=input_name, output_file=output_name)


def resolve_drop_privileges(drop_privileges: Optional[bool], run_uid: int) -> bool:
    """确定本次运行是否降权；目录准备与执行器必须用同一个值。"""
    if drop_privileges is not None:
        return drop_privileges
    return os.geteuid() == 0 and run_uid != os.geteuid()


def _apply_ownership_and_mode(path: Path, mode: int, apply_owner: Optional[tuple[int, int]]) -> None:
    """设置权限；需要降权时把属主改成执行身份。"""
    if apply_owner is not None:
        os.chown(path, apply_owner[0], apply_owner[1])
    os.chmod(path, mode)


def prepare_case_files(work_dir: Path, index: int, input_path: Path,
                       io: IOConfig, *, drop: bool, run_uid: int, run_gid: int) -> CaseFiles:
    """为第 index 个测试点建独立目录，复制输入，返回各文件路径。

    stdio 与 file 都使用独立目录：stdio 只是答案取捕获的 stdout。
    输入用复制而非软链接，提交改动自己的副本不影响原题目数据。

    成功返回后由调用者负责清理；中途失败会回滚自己刚创建的目录。
    """
    case_dir = work_dir / f"case-{index}"
    apply_owner = (run_uid, run_gid) if drop else None
    case_dir.mkdir(parents=True)
    try:
        stdin_path = input_path.resolve()
        stdout_path = case_dir / STDOUT_NAME
        stderr_path = case_dir / STDERR_NAME

        if io.is_file_mode:
            # 输入副本：给执行身份读权限，提交自己再打开它。
            source_copy = case_dir / io.input_file
            shutil.copyfile(stdin_path, source_copy)
            _apply_ownership_and_mode(source_copy, 0o444, apply_owner)
            answer_path = case_dir / io.output_file
            # 不提前创建输出文件：提交没生成它时才能判为“未生成输出”。
        else:
            answer_path = stdout_path

        # 目录权限必须在写文件之后设置：降权后执行身份要能在其中创建输出。
        _apply_ownership_and_mode(case_dir, 0o700, apply_owner)
    except BaseException:
        shutil.rmtree(case_dir, ignore_errors=True)
        raise

    return CaseFiles(cwd=case_dir, stdin_path=stdin_path, stdout_path=stdout_path,
                     stderr_path=stderr_path, answer_path=answer_path)


def check_answer_file(answer_path: Path, *, stdin_path: Optional[Path] = None,
                      expected_path: Optional[Path] = None) -> tuple[bool, str]:
    """检查答案文件是否可用；返回 (是否可比较, 原因或空)。

    用 lstat 判断类型，不跟随软链接；并拒绝与只读输入（原输入、标准答案）
    形成别名——那会导致拿同一个文件自我比较。执行判定在此之前，本函数只
    在内部 OK 时调用。
    """
    try:
        info = answer_path.lstat()
    except FileNotFoundError:
        return False, f"未生成输出文件 {answer_path.name}"
    except OSError as exc:
        return False, f"无法读取输出文件 {answer_path.name}：{exc}"

    import stat as stat_module
    if stat_module.S_ISDIR(info.st_mode):
        return False, f"输出 {answer_path.name} 是目录，必须是普通文件"
    if stat_module.S_ISLNK(info.st_mode):
        return False, f"输出 {answer_path.name} 是软链接，必须是普通文件"
    if not stat_module.S_ISREG(info.st_mode):
        return False, f"输出 {answer_path.name} 不是普通文件"

    for readonly in (stdin_path, expected_path):
        if readonly is None:
            continue
        try:
            if answer_path.exists() and Path(readonly).exists() \
                    and os.path.samefile(answer_path, readonly):
                return False, f"输出 {answer_path.name} 与只读文件 {Path(readonly).name} 是同一文件"
        except OSError:
            pass
    return True, ""
