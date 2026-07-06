# -*- coding: utf-8 -*-
"""模块三：批量重命名（编号对照表）。

读取 CSV 对照表（两列：编号,英文名），把 `001.中文名` 式文件夹改为 `001.英文名`。
- 编号部分保留原样（含前导零），匹配时双方都去前导零（"001" 匹配 "1"）
- 支持字母编号（XB001、K001、KX01 等）
- 新名自动清除 Windows 非法字符
- 未匹配的文件夹列入待处理清单
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from utils import sanitize_filename

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
