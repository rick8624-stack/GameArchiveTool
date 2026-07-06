# -*- coding: utf-8 -*-
"""通用工具函数：路径处理、非法字符清理、pyinstaller 资源路径兼容。"""

import re
import sys
from pathlib import Path

# Windows 传统路径长度上限（MAX_PATH = 260，含结尾空字符，实际可用 259）
MAX_PATH_LEN = 259

# Windows 文件名非法字符
_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def app_dir() -> Path:
    """返回程序所在目录（config.json / progress.json 存放处）。

    pyinstaller 打包后 __file__ 指向临时解压目录（sys._MEIPASS，只读），
    可写的配置必须放在 exe 旁边，因此打包环境下用 sys.executable 定位。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def resource_path(relative: str) -> Path:
    """获取只读资源文件路径，兼容 pyinstaller 单文件模式（sys._MEIPASS）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).parent / relative


def sanitize_filename(name: str) -> str:
    """清除 Windows 文件名非法字符 \\/:*?"<>|，并去掉首尾空格和末尾的点。"""
    cleaned = _ILLEGAL_CHARS_RE.sub("", name)
    return cleaned.strip().rstrip(".")


def is_path_too_long(path: Path) -> bool:
    """判断路径是否超过 Windows 260 字符限制。"""
    return len(str(path)) > MAX_PATH_LEN
