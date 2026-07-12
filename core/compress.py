# -*- coding: utf-8 -*-
"""模块四：一键压缩。

把指定目录下的**每个一级子文件夹**分别压缩成单独的压缩包，与批量解压对称：
- 只取首层文件夹（不递归），空文件夹静默跳过
- 包内顶层即该文件夹名，正好对上解压侧「唯一顶层文件夹」的约定，形成闭环
- 跳过模式：目标压缩包已存在则跳过（重复运行不重复压缩）
- 磁盘空间预检、压缩成功后可选删除源文件夹（进回收站，复用解压侧开关模式）
- 失败不中断整体流程，记入失败清单
"""

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core.config import Config
from core.sevenzip import SevenZip
from utils import is_path_too_long

# 可选依赖：装了 send2trash 则"删除源文件夹"进回收站（可恢复），没装则永久删除
try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

# 磁盘空间预检的安全余量：剩余空间需大于「源目录大小 + 此值」
_FREE_SPACE_MARGIN = 64 * 1024 * 1024

# 各格式对应的压缩包扩展名
_EXT_BY_FMT = {"7z": "7z", "zip": "zip"}


@dataclass
class CompressRecord:
    """一个文件夹的压缩结果，用于报告导出与失败清单。"""
    folder: str
    result: str                # 成功 / 失败 / 跳过
    detail: str = ""
    elapsed: float = 0.0
    out_file: str = ""
    op: str = "压缩"


def find_top_folders(root: Path) -> list[Path]:
    """返回 root 下的一级子文件夹（不递归），跳过空文件夹。

    不用 glob：游戏文件夹名常含 [] 等 glob 特殊字符，改用 iterdir。
    """
    folders: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return folders
    for p in entries:
        if not p.is_dir():
            continue
        try:
            if any(p.iterdir()):     # 非空才压缩
                folders.append(p)
        except OSError:
            continue
    return folders


