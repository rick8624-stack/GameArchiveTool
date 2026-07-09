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
