# -*- coding: utf-8 -*-
"""WinRAR / UnRAR.exe 封装：7z 解压 rar 失败时的回退引擎。

部分 WinRAR 新版本生成的加密 rar，7z 的 rar 实现无法正确解压
（表现为密码正确却报错），而 WinRAR 自家的 UnRAR 可以。
"""

from pathlib import Path
from typing import Callable, Optional

from core.sevenzip import SevenZipResult, run_streaming

# WinRAR 常见安装位置（UnRAR.exe 随 WinRAR 一起安装）
DEFAULT_UNRAR_PATHS = (
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
)


def detect_unrar() -> str:
    """探测 UnRAR.exe：返回第一个存在的默认路径，都不存在返回首个默认值。"""
    for p in DEFAULT_UNRAR_PATHS:
        if Path(p).is_file():
            return p
    return DEFAULT_UNRAR_PATHS[0]


class UnRar:
    def __init__(self, exe_path: str):
        self.exe_path = exe_path

    def available(self) -> bool:
        return bool(self.exe_path) and Path(self.exe_path).is_file()

    @staticmethod
    def _pw_arg(password: str) -> str:
        # -p- 表示"无密码且不弹交互提示"；有密码时 -p<pwd>
        return f"-p{password}" if password else "-p-"

    def test(
        self,
        archive: Path,
        password: str = "",
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> SevenZipResult:
        """unrar t 测试模式验证密码/完整性。"""
        return run_streaming(
            [self.exe_path, "t", self._pw_arg(password), "-y", str(archive)],
            progress_cb,
        )

    def extract(
        self,
        archive: Path,
        out_dir: Path,
        password: str = "",
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> SevenZipResult:
        """unrar x 完整路径解压，-o+ 覆盖重名文件（与 7z -y 行为一致）。"""
        out_dir.mkdir(parents=True, exist_ok=True)
        # 目标路径以反斜杠结尾，UnRAR 才把它当目录
        dest = str(out_dir).rstrip("\\/") + "\\"
        return run_streaming(
            [self.exe_path, "x", self._pw_arg(password), "-y", "-o+",
             str(archive), dest],
            progress_cb,
        )
