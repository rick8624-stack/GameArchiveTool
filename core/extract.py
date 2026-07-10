# -*- coding: utf-8 -*-
"""模块二：批量解压（核心功能）。

- 递归扫描压缩文件，识别分卷（只处理首卷，后续卷跳过由 7z 自动串联）
- 密码池机制：先无密码，再按命中次数降序逐个用 7z t 验证，通过才真正解压
- 解压成功后可选删除原压缩包（含分卷全部文件）
- 失败不中断整体流程，记入失败清单
"""

import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.config import Config
from core.sevenzip import ListResult, SevenZip
from core.unrar import UnRar
from utils import is_path_too_long

# 可选依赖：装了 send2trash 则"删除原压缩包"进回收站（可恢复），
# 没装则回退为永久删除
try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

# 磁盘空间预检的安全余量：剩余空间需大于「未压缩总大小 + 此值」
_FREE_SPACE_MARGIN = 64 * 1024 * 1024

# 嵌套解压最大层数（压缩包里套压缩包），防止无限套娃/压缩炸弹
MAX_NESTED_DEPTH = 4

# ---------- 压缩包识别 ----------

# .7z.001 / .zip.001 / .rar.001 数字分卷
_NNN_VOLUME_RE = re.compile(r"(?i)^(?P<stem>.+)\.(?P<fmt>7z|zip|rar)\.(?P<num>\d{3})$")
# .part1.rar / .part01.rar 分卷（带主名，如 游戏.part1.rar）
_PART_VOLUME_RE = re.compile(r"(?i)^(?P<stem>.+)\.part(?P<num>\d+)\.rar$")
# 裸命名分卷：整个文件名就是 part1.rar / part01.rar（常见于一个游戏一个文件夹）
_BARE_PART_RE = re.compile(r"(?i)^part(?P<num>\d+)\.rar$")
# 旧式 rar 分卷的后续卷 .r00 .r01 ...
_R_VOLUME_RE = re.compile(r"(?i)^(?P<stem>.+)\.r\d{2}$")
# 旧式 zip 分卷的后续卷 .z01 .z02 ...（主卷是同名 .zip）
_Z_VOLUME_RE = re.compile(r"(?i)^(?P<stem>.+)\.z\d{2}$")
# 无格式中缀的纯数字分卷（HJSplit 风格：游戏.001 / 游戏.002）
_PLAIN_NNN_RE = re.compile(r"^(?P<stem>.+)\.(?P<num>\d{3})$")
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
    out_dir: str = ""          # 实际解压目标目录（成功时非空，嵌套扫描用）
    op: str = "解压"           # 操作类型：解压 / 扩展名修正


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

        if _Z_VOLUME_RE.match(name):
            continue  # 旧式 zip 分卷的 .zNN 后续卷，跳过（跟随主 .zip 处理）

        m = _SINGLE_RE.match(name)
        if m:
            stem = m.group("stem")
            fmt = m.group("fmt").lower()
            if fmt == "rar":
                # 检查是否为旧式分卷主卷（同名 .r00 存在）
                r_vols = _collect_r_volumes(path, stem)
                if r_vols:
                    items.append(ArchiveItem(path, [path] + r_vols, stem, "old_rar"))
                    continue
            elif fmt == "zip":
                # 检查是否为旧式 zip 分卷主卷（同名 .z01 存在）
                z_vols = _collect_z_volumes(path, stem)
                if z_vols:
                    items.append(ArchiveItem(path, [path] + z_vols, stem, "old_zip"))
                    continue
            items.append(ArchiveItem(path, [path], stem, "single"))
            continue

        # HJSplit 风格纯数字分卷（游戏.001，无 .7z/.zip 中缀）。
        # 必须放在最后判断：带格式中缀的 .001 已被 _NNN_VOLUME_RE 消费
        m = _PLAIN_NNN_RE.match(name)
        if m:
            if int(m.group("num")) != 1:
                continue  # 后续卷跳过
            volumes = _collect_plain_volumes(path, m.group("stem"))
            items.append(ArchiveItem(path, volumes, m.group("stem"), "plain_nnn"))
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


