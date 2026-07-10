# -*- coding: utf-8 -*-
"""模块一：预处理（文件名清理）。

递归扫描根目录，找出文件名末尾带多余字符的文件（默认末尾"删"字），
生成「原名 → 新名」预览计划，用户确认后批量重命名。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class RenamePlan:
    """一条重命名计划。"""
    path: Path            # 原文件完整路径
    new_name: str         # 新文件名（不含目录）
    note: str = ""        # 备注（如冲突提示）
    skip: bool = False    # 是否因冲突等原因跳过
    result: str = ""      # 执行后回填：成功 / 失败 / 跳过（空=未执行）

    @property
    def new_path(self) -> Path:
        return self.path.with_name(self.new_name)


@dataclass
class PreprocessResult:
    renamed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def parse_rules(rules: str | list[str]) -> list[str]:
    """规则解析：字符串按 ; 或 ；分隔为多条规则（普通模式下规则本身
    因此不能包含分号，正则里可用 \\x3b 表示分号）。"""
    if isinstance(rules, str):
        parts = re.split(r"[;；]", rules)
    else:
        parts = rules
    return [p for p in (s.strip() for s in parts) if p]


def build_plans(root: Path, rules: str | list[str],
                use_regex: bool) -> tuple[list[RenamePlan], Optional[str]]:
    """扫描 root，按规则生成重命名计划。

    rules 为要去除的末尾字符（普通模式）或正则（正则模式，自动锚定到末尾），
    支持多条规则（; 分隔），按顺序依次应用到文件名末尾。
    返回 (计划列表, 错误信息)；规则非法时错误信息非 None。
    """
    rule_list = parse_rules(rules)
    if not rule_list:
        return [], "清理规则不能为空"

    patterns: list[re.Pattern] = []
    if use_regex:
        for rule in rule_list:
            try:
                # 自动加 (?:...)$ 锚定末尾，用户只需写要匹配的尾部内容
                patterns.append(re.compile(f"(?:{rule})$"))
            except re.error as e:
                return [], f"正则表达式「{rule}」无效：{e}"

    plans: list[RenamePlan] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        new_name = name
        # 多条规则按顺序各应用一次
        if use_regex:
            for pat in patterns:
                new_name = pat.sub("", new_name)
        else:
            for rule in rule_list:
                if new_name.endswith(rule):
                    new_name = new_name[: -len(rule)]
        # 去掉清理后残留的末尾空格（Windows 文件名不允许以空格结尾）
        new_name = new_name.rstrip()

        if new_name == name or not new_name:
            continue  # 不匹配规则，或去掉后缀后为空名，跳过

        plan = RenamePlan(path=path, new_name=new_name)
        if plan.new_path.exists():
            plan.skip = True
            plan.note = "目标文件已存在，跳过"
        plans.append(plan)
    return plans, None


# ---------- 一键预处理：统一同一文件夹内命名不一致的分卷 ----------

# 分卷命名形态：带格式中缀数字卷 / partN.rar / 纯数字卷（fmt 需先于 plain 匹配）
_VOLFIX_PATTERNS = (
    ("fmt", re.compile(r"(?i)^(?P<stem>.+?)\.(?P<mid>7z|zip|rar)\.(?P<num>\d{3})$")),
    ("part", re.compile(r"(?i)^(?P<stem>.+?)\.part(?P<num>\d+)\.rar$")),
    ("plain", re.compile(r"^(?P<stem>.+?)\.(?P<num>\d{3})$")),
)


def build_volume_fix_plans(root: Path) -> list[RenamePlan]:
    """找出同一文件夹内「同属一套但主名不一致」的分卷并生成统一命名计划。

    典型场景：下载器/网站给各卷加了不同的前后缀，如
    游戏A.7z.001 / 游戏A(1).7z.002 / 游戏A - 副本.7z.003 ——
    这样 7z 无法自动串联后续卷。以首卷的主名为准统一其余卷。

    判定条件刻意保守（宁可不动，不可改错）：
    - 同目录、同分卷类型（含格式中缀）归为一组
    - 组内卷号无重复（有重复说明是多套分卷混放，无法判断归属）
    - 卷号从 1 起连续（不连续可能缺卷或本就不是一套）
    - 主名至少有两种写法（全一致则无需处理）
    """
    plans: list[RenamePlan] = []
    dirs = [root] + [p for p in sorted(root.rglob("*")) if p.is_dir()]
    for d in dirs:
        try:
            files = [p for p in d.iterdir() if p.is_file()]
        except OSError:
            continue
        groups: dict[tuple[str, str], list] = {}
        for f in files:
            for kind, pat in _VOLFIX_PATTERNS:
                m = pat.match(f.name)
                if m:
                    mid = m.group("mid").lower() if kind == "fmt" else ""
                    groups.setdefault((kind, mid), []).append((f, m))
                    break
        for members in groups.values():
            stems = {m.group("stem") for _, m in members}
            if len(stems) < 2:
                continue
            nums = [int(m.group("num")) for _, m in members]
            if len(set(nums)) != len(nums):
                continue  # 卷号重复：多套分卷混放
            if sorted(nums) != list(range(1, len(nums) + 1)):
                continue  # 卷号不连续：可能缺卷或不是同一套
            # 以首卷（卷号最小）的主名为准
            canonical = min(members, key=lambda t: int(t[1].group("num")))[1].group("stem")
            for f, m in members:
                if m.group("stem") == canonical:
                    continue
                # 按匹配到的正则重建新名（m.re 即当初命中的那条模式）
                if m.re is _VOLFIX_PATTERNS[0][1]:
                    new_name = f"{canonical}.{m.group('mid')}.{m.group('num')}"
                elif m.re is _VOLFIX_PATTERNS[1][1]:
                    new_name = f"{canonical}.part{m.group('num')}.rar"
                else:
                    new_name = f"{canonical}.{m.group('num')}"
                plan = RenamePlan(path=f, new_name=new_name, note="统一分卷主名")
                if plan.new_path.exists():
                    plan.skip = True
                    plan.note = "目标文件已存在，跳过"
                plans.append(plan)
    return plans


def execute_plans(
    plans: list[RenamePlan],
    log: Callable[[str, str], None],
    should_stop: Optional[Callable[[], bool]] = None,
) -> PreprocessResult:
    """执行重命名计划。log(msg, tag) 用于向 UI 推送日志；
    should_stop() 返回 True 时在当前文件处理完后停止。"""
    result = PreprocessResult()
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
            result.errors.append(f"{plan.path}：{e}")
            plan.result = "失败"
            plan.note = str(e)
            log(f"[失败] {plan.path.name}：{e}", "error")
    return result
