# -*- coding: utf-8 -*-
"""7z.exe 子进程封装：密码测试(t)、解压(x)、-bsp1 实时进度解析、失败原因分类。"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

# 隐藏子进程控制台窗口（仅 Windows）
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# -bsp1 进度输出形如 " 23% 12 - filename"，7z 用 \b 退格原地刷新
_PERCENT_RE = re.compile(r"(\d{1,3})%")


class SevenZipResult:
    """一次 7z 调用的结果。"""

    def __init__(self, returncode: int, output: str):
        self.returncode = returncode
        self.output = output

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SevenZip:
    def __init__(self, exe_path: str):
        self.exe_path = exe_path

    def available(self) -> bool:
        return Path(self.exe_path).is_file()

    # ---------- 基础调用 ----------

    def _run(
        self,
        args: list[str],
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> SevenZipResult:
        """执行 7z 并逐字符读取输出流以解析实时百分比。

        关键点：
        - stdin=DEVNULL：防止 7z 遇到需要密码/确认时交互等待导致挂死
        - -sccUTF-8：强制 7z 控制台输出用 UTF-8，保证中文文件名不乱码
        - 7z 的 -bsp1 进度用 \b/\r 原地刷新，不能按行读，需按字符读并把
          退格/回车视作换行来切分
        """
        cmd = [self.exe_path] + args + ["-sccUTF-8"]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            return SevenZipResult(-1, f"无法启动 7z.exe：{e}")

        lines: list[str] = []
        buf = ""
        while True:
            ch = proc.stdout.read(1)
            if ch == "":
                break
            if ch in ("\b", "\r"):
                ch = "\n"
            if ch == "\n":
                line = buf.strip()
                buf = ""
                if not line:
                    continue
                m = _PERCENT_RE.search(line)
                if m and progress_cb:
                    progress_cb(min(100, int(m.group(1))))
                lines.append(line)
                # 防止超大压缩包的海量进度行占用内存，只保留头尾
                if len(lines) > 400:
                    lines = lines[:100] + lines[-200:]
            else:
                buf += ch
        if buf.strip():
            lines.append(buf.strip())
        proc.wait()
        return SevenZipResult(proc.returncode, "\n".join(lines))

    # ---------- 对外操作 ----------

    def test(
        self,
        archive: Path,
        password: str = "",
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> SevenZipResult:
        """7z t 测试模式验证密码/完整性。password 为空串即"无密码尝试"。

        注意必须始终传 -p 参数（即使密码为空），否则 7z 会弹出交互式
        密码输入而卡住整个流程。
        """
        return self._run(["t", str(archive), f"-p{password}", "-bsp1"], progress_cb)

    def extract(
        self,
        archive: Path,
        out_dir: Path,
        password: str = "",
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> SevenZipResult:
        """7z x 完整路径解压。-y 表示重名文件直接覆盖（调用方负责在日志标注）。"""
        return self._run(
            ["x", str(archive), f"-o{out_dir}", f"-p{password}", "-y", "-bsp1"],
            progress_cb,
        )

    # ---------- 失败原因分类 ----------

    @staticmethod
    def classify_failure(output: str, archive: Path) -> str:
        """根据 7z 输出和磁盘上的分卷情况，给出人类可读的失败原因。

        分类优先级：缺分卷 > 密码错误 > 无法识别 > 分卷数据错误 > 损坏。
        对 .NNN 分卷报数据错误时，结合磁盘上同名分卷数量辅助判断：
        分卷缺失（如只有 .001 没有 .002）与真正损坏在 7z 输出上难以区分，
        因此按需求标注为「疑似分卷不完整」而非单纯的损坏/密码错误。
        """
        low = output.lower()
        if "missing volume" in low:
            return "疑似分卷不完整（缺少后续分卷）"
        if "wrong password" in low:
            return "密码错误（密码池全部未命中）"
        if "cannot open the file as archive" in low or "is not archive" in low:
            return "压缩包损坏（无法识别为压缩文件）"
        if "crc failed" in low or "data error" in low or "unexpected end" in low:
            m = _VOLUME_NNN_RE.match(archive.name)
            if m:
                nums = _existing_volume_nums(archive, m.group("base"))
                if nums != list(range(1, len(nums) + 1)):
                    return f"疑似分卷不完整（分卷编号不连续，已找到 {len(nums)} 卷）"
                return (f"疑似分卷不完整（已找到 {len(nums)} 卷但数据校验失败，"
                        "可能缺少后续分卷或分卷损坏）")
            return "压缩包损坏（数据错误/CRC 校验失败）"
        return "未知错误"


_VOLUME_NNN_RE = re.compile(r"^(?P<base>.+)\.(?P<num>\d{3})$")


def _existing_volume_nums(archive: Path, base: str) -> list[int]:
    """列出磁盘上同名 .NNN 分卷的编号（升序）。
    不用 glob：游戏文件名常含 [] 等 glob 特殊字符，改用 iterdir + 正则匹配。"""
    return sorted(
        int(mm.group("num"))
        for p in archive.parent.iterdir()
        if (mm := _VOLUME_NNN_RE.match(p.name)) and mm.group("base") == base
    )