def _collect_z_volumes(main_zip: Path, stem: str) -> list[Path]:
    """收集旧式 zip 分卷的 .z01 .z02 ... 后续卷（不含主 .zip）。"""
    pat = re.compile(rf"(?i)^{re.escape(stem)}\.z\d{{2}}$")
    return sorted(p for p in main_zip.parent.iterdir() if pat.match(p.name))


def _collect_plain_volumes(first: Path, stem: str) -> list[Path]:
    """收集纯数字分卷组（游戏.001 / .002 ...）的全部文件。"""
    pat = re.compile(rf"^{re.escape(stem)}\.\d{{3}}$")
    return sorted(p for p in first.parent.iterdir() if pat.match(p.name))


# ---------- 智能识别伪装扩展名 ----------

# 压缩格式文件头魔数
_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),      # rar4 与 rar5 前 7 字节相同
    (b"PK\x03\x04", "zip"),
)

# 本质是 zip 但不该被当压缩包处理的容器格式（改了扩展名反而破坏文件）：
# 办公文档/程序包等通用容器，以及游戏存档、游戏引擎资源包等游戏相关格式
_ZIP_CONTAINER_EXTS = {
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
    ".jar", ".war", ".apk", ".ipa", ".epub", ".xpi",
    ".whl", ".nupkg", ".vsix", ".cbz", ".aab",
    ".save", ".sav", ".pak", ".pk3", ".pk4", ".vpk",
}

# 伪装识别的默认最小文件大小（MB）：游戏存档等小文件常用 zip 压缩存储，
# 会被魔数误判；真正需要解压的游戏压缩包远大于此
DEFAULT_SMART_FIX_MIN_MB = 1.0

_ALL_ARCHIVE_NAME_RES = (_NNN_VOLUME_RE, _BARE_PART_RE, _PART_VOLUME_RE,
                         _R_VOLUME_RE, _Z_VOLUME_RE, _PLAIN_NNN_RE, _SINGLE_RE)

# 任意长度数字后缀（.1 .01 .001 .0001 ...）——分卷成员的通用形态。
# 注意：这比 _PLAIN_NNN_RE 更宽（不限 3 位），仅用于"绝不改名"的保护性判断，
# 避免把两位数/四位数分卷的首卷误当独立文件追加扩展名而拆散分卷。
_NUMERIC_SUFFIX_RE = re.compile(r"^(?P<stem>.+)\.(?P<num>\d+)$")

# 被错误追加了压缩扩展名的分卷首卷，如 game.001.7z / game.exe.001.rar
_APPENDED_EXT_RE = re.compile(r"(?i)^(?P<base>.+)\.(?P<num>\d+)\.(?P<ext>7z|zip|rar)$")


