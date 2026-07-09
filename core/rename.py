# -*- coding: utf-8 -*-
"""模块三：批量重命名（编号对照表）。

读取 CSV 对照表（两列：编号,英文名），把 `001.中文名` 式文件夹改为 `001.英文名`。
- 编号部分保留原样（含前导零），匹配时双方都去前导零（"001" 匹配 "1"）
- 支持字母编号（XB001、K001、KX01 等）
- 新名自动清除 Windows 非法字符
- 未匹配的文件夹列入待处理清单
"""

import csv
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from utils import sanitize_filename

# 重命名历史文件（放在根目录里，跟数据走），供回退使用
HISTORY_FILE = ".rename_history.json"

# 文件夹名格式：编号.名称（编号可带字母前缀，如 XB001）
_FOLDER_RE = re.compile(r"^(?P<code>[A-Za-z]*\d+)\.(?P<name>.+)$")
# 编号格式：可选字母前缀 + 数字
_CODE_RE = re.compile(r"^(?P<prefix>[A-Za-z]*)(?P<digits>\d+)$")


def normalize_code(code: str) -> Optional[str]:
    """编号归一化：字母前缀转大写 + 数字去前导零。

    "001" → "1"，"XB001" → "XB1"，"kx01" → "KX1"。
    不符合编号格式（如 CSV 表头"编号"）返回 None。
    """
    m = _CODE_RE.match(code.strip())
    if not m:
        return None
    return m.group("prefix").upper() + str(int(m.group("digits")))


def load_mapping(csv_path: Path) -> tuple[dict[str, str], list[str]]:
    """读取 CSV 对照表，返回 (归一化编号 → 英文名 字典, 警告列表)。

    用 utf-8-sig 读取以兼容 Excel 导出的带 BOM 文件；表头行（编号列
    无法归一化）自动跳过。
    """
    mapping: dict[str, str] = {}
    warnings: list[str] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row_num, row in enumerate(csv.reader(f), 1):
            if len(row) < 2 or not row[0].strip() or not row[1].strip():
                continue
            key = normalize_code(row[0])
            if key is None:
                if row_num > 1:  # 首行大概率是表头，不告警
                    warnings.append(f"第 {row_num} 行编号格式无法识别：{row[0]}")
                continue
            if key in mapping and mapping[key] != row[1].strip():
                warnings.append(f"第 {row_num} 行编号 {row[0]} 重复，以后出现的为准")
            mapping[key] = row[1].strip()
    return mapping, warnings


@dataclass
class FolderRenamePlan:
    path: Path
    new_name: str
    note: str = ""
    skip: bool = False
    result: str = ""   # 执行后回填：成功 / 失败 / 跳过（空=未执行）

    @property
    def new_path(self) -> Path:
        return self.path.with_name(self.new_name)


@dataclass
class RenameScan:
    plans: list[FolderRenamePlan] = field(default_factory=list)
    unmatched: list[Path] = field(default_factory=list)   # 待处理清单：未匹配到对照表


def build_plans(root: Path, mapping: dict[str, str]) -> RenameScan:
    """递归扫描所有 `编号.名称` 格式的文件夹，生成重命名计划。

    深层目录排在前面（bottom-up），保证先改子文件夹再改父文件夹，
    避免父目录改名后子目录路径失效。
    """
    scan = RenameScan()
    folders = [p for p in root.rglob("*") if p.is_dir()]
    # 按路径深度降序 → 自底向上重命名
    folders.sort(key=lambda p: len(p.parts), reverse=True)

    for folder in folders:
        m = _FOLDER_RE.match(folder.name)
        if not m:
            continue  # 不是"编号.名称"格式的文件夹，不参与本功能
        key = normalize_code(m.group("code"))
        english = mapping.get(key) if key else None
        if english is None:
            scan.unmatched.append(folder)
            continue
        # 编号部分保留原样（含前导零），只替换名称部分；清除非法字符
        new_name = f"{m.group('code')}.{sanitize_filename(english)}"
        if new_name == folder.name:
            continue  # 已经是目标名，无需处理
        plan = FolderRenamePlan(path=folder, new_name=new_name)
        if plan.new_path.exists():
            plan.skip = True
            plan.note = "目标文件夹已存在，跳过"
        scan.plans.append(plan)
    return scan