def _dir_size(path: Path) -> int:
    """估算目录未压缩总大小（字节），用于磁盘空间预检。"""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _free_space(path: Path) -> Optional[int]:
    """查询 path 所在磁盘剩余空间；path 不存在时向上找最近存在的祖先。"""
    p = path
    while not p.exists():
        parent = p.parent
        if parent == p:
            return None
        p = parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def compress_one(
    folder: Path,
    out_dir: Path,
    config: Config,
    sz: SevenZip,
    log: Callable[[str, str], None],
    file_progress: Optional[Callable[[int], None]] = None,
) -> CompressRecord:
    """压缩单个文件夹 → out_dir/<folder>.<ext>。"""
    start = time.time()
    fmt = config.data.get("compress_format", "7z")
    if fmt not in _EXT_BY_FMT:
        fmt = "7z"
    ext = _EXT_BY_FMT[fmt]
    volume_size = str(config.data.get("compress_volume_size", "")).strip()
    # 分卷时 7z 会追加 .001 等后缀，首卷名即 <folder>.<ext>.001
    out_file = out_dir / f"{folder.name}.{ext}"

    if is_path_too_long(out_file):
        log(f"[跳过] 目标路径过长：{out_file}", "warn")
        return CompressRecord(str(folder), "跳过", "目标路径过长",
                              elapsed=time.time() - start)

    # 跳过模式：目标压缩包已存在则跳过（分卷时判断首卷）。
    # 否则 7z a 会把内容追加进旧包，造成重复/污染
    check = out_dir / f"{folder.name}.{ext}.001" if volume_size else out_file
    if config.data.get("skip_existing_folder", True) and check.exists():
        log(f"[跳过] 目标压缩包已存在：{check.name}", "warn")
        return CompressRecord(str(folder), "跳过", "目标压缩包已存在（跳过模式）",
                              elapsed=time.time() - start)

    # 磁盘空间预检：剩余空间需大于源目录大小 + 安全余量（压缩后通常更小，从宽）
    src_size = _dir_size(folder)
    free = _free_space(out_dir)
    if free is not None and free < src_size + _FREE_SPACE_MARGIN:
        need_gb, free_gb = src_size / 1024 ** 3, free / 1024 ** 3
        reason = f"磁盘空间不足（源约 {need_gb:.1f} GB，剩余 {free_gb:.1f} GB）"
        log(f"[失败] {folder.name}：{reason}", "error")
        return CompressRecord(str(folder), "失败", reason, elapsed=time.time() - start)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"[失败] {folder.name}：无法创建输出目录 {out_dir}（{e}）", "error")
        return CompressRecord(str(folder), "失败", f"输出目录创建失败：{e}",
                              elapsed=time.time() - start)

    if file_progress:
        file_progress(0)
    try:
        level = int(config.data.get("compress_level", 5))
    except (TypeError, ValueError):
        level = 5
    log(f"    压缩 {folder.name} → {out_file.name}（{fmt}, 级别 {level}）...", "info")
    result = sz.add(
        out_file, folder,
        password=config.data.get("compress_password", ""),
        level=level, fmt=fmt,
        header_encrypt=config.data.get("compress_header_encrypt", True),
        volume_size=volume_size,
        progress_cb=file_progress,
    )
    if not result.ok:
        log(f"[失败] {folder.name}：压缩出错\n{result.output[-300:]}", "error")
        return CompressRecord(str(folder), "失败", "7z 压缩失败",
                              elapsed=time.time() - start)

    # 可选：删除源文件夹（装了 send2trash 则进回收站可恢复）
    deleted_note = ""
    if config.data.get("compress_delete_source", False):
        use_recycle = config.data.get("delete_to_recycle", True) and _send2trash is not None
        how = "回收站" if use_recycle else "永久删除"
        try:
            if use_recycle:
                _send2trash(str(folder))
            else:
                shutil.rmtree(folder)
            deleted_note = f"；已删除源文件夹（{how}）"
            log(f"    已删除源文件夹（{how}）", "info")
        except OSError as e:
            deleted_note = f"；源文件夹删除失败：{e}"
            log(f"    源文件夹删除失败：{e}", "warn")

    elapsed = time.time() - start
    pwd_note = "（加密）" if config.data.get("compress_password", "") else ""
    log(f"[成功] {folder.name} → {out_file} {pwd_note}，耗时 {elapsed:.1f}s{deleted_note}",
        "success")
    return CompressRecord(str(folder), "成功", deleted_note.lstrip("；"),
                          elapsed=elapsed, out_file=str(out_file))


def compress_batch(
    config: Config,
    sz: SevenZip,
    log: Callable[[str, str], None],
    scan_root: Path,
    should_stop: Optional[Callable[[], bool]] = None,
    file_progress: Optional[Callable[[int], None]] = None,
    total_progress: Optional[Callable[[int, int, str], None]] = None,
    on_record: Optional[Callable[[CompressRecord], None]] = None,
) -> tuple[list[CompressRecord], bool]:
    """批量压缩主循环：遍历 scan_root 的一级子文件夹，逐个压缩成单独的包。

    输出目录取 config["compress_output_dir"]，留空则放在 scan_root（各文件夹旁）。
    返回 (全部记录, 是否被用户停止)。
    """
    records: list[CompressRecord] = []

    def emit(rec: CompressRecord):
        records.append(rec)
        if on_record:
            on_record(rec)

    out_raw = str(config.data.get("compress_output_dir", "")).strip()
    out_dir = Path(out_raw) if out_raw else scan_root

    log(f"—— 一键压缩：扫描 {scan_root} 的一级子文件夹 ——", "info")
    folders = find_top_folders(scan_root)
    log(f"共找到 {len(folders)} 个待压缩文件夹", "info")

    total = len(folders)
    stopped = False
    for done, folder in enumerate(folders):
        if should_stop and should_stop():
            log("已按用户请求停止", "warn")
            stopped = True
            break
        if total_progress:
            total_progress(done, total, folder.name)
        log(f"[{done + 1}/{total}] 压缩 {folder.name}", "info")
        emit(compress_one(folder, out_dir, config, sz, log, file_progress))
        if total_progress:
            total_progress(done + 1, total, "")

    return records, stopped
