# -*- coding: utf-8 -*-
"""config.json 读写：7z 路径、密码池（含命中计数）、上次根目录、各开关状态。"""

import copy
import json
from pathlib import Path

from utils import app_dir

CONFIG_FILE = "config.json"

# 默认配置。密码池条目格式：{"password": "xxx", "hits": 0}
DEFAULTS = {
    "seven_zip_path": r"C:\Program Files\7-Zip\7z.exe",
    "winrar_path": r"C:\Program Files\WinRAR\UnRAR.exe",  # 7z 解 rar 失败时的回退引擎
    "passwords": [],
    "last_root": "",
    "last_csv": "",
    "delete_after_extract": False,     # 解压成功后删除原压缩包（含全部分卷）
    "delete_to_recycle": True,         # 删除原压缩包时进回收站（需 send2trash，否则永久删除）
    "extract_to_subfolder": False,     # 解压到以压缩包名命名的子文件夹
    "extract_target_dir": "",          # 目标解压路径（留空=解压到压缩包所在目录）
    "skip_existing_folder": True,      # 跳过模式：目标位置已有同名文件夹时跳过该压缩包
    "force_extract": False,            # 强制解压：校验因分卷缺失/损坏失败时仍强制解压（结果可能不完整，不回退）
    "clean_before_extract": True,      # 批量解压前自动执行文件名清理
    "smart_ext_fix": True,             # 智能识别伪装扩展名（按文件头魔数修正）
    "smart_fix_min_mb": 1.0,           # 小于此大小(MB)的文件不做伪装识别（防游戏存档误判）
    "nested_extract": True,            # 嵌套解压（压缩包里的压缩包，最多 4 层）
    "rename_mode": "csv",              # 重命名模式：csv=对照表 / seq=默认编号 1,2,3...
    "preprocess_suffix": "删",         # 文件名清理规则，多条用 ; 分隔
    "preprocess_use_regex": False,     # 清理规则是否按正则解释
}


class Config:
    """配置管理器。load() 读取，save() 写回，属性直接读写 self.data。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / CONFIG_FILE)
        # 必须深拷贝：DEFAULTS 里的 passwords 是可变列表，浅拷贝会让
        # 所有 Config 实例共享同一个密码池对象
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                # 只覆盖已知键，保证新版本增加的默认键不丢失
                for key in DEFAULTS:
                    if key in loaded:
                        self.data[key] = loaded[key]
            except (json.JSONDecodeError, OSError):
                # 配置损坏时回退默认值，不让程序启动失败
                self.data = copy.deepcopy(DEFAULTS)

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass  # 配置写失败不应中断主流程

    # ---------- 密码池 ----------

    def sorted_passwords(self) -> list[dict]:
        """密码池按命中次数降序排列（命中多的优先尝试）。"""
        return sorted(self.data["passwords"], key=lambda p: p.get("hits", 0), reverse=True)

    def add_password(self, password: str) -> bool:
        """新增密码，重复则忽略。返回是否添加成功。"""
        password = password.strip()
        if not password or any(p["password"] == password for p in self.data["passwords"]):
            return False
        self.data["passwords"].append({"password": password, "hits": 0})
        self.save()
        return True

    def remove_password(self, password: str) -> None:
        self.data["passwords"] = [p for p in self.data["passwords"] if p["password"] != password]
        self.save()

    def update_password(self, old: str, new: str) -> bool:
        new = new.strip()
        if not new:
            return False
        for p in self.data["passwords"]:
            if p["password"] == old:
                p["password"] = new
                self.save()
                return True
        return False

    def record_hit(self, password: str) -> None:
        """密码命中一次，计数 +1 并持久化（下次排序自动前移）。"""
        for p in self.data["passwords"]:
            if p["password"] == password:
                p["hits"] = p.get("hits", 0) + 1
                break
        self.save()