def build_default_plans(root: Path) -> RenameScan:
    """默认编号模式：把根目录下的一级子文件夹按名称排序，重命名为 1, 2, 3...

    不需要对照表；已经是目标编号的文件夹原样保留。"""
    scan = RenameScan()
    folders = sorted((p for p in root.iterdir() if p.is_dir()),
                     key=lambda p: p.name)
    for i, folder in enumerate(folders, 1):
        new_name = str(i)
        if folder.name == new_name:
            continue  # 已是目标名
        plan = FolderRenamePlan(path=folder, new_name=new_name)
        if plan.new_path.exists():
            plan.skip = True
            plan.note = "目标文件夹已存在，跳过"
        scan.plans.append(plan)
    return scan


# ---------- 重命名历史与回退 ----------

def _history_path(root: Path) -> Path:
    return root / HISTORY_FILE


def _load_history(root: Path) -> list[dict]:
    p = _history_path(root)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def record_history(root: Path, plans: list["FolderRenamePlan"]) -> int:
    """把本批成功的重命名（旧名/新名）追加写入历史文件，供回退。返回记录条数。"""
    entries = [{"old": str(p.path), "new": str(p.new_path)}
               for p in plans if p.result == "成功"]
    if not entries:
        return 0
    history = _load_history(root)
    history.append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "renames": entries,
    })
    try:
        _history_path(root).write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return 0
    return len(entries)


def undo_last(root: Path, log: Callable[[str, str], None]) -> tuple[int, int]:
    """回退最近一批重命名（新名 → 旧名，逆序执行保证父子目录顺序正确）。

    返回 (回退成功数, 失败数)；无历史时返回 (0, 0)。"""
    history = _load_history(root)
    if not history:
        log("没有可回退的重命名历史", "warn")
        return 0, 0
    batch = history.pop()
    restored = failed = 0
    for entry in reversed(batch["renames"]):
        new_path, old_path = Path(entry["new"]), Path(entry["old"])
        try:
            new_path.rename(old_path)
            restored += 1
            log(f"[回退] {new_path.name} → {old_path.name}", "success")
        except OSError as e:
            failed += 1
            log(f"[回退失败] {new_path}：{e}", "error")
    try:
        if history:
            _history_path(root).write_text(
                json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            _history_path(root).unlink(missing_ok=True)
    except OSError:
        pass
    log(f"—— 回退完成（{batch['time']} 那批）：成功 {restored}，失败 {failed} ——", "info")
    return restored, failed


@dataclass
class RenameResult:
    renamed: int = 0
    skipped: int = 0
    failed: int = 0


def execute_plans(
    plans: list[FolderRenamePlan],
    log: Callable[[str, str], None],
    should_stop: Optional[Callable[[], bool]] = None,
) -> RenameResult:
    """执行文件夹重命名计划。should_stop() 为 True 时停止后续处理。"""
    result = RenameResult()
    for plan in plans:
        if should_stop and should_stop():
            log("已按用户请求停止", "warn")
            break
        if plan.skip:
            result.skipped += 1
            plan.result = "跳过"
            log(f"[跳过] {plan.path.name}：{plan.note}", "warn")
            continue
        try:
            plan.path.rename(plan.new_path)
            result.renamed += 1
            plan.result = "成功"
            log(f"[重命名] {plan.path.name} → {plan.new_name}", "success")
        except OSError as e:
            result.failed += 1
            plan.result = "失败"
            plan.note = str(e)
            log(f"[失败] {plan.path.name}：{e}", "error")
    return result