def sniff_archive_format(path: Path) -> Optional[str]:
    """读文件头魔数判断是否为压缩文件，返回 '7z'/'rar'/'zip' 或 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    for magic, fmt in _MAGICS:
        if head.startswith(magic):
            return fmt
    return None


def _numeric_volume_bases(files: list[Path]) -> set[str]:
    """收集同目录内所有"数字分卷组"的基名。

    基名取数字后缀之前的部分（game.001 → game、game.7z.002 → game.7z）。
    这些基名下的成员、以及与基名同名的 SFX 首卷（game.exe 对 game.001）
    都属于分卷组，伪装修正一律不得触碰。
    """
    bases: set[str] = set()
    for f in files:
        m = _NUMERIC_SUFFIX_RE.match(f.name)
        if m:
            bases.add(m.group("stem"))
    return bases


def _belongs_to_volume_set(path: Path, vol_bases: set[str]) -> bool:
    """判断某文件是否属于分卷组（分卷成员本身，或 SFX 首卷）。

    - 自身带数字后缀：game.001 / game.7z.002 → 是
    - 自身去掉最后一段扩展名后等于某分卷基名：game.exe（旁边有 game.001）→ 是
      （SFX 自解压包的首卷常是 .exe，其余卷是 .001/.002）
    """
    if _NUMERIC_SUFFIX_RE.match(path.name):
        return True
    # 去掉最后一段扩展名后与分卷基名相同 → SFX 首卷
    stem_wo_ext = path.name[: -len(path.suffix)] if path.suffix else path.name
    return stem_wo_ext in vol_bases


def restore_appended_volume_ext(
    root: Path,
    log: Callable[[str, str], None],
) -> list[tuple[Path, Path]]:
    """修复此前被错误追加了压缩扩展名的分卷首卷。

    历史/异常情况下 game.001 可能被误改成 game.001.7z，拆散了分卷。
    当同目录存在该分卷组的其他成员（如 game.002 或 game.002.7z）时，
    去掉多余的尾部扩展名还原为 game.001，使 7z 能重新按分卷串联。
    返回 [(原路径, 还原路径), ...]。
    """
    restored: list[tuple[Path, Path]] = []
    for d in {p.parent for p in root.rglob("*") if p.is_file()} | {root}:
        try:
            files = [p for p in d.iterdir() if p.is_file()]
        except OSError:
            continue
        names = {p.name for p in files}
        for f in files:
            m = _APPENDED_EXT_RE.match(f.name)
            if not m:
                continue
            base, num = m.group("base"), int(m.group("num"))
            # 确认这是分卷：存在相邻卷号的兄弟文件（去不去多余扩展名都算）
            sib_next = f"{base}.{num + 1:0{len(m.group('num'))}d}"
            has_sibling = any(
                n == sib_next or n.startswith(sib_next + ".") or
                (n != f.name and _NUMERIC_SUFFIX_RE.match(n) and
                 n.rsplit(".", 1)[0] == base)
                for n in names
            )
            if not has_sibling:
                continue
            target = f.with_name(f"{base}.{m.group('num')}")
            if target.exists():
                continue
            try:
                f.rename(target)
            except OSError as e:
                log(f"[分卷修复] 失败 {f.name}：{e}", "error")
                continue
            log(f"[分卷修复] {f.name} → {target.name}（还原被拆散的分卷首卷）", "success")
            restored.append((f, target))
    return restored


def fix_disguised_extensions(
    root: Path,
    log: Callable[[str, str], None],
    min_size: int = int(DEFAULT_SMART_FIX_MIN_MB * 1024 * 1024),
) -> list[tuple[Path, Path]]:
    """扫描 root，找出「内容是压缩文件但扩展名不对」的伪装文件并修正扩展名。

    修正方式为在原名后追加正确扩展名（游戏.jpg → 游戏.jpg.rar），
    不破坏原名信息且保证后续能被压缩包识别规则命中。

    分卷感知（关键）：任何属于数字分卷组的文件——分卷成员本身、以及与
    分卷同基名的 SFX 首卷（game.exe 对 game.001）——一律不改名。否则会把
    分卷首卷变成独立压缩包，导致 7z 无法串联后续卷（这是本函数的核心约束）。
    另外不动：已能按名字识别的压缩包；docx/存档/资源包等 zip 容器格式；
    小于 min_size 的文件（游戏存档常以 zip 存储，会被魔数误判）。
    返回 [(原路径, 新路径), ...]。
    """
    fixed: list[tuple[Path, Path]] = []
    # 按目录分组，先算出每个目录里的分卷基名
    dirs: dict[Path, list[Path]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            dirs.setdefault(path.parent, []).append(path)
    for d, files in dirs.items():
        vol_bases = _numeric_volume_bases(files)
        for path in files:
            name = path.name
            if any(r.match(name) for r in _ALL_ARCHIVE_NAME_RES):
                continue  # 名字已可识别（含分卷后续卷），不需要修正
            if _belongs_to_volume_set(path, vol_bases):
                continue  # 分卷成员 / SFX 首卷：改名会拆散分卷，绝不触碰
            if path.suffix.lower() in _ZIP_CONTAINER_EXTS:
                continue  # zip 容器格式，内容是 PK 头属正常，不能改名
            if min_size > 0:
                try:
                    if path.stat().st_size < min_size:
                        continue  # 小文件大概率是存档/配置等，不做伪装识别
                except OSError:
                    continue
            fmt = sniff_archive_format(path)
            if fmt is None:
                continue
            new_path = path.with_name(f"{name}.{fmt}")
            if new_path.exists():
                log(f"[扩展名修正] 跳过 {name}：目标 {new_path.name} 已存在", "warn")
                continue
            try:
                path.rename(new_path)
            except OSError as e:
                log(f"[扩展名修正] 失败 {name}：{e}", "error")
                continue
            log(f"[扩展名修正] {name} → {new_path.name}（内容为 {fmt} 格式）", "success")
            fixed.append((path, new_path))
    return fixed


# ---------- 单个压缩包的完整处理流程 ----------

def extract_one(
    item: ArchiveItem,
    config: Config,
    sz: SevenZip,
    log: Callable[[str, str], None],
    file_progress: Optional[Callable[[int], None]] = None,
    preferred_passwords: Optional[list[str]] = None,
    dest_parent: Optional[Path] = None,
) -> ExtractRecord:
    """处理一个压缩包：密码尝试 → 解压 → 可选删除原包。

    log(msg, tag) 推送日志；file_progress(percent) 推送当前文件进度。
    preferred_passwords 优先于密码池尝试（批次内密码局部性：同一来源的
    游戏包通常共用密码，上一个包命中的密码先试能省掉大量无效测试）。
    dest_parent 指定解压基准目录（目标解压路径功能），None 为压缩包所在目录。
    """
    archive = item.main_file
    start = time.time()
    base = dest_parent if dest_parent is not None else archive.parent
    skip_existing = config.data.get("skip_existing_folder", True)

    # 边界：路径过长直接跳过
    if is_path_too_long(archive):
        log(f"[跳过] 路径超过 Windows 260 字符限制：{archive}", "warn")
        return ExtractRecord(str(archive), "跳过", "路径过长", elapsed=time.time() - start)

    # 跳过模式（一）：子文件夹模式下，目标同名文件夹已存在且非空 → 视为已解压过。
    # 在密码尝试之前检查，省掉整个测试开销
    if skip_existing and config.data["extract_to_subfolder"]:
        tgt = base / item.stem
        if tgt.is_dir() and any(tgt.iterdir()):
            log(f"[跳过] 同名文件夹已存在：{tgt}", "warn")
            return ExtractRecord(str(archive), "跳过", "同名文件夹已存在（跳过模式）",
                                 elapsed=time.time() - start)

    # 密码尝试顺序：无密码 → 批次内最近命中 → 密码池按命中次数降序（去重）
    candidates = [""]
    for pwd in (preferred_passwords or []):
        if pwd and pwd not in candidates:
            candidates.append(pwd)
    for entry in config.sorted_passwords():
        if entry["password"] not in candidates:
            candidates.append(entry["password"])

    hit_password: Optional[str] = None
    listing: Optional[ListResult] = None
    last_output = ""

    for pwd in candidates:
        label = "无密码" if pwd == "" else f"密码「{pwd}」"
        # 第一道闸：7z l 只读文件头，头部加密的包密码不对时立即失败，
        # 免去对大文件做整包 t 测试的开销
        lr = sz.list_archive(archive, pwd)
        if not lr.ok:
            last_output = lr.output
            reason = SevenZip.classify_failure(lr.output, archive)
            if "分卷" in reason or "无法识别" in reason:
                log(f"    快速检查失败：{reason}", "warn")
                break
            log(f"    {label} 快速验证未通过", "info")
            continue
        # 跳过模式（二）：包内唯一顶层文件夹在目标位置已存在且非空 → 视为已解压过
        if skip_existing and lr.single_top_dir:
            tgt = base / lr.single_top_dir
            if tgt.is_dir() and any(tgt.iterdir()):
                log(f"[跳过] 包内顶层文件夹已存在于目标位置：{tgt}", "warn")
                return ExtractRecord(str(archive), "跳过",
                                     "包内顶层文件夹已存在（跳过模式）",
                                     elapsed=time.time() - start)
        # 第二道闸：7z t 完整校验数据（非头部加密的包 l 会误通过）
        log(f"    测试 {label} ...", "info")
        result = sz.test(archive, pwd, progress_cb=file_progress)
        if result.ok:
            hit_password = pwd
            listing = lr
            log(f"    {label} 验证通过", "info")
            break
        last_output = result.output
        # 分卷不完整/根本不是压缩文件时，继续试其他密码没有意义，提前结束。
        # 注意 CRC/数据错误不提前结束——加密压缩包密码不对时也会报这类错误。
        reason = SevenZip.classify_failure(result.output, archive)
        if "分卷" in reason or "无法识别" in reason:
            log(f"    测试失败：{reason}", "warn")
            break

    # WinRAR 引擎回退（一）：7z 密码测试全部失败且目标是 rar 系文件时，
    # 用 UnRAR 重试密码池。部分 WinRAR 新版本生成的加密 rar，7z 无法解压但 UnRAR 可以
    unrar_engine: Optional[UnRar] = None
    if hit_password is None:
        unrar_engine, fb_pwd = _try_unrar_fallback(
            archive, config, candidates, log, file_progress,
            reason="7z 全部密码尝试失败")
        if unrar_engine is not None:
            hit_password = fb_pwd

    if hit_password is None:
        reason = SevenZip.classify_failure(last_output, archive)
        log(f"[失败] {archive.name}：{reason}", "error")
        return ExtractRecord(str(archive), "失败", reason, elapsed=time.time() - start)

    # 确定解压目标目录；包内已有唯一顶层文件夹时不再套子文件夹（避免 游戏A/游戏A/ 双重嵌套）
    use_subfolder = config.data["extract_to_subfolder"]
    if use_subfolder and listing and listing.single_top_dir:
        log(f"    包内已有唯一顶层文件夹「{listing.single_top_dir}」，"
            "直接解压到当前目录（避免双重嵌套）", "info")
        use_subfolder = False
    out_dir = base / item.stem if use_subfolder else base
    if is_path_too_long(out_dir):
        log(f"[跳过] 目标路径过长：{out_dir}", "warn")
        return ExtractRecord(str(archive), "跳过", "目标路径过长", elapsed=time.time() - start)

    # 磁盘空间预检：剩余空间需大于未压缩总大小 + 安全余量
    if listing and listing.total_size:
        free = _free_space(base)
        if free is not None and free < listing.total_size + _FREE_SPACE_MARGIN:
            need_gb = listing.total_size / 1024 ** 3
            free_gb = free / 1024 ** 3
            reason = f"磁盘空间不足（需约 {need_gb:.1f} GB，剩余 {free_gb:.1f} GB）"
            log(f"[失败] {archive.name}：{reason}", "error")
            return ExtractRecord(str(archive), "失败", reason,
                                 password=hit_password, elapsed=time.time() - start)

    # 覆盖标注：目标目录已有文件时提示 -y 会直接覆盖重名文件
    if out_dir.exists() and any(out_dir.iterdir()):
        log(f"    注意：目标目录已有文件，重名文件将被 -y 覆盖：{out_dir}", "warn")

    # 真正解压（WinRAR 回退时用同一引擎，避免 7z 再次失败）
    if file_progress:
        file_progress(0)
    if unrar_engine is not None:
        result = unrar_engine.extract(archive, out_dir, hit_password,
                                      progress_cb=file_progress)
    else:
        result = sz.extract(archive, out_dir, hit_password, progress_cb=file_progress)

    # WinRAR 引擎回退（二）：7z 测试通过但解压阶段因格式问题失败时，
    # 同样要给 UnRAR 机会——命中的密码优先，其余候选兜底
    if not result.ok and unrar_engine is None:
        ordered = [hit_password] + [c for c in candidates if c != hit_password]
        fb_engine, fb_pwd = _try_unrar_fallback(
            archive, config, ordered, log, file_progress,
            reason="7z 解压阶段失败")
        if fb_engine is not None:
            unrar_engine = fb_engine
            hit_password = fb_pwd
            result = fb_engine.extract(archive, out_dir, hit_password,
                                       progress_cb=file_progress)

    if not result.ok:
        reason = SevenZip.classify_failure(result.output, archive)
        log(f"[失败] {archive.name}：解压阶段出错（{reason}）", "error")
        return ExtractRecord(str(archive), "失败", f"解压阶段：{reason}",
                             password=hit_password, elapsed=time.time() - start)

    # 命中计数 +1（无密码不计）
    if hit_password:
        config.record_hit(hit_password)

    # 可选：删除原压缩包（含分卷全部文件）。装了 send2trash 则进回收站可恢复
    deleted_note = ""
    if config.data["delete_after_extract"]:
        use_recycle = config.data.get("delete_to_recycle", True) and _send2trash is not None
        how = "回收站" if use_recycle else "永久删除"
        failed_del = []
        for vol in item.volume_files:
            try:
                if use_recycle:
                    _send2trash(str(vol))
                else:
                    vol.unlink()
            except OSError as e:
                failed_del.append(f"{vol.name}({e})")
        if failed_del:
            deleted_note = f"；部分原文件删除失败：{', '.join(failed_del)}"
            log(f"    原压缩包删除失败：{', '.join(failed_del)}", "warn")
        else:
            deleted_note = f"；已删除原压缩包 {len(item.volume_files)} 个文件（{how}）"
            log(f"    已删除原压缩包（{len(item.volume_files)} 个文件，{how}）", "info")

    elapsed = time.time() - start
    pwd_note = f"（密码：{hit_password}）" if hit_password else "（无密码）"
    engine_note = "［WinRAR 引擎］" if unrar_engine is not None else ""
    detail = deleted_note.lstrip("；")
    if unrar_engine is not None:
        detail = f"WinRAR 引擎回退；{detail}".rstrip("；")
    log(f"[成功] {archive.name} → {out_dir} {pwd_note}{engine_note}，"
        f"耗时 {elapsed:.1f}s{deleted_note}", "success")
    return ExtractRecord(str(archive), "成功", detail,
                         password=hit_password, elapsed=elapsed, out_dir=str(out_dir))


def _try_unrar_fallback(
    archive: Path,
    config: Config,
    candidates: list[str],
    log: Callable[[str, str], None],
    file_progress: Optional[Callable[[int], None]],
    reason: str,
) -> tuple[Optional[UnRar], str]:
    """尝试 WinRAR 引擎回退：逐个候选密码用 UnRAR 验证。

    只要 UnRAR 可用就无条件尝试——不再靠扩展名或魔数判断格式。原因：
    伪装包（rar 改名成 .7z/.zip/.001）7z 能读文件名却解不了数据，靠名字/
    魔数判断都可能漏掉；UnRAR 按内容识别，遇到真正非 rar 的内容会自行失败，
    无害。返回 (引擎, 命中密码)，UnRAR 不可用或全部失败返回 (None, "")。"""
    unrar = UnRar(config.data.get("winrar_path", ""))
    if not unrar.available():
        log("    未配置可用的 UnRAR，无法回退（请在详细设置中指定 UnRAR 路径）", "warn")
        return None, ""
    log(f"    {reason}，改用 WinRAR 引擎（UnRAR）重试...", "warn")
    for pwd in candidates:
        label = "无密码" if pwd == "" else f"密码「{pwd}」"
        log(f"    [WinRAR] 测试 {label} ...", "info")
        if unrar.test(archive, pwd, progress_cb=file_progress).ok:
            log(f"    [WinRAR] {label} 验证通过", "info")
            return unrar, pwd
    log("    [WinRAR] 全部候选密码也未通过", "warn")
    return None, ""


def _free_space(path: Path) -> Optional[int]:
    """查询 path 所在磁盘的剩余空间；path 不存在时向上找最近存在的祖先目录。"""
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


# ---------- 批量解压（含嵌套解压、目标路径、密码局部性） ----------

def extract_batch(
    config: Config,
    sz: SevenZip,
    log: Callable[[str, str], None],
    scan_root: Optional[Path] = None,
    items: Optional[list[ArchiveItem]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    file_progress: Optional[Callable[[int], None]] = None,
    total_progress: Optional[Callable[[int, int, str], None]] = None,
    on_record: Optional[Callable[[ExtractRecord], None]] = None,
    completed: Optional[set[str]] = None,
    mark_done: Optional[Callable[[str], None]] = None,
) -> tuple[list[ExtractRecord], list[ArchiveItem], bool]:
    """批量解压主循环（GUI worker 与将来的 CLI 共用）。

    - items 为 None 时从 scan_root 扫描（并先做伪装扩展名修正）；
      非 None 时只处理给定项（失败重试场景）
    - 嵌套解压：解压成功后扫描输出目录中新出现的压缩包，继续入队处理，
      最多 MAX_NESTED_DEPTH 层；用 seen 集合防止重复/循环
    - 目标解压路径：config["extract_target_dir"] 非空时，顶层压缩包按
      相对 scan_root 的目录结构解压到目标路径；嵌套项原地解压
    - completed / mark_done：断点续传的查询与落盘回调
    返回 (全部记录, 失败项列表, 是否被用户停止)。
    """
    records: list[ExtractRecord] = []
    failed_items: list[ArchiveItem] = []

    def emit(rec: ExtractRecord):
        records.append(rec)
        if on_record:
            on_record(rec)

    smart_fix = config.data.get("smart_ext_fix", True)
    try:
        smart_fix_min = int(float(config.data.get(
            "smart_fix_min_mb", DEFAULT_SMART_FIX_MIN_MB)) * 1024 * 1024)
    except (TypeError, ValueError):
        smart_fix_min = int(DEFAULT_SMART_FIX_MIN_MB * 1024 * 1024)
    if items is None:
        # 先修复被错误追加扩展名而拆散的分卷首卷（如 game.001.7z → game.001），
        # 让后续扫描能重新按分卷串联；再做伪装扩展名修正
        if smart_fix:
            for old, new in restore_appended_volume_ext(scan_root, log):
                emit(ExtractRecord(str(old), "成功", f"还原为 {new.name}",
                                   op="分卷修复"))
            for old, new in fix_disguised_extensions(scan_root, log, smart_fix_min):
                emit(ExtractRecord(str(old), "成功", f"修正为 {new.name}",
                                   op="扩展名修正"))
        log(f"—— 批量解压：扫描 {scan_root} ——", "info")
        items = find_archives(scan_root)
        log(f"共找到 {len(items)} 个压缩包（分卷已归并为首卷）", "info")

    target_raw = config.data.get("extract_target_dir", "").strip()
    target_root = Path(target_raw) if target_raw else None
    nested_enabled = config.data.get("nested_extract", True)

    queue = deque((it, 1) for it in items)          # (待处理项, 嵌套层级)
    seen = {str(it.main_file) for it in items}      # 防重复/防循环
    total = len(queue)
    done = 0
    last_hit = ""                                    # 批次内密码局部性
    stopped = False

    while queue:
        if should_stop and should_stop():
            log("已按用户请求停止", "warn")
            stopped = True
            break
        item, depth = queue.popleft()
        path_str = str(item.main_file)
        if total_progress:
            total_progress(done, total, item.main_file.name)

        # 断点续传：跳过上次已完成项
        if completed and path_str in completed:
            log(f"[跳过] 上次已完成：{item.main_file.name}", "info")
            emit(ExtractRecord(path_str, "跳过", "断点续传：上次已完成"))
            done += 1
            if total_progress:
                total_progress(done, total, "")
            continue

        # 目标解压路径：顶层项映射到 target/相对路径；嵌套项（已在输出树内）原地
        dest_parent = None
        if target_root is not None and scan_root is not None:
            try:
                rel = item.main_file.parent.relative_to(scan_root)
                dest_parent = target_root / rel
            except ValueError:
                dest_parent = None

        depth_note = f"（嵌套第 {depth} 层）" if depth > 1 else ""
        log(f"[{done + 1}/{total}] 处理 {item.main_file.name} {depth_note}", "info")
        rec = extract_one(item, config, sz, log,
                          file_progress=file_progress,
                          preferred_passwords=[last_hit] if last_hit else None,
                          dest_parent=dest_parent)
        emit(rec)

        if rec.result == "成功":
            if mark_done:
                mark_done(path_str)
            if rec.password:
                last_hit = rec.password
            # 嵌套解压：扫描输出目录中新出现的压缩包
            if nested_enabled and depth < MAX_NESTED_DEPTH and rec.out_dir:
                out = Path(rec.out_dir)
                if smart_fix:
                    for old, new in fix_disguised_extensions(out, log, smart_fix_min):
                        emit(ExtractRecord(str(old), "成功", f"修正为 {new.name}",
                                           op="扩展名修正"))
                new_items = [ni for ni in find_archives(out)
                             if str(ni.main_file) not in seen]
                for ni in new_items:
                    seen.add(str(ni.main_file))
                    queue.append((ni, depth + 1))
                total += len(new_items)
                if new_items:
                    log(f"    发现 {len(new_items)} 个嵌套压缩包，"
                        f"加入队列（第 {depth + 1} 层）", "info")
        elif rec.result == "失败":
            failed_items.append(item)

        done += 1
        if total_progress:
            total_progress(done, total, "")

    return records, failed_items, stopped
