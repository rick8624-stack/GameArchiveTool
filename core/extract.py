# -*- coding: utf-8 -*-
"""模块二：批量解压（核心功能）。

- 递归扫描压缩文件，识别分卷（只处理首卷，后续卷跳过由 7z 自动串联）
- 密码池机制：先无密码，再按命中次数降序逐个用 7z t 验证，通过才真正解压
- 解压成功后可选删除原压缩包（含分卷全部文件）
- 失败不中断整体流程，记入失败清单
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.config import Config
from core.sevenzip import SevenZip, SevenZipResult
from utils import is_path_too_long

# ---------- 压缩包识别 ----------

# .7z.001 / .zip.001 / .rar.001 数字分卷
_NNN_VOLUME_RE = re.compile(r"(?i)^(?P<stem>.+)\.(?P<fmt>7z|zip|rar)\.(?P<num>\d{3})$")
# .part1.rar / .part01.rar 分卷（带主名，如 游戏.part1.rar）
_PART_VOLUME_RE = re.compile(r"(?i)^(?P<stem>.+)\.part(?P<num>\d+)\.rar$")
# 裸命名分卷：整个文件名就是 part1.rar / part01.rar（常见于一个游戏一个文件夹）
_BARE_PART_RE = re.compile(r"(?i)^part(?P<num>\d+)\.rar$")
# 旧式 rar 分卷的后续卷 .r00 .r01 ...
_R_VOLUME_RE = re.compile(r"(?i)^(?P<stem>.+)\.r\d{2}$")
# 普通单文件压缩包
_SINGLE_RE = re.compile(r"(?i)^(?P<stem>.+)\.(?P<fmt>7z|zip|rar)$")


@dataclass
class ArchiveItem:
    """一个待解压项：首卷文件 + 所属分卷组的全部文件（用于成功后删除）。"""
    main_file: Path
    volume_files: list[Path]   # 含 main_file 本身；单文件压缩包时只有它自己
    stem: str                  # 压缩包基础名（用于"解压到子文件夹"的文件夹名）
    kind: str                  # single / nnn / part / old_rar


@dataclass
class ExtractRecord:
    """一个压缩包的处理结果，用于报告导出与失败清单。"""
    archive: str
    result: str                # 成功 / 失败 / 跳过
    detail: str = ""           # 失败原因或备注
    password: str = ""         # 命中的密码（无密码为空）
    elapsed: float = 0.0       # 耗时（秒）


def find_archives(root: Path) -> list[ArchiveItem]:
    """递归扫描根目录，返回待处理压缩包列表（分卷只保留首卷）。

    空文件夹、无压缩包的文件夹自然不会产生任何条目（静默跳过）。
    """
    items: list[ArchiveItem] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name

        m = _NNN_VOLUME_RE.match(name)
        if m:
            if int(m.group("num")) != 1:
                continue  # 只处理 .001 首卷，后续卷由 7z 自动串联
            volumes = _collect_nnn_volumes(path, m.group("stem"), m.group("fmt"))
            items.append(ArchiveItem(path, volumes, m.group("stem"), "nnn"))
            continue

        m = _BARE_PART_RE.match(name)
        if m:
            # 裸命名分卷必须先于 _SINGLE_RE 判断，否则 part2.rar 会被误当成
            # 独立压缩包重复解压
            if int(m.group("num")) != 1:
                continue  # 只处理 part1/part01，后续卷跳过
            volumes = sorted(p for p in path.parent.iterdir()
                             if p.is_file() and _BARE_PART_RE.match(p.name))
            # 裸命名没有主名，"解压到子文件夹"时用所在文件夹名代替
            items.append(ArchiveItem(path, volumes, path.parent.name, "bare_part"))
            continue

        m = _PART_VOLUME_RE.match(name)
        if m:
            if int(m.group("num")) != 1:
                continue  # 只处理 part1，part2+ 跳过
            volumes = _collect_part_volumes(path, m.group("stem"))
            items.append(ArchiveItem(path, volumes, m.group("stem"), "part"))
            continue

        if _R_VOLUME_RE.match(name):
            continue  # 旧式分卷的 .rNN 后续卷，跳过（跟随主 .rar 处理）

        m = _SINGLE_RE.match(name)
        if m:
            stem = m.group("stem")
            if m.group("fmt").lower() == "rar":
                # 检查是否为旧式分卷主卷（同名 .r00 存在）
                r_vols = _collect_r_volumes(path, stem)
                if r_vols:
                    items.append(ArchiveItem(path, [path] + r_vols, stem, "old_rar"))
                    continue
            items.append(ArchiveItem(path, [path], stem, "single"))
    return items


def _collect_nnn_volumes(first: Path, stem: str, fmt: str) -> list[Path]:
    """收集 .7z.001 式分卷组的全部文件。"""
    pat = re.compile(rf"(?i)^{re.escape(stem)}\.{re.escape(fmt)}\.\d{{3}}$")
    return sorted(p for p in first.parent.iterdir() if pat.match(p.name))


def _collect_part_volumes(first: Path, stem: str) -> list[Path]:
    """收集 .partN.rar 式分卷组的全部文件。"""
    pat = re.compile(rf"(?i)^{re.escape(stem)}\.part\d+\.rar$")
    return sorted(p for p in first.parent.iterdir() if pat.match(p.name))


def _collect_r_volumes(main_rar: Path, stem: str) -> list[Path]:
    """收集旧式分卷的 .r00 .r01 ... 后续卷（不含主 .rar）。"""
    pat = re.compile(rf"(?i)^{re.escape(stem)}\.r\d{{2}}$")
    return sorted(p for p in main_rar.parent.iterdir() if pat.match(p.name))


# ---------- 单个压缩包的完整处理流程 ----------

def extract_one(
    item: ArchiveItem,
    config: Config,
    sz: SevenZip,
    log: Callable[[str, str], None],
    file_progress: Optional[Callable[[int], None]] = None,
) -> ExtractRecord:
    """处理一个压缩包：密码尝试 → 解压 → 可选删除原包。

    log(msg, tag) 推送日志；file_progress(percent) 推送当前文件进度。
    """
    archive = item.main_file
    start = time.time()

    # 边界：路径过长直接跳过
    if is_path_too_long(archive):
        log(f"[跳过] 路径超过 Windows 260 字符限制：{archive}", "warn")
        return ExtractRecord(str(archive), "跳过", "路径过长", elapsed=time.time() - start)

    # 确定解压目标目录
    out_dir = archive.parent / item.stem if config.data["extract_to_subfolder"] else archive.parent
    if is_path_too_long(out_dir):
        log(f"[跳过] 目标路径过长：{out_dir}", "warn")
        return ExtractRecord(str(archive), "跳过", "目标路径过长", elapsed=time.time() - start)

    # 密码尝试顺序：先无密码，再按命中次数降序
    candidates = [""] + [p["password"] for p in config.sorted_passwords()]
    hit_password: Optional[str] = None
    last_result: Optional[SevenZipResult] = None

    for pwd in candidates:
        label = "无密码" if pwd == "" else f"密码「{pwd}」"
        log(f"    测试 {label} ...", "info")
        result = sz.test(archive, pwd, progress_cb=file_progress)
        last_result = result
        if result.ok:
            hit_password = pwd
            log(f"    {label} 验证通过", "info")
            break
        # 分卷不完整/根本不是压缩文件时，继续试其他密码没有意义，提前结束。
        # 注意 CRC/数据错误不提前结束——加密压缩包密码不对时也会报这类错误。
        reason = SevenZip.classify_failure(result.output, archive)
        if "分卷" in reason or "无法识别" in reason:
            log(f"    测试失败：{reason}", "warn")
            break

    if hit_password is None:
        reason = SevenZip.classify_failure(last_result.output if last_result else "", archive)
        log(f"[失败] {archive.name}：{reason}", "error")
        return ExtractRecord(str(archive), "失败", reason, elapsed=time.time() - start)

    # 覆盖标注：目标目录已有文件时提示 -y 会直接覆盖重名文件
    if out_dir.exists() and any(out_dir.iterdir()):
        log(f"    注意：目标目录已有文件，重名文件将被 -y 覆盖：{out_dir}", "warn")

    # 真正解压
    if file_progress:
        file_progress(0)
    result = sz.extract(archive, out_dir, hit_password, progress_cb=file_progress)
    if not result.ok:
        reason = SevenZip.classify_failure(result.output, archive)
        log(f"[失败] {archive.name}：解压阶段出错（{reason}）", "error")
        return ExtractRecord(str(archive), "失败", f"解压阶段：{reason}",
                             password=hit_password, elapsed=time.time() - start)

    # 命中计数 +1（无密码不计）
    if hit_password:
        config.record_hit(hit_password)

    # 可选：删除原压缩包（含分卷全部文件）
    deleted_note = ""
    if config.data["delete_after_extract"]:
        failed_del = []
        for vol in item.volume_files:
            try:
                vol.unlink()
            except OSError as e:
                failed_del.append(f"{vol.name}({e})")
        if failed_del:
            deleted_note = f"；部分原文件删除失败：{', '.join(failed_del)}"
            log(f"    原压缩包删除失败：{', '.join(failed_del)}", "warn")
        else:
            deleted_note = f"；已删除原压缩包 {len(item.volume_files)} 个文件"
            log(f"    已删除原压缩包（{len(item.volume_files)} 个文件）", "info")

    elapsed = time.time() - start
    pwd_note = f"（密码：{hit_password}）" if hit_password else "（无密码）"
    log(f"[成功] {archive.name} → {out_dir} {pwd_note}，耗时 {elapsed:.1f}s{deleted_note}", "success")
    return ExtractRecord(str(archive), "成功", deleted_note.lstrip("；"),
                         password=hit_password, elapsed=elapsed)
