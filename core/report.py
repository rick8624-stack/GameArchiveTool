# -*- coding: utf-8 -*-
"""处理报告 CSV 导出：文件路径、操作类型、结果、使用的密码、耗时。"""

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReportRecord:
    path: str          # 文件/文件夹路径
    operation: str     # 预处理 / 解压 / 重命名
    result: str        # 成功 / 失败 / 跳过
    detail: str = ""   # 失败原因或备注
    password: str = "" # 解压使用的密码
    elapsed: float = 0.0


def export_csv(records: list[ReportRecord], out_path: Path) -> None:
    """导出报告。utf-8-sig 编码保证 Excel 直接打开中文不乱码。"""
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["文件路径", "操作类型", "结果", "详情", "使用的密码", "耗时(秒)"])
        for r in records:
            writer.writerow([r.path, r.operation, r.result, r.detail,
                             r.password, f"{r.elapsed:.1f}"])
