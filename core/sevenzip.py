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


def run_streaming(cmd, progress_cb=None) -> "SevenZipResult":
    """执行外部解压引擎（7z / UnRAR 通用）并逐字符解析实时百分比。

    关键点：
    - stdin=DEVNULL：防止子进程遇到需要密码/确认时交互等待导致挂死
    - 进度用 \b/\r 原地刷新，不能按行读，需按字符读并把退格/回车视作换行
    """
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
        return SevenZipResult(-1, f"无法启动 {cmd[0]}：{e}")

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
    proc.stdout.close()
    proc.wait()
    return SevenZipResult(proc.returncode, "\n".join(lines))


class SevenZipResult:
    """一次 7z 调用的结果。"""

    def __init__(self, returncode: int, output: str):
        self.returncode = returncode
        self.output = output

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ListResult:
    """7z l 列表结果：用于快速密码验证、双重嵌套判断和磁盘空间预检。"""

    def __init__(self, ok: bool, output: str, top_names: set[str],
                 top_dirs: set[str], total_size: int, encrypted: bool = False):
        self.ok = ok
        self.output = output
        self.top_names = top_names     # 压缩包内的顶层条目名集合
        self.top_dirs = top_dirs       # 顶层条目中是文件夹的那些
        self.total_size = total_size   # 未压缩总大小（字节）
        self.encrypted = encrypted     # 包内存在加密条目（-slt 的 Encrypted = +）

    @property
    def single_top_dir(self) -> str | None:
        """包内是否只有唯一的顶层文件夹（用于避免解压双重嵌套）。"""
        if len(self.top_names) == 1:
            name = next(iter(self.top_names))
            if name in self.top_dirs:
                return name
        return None


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
        """执行 7z（-sccUTF-8 强制 UTF-8 控制台输出，保证中文文件名不乱码）。"""
        return run_streaming([self.exe_path] + args + ["-sccUTF-8"], progress_cb)

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

    def add(
        self,
        archive: Path,
        source: Path,
        password: str = "",
        level: int = 5,
        fmt: str = "7z",
        header_encrypt: bool = True,
        volume_size: str = "",
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> SevenZipResult:
        """7z a 压缩：把 source（文件夹或文件）打包进 archive。

        source 传文件夹本身时，包内顶层即该文件夹名，正好对上解压侧
        「唯一顶层文件夹」的约定。level 0–9（0=仅存储最快）；password 非空
        时加密，7z 格式可再开 -mhe 连文件名一起加密；volume_size 非空时分卷
        （如 "2g"/"700m"）。zip 不支持 -mhe，按格式分支。
        """
        args = ["a", f"-t{fmt}", str(archive), str(source),
                f"-mx={level}", "-y", "-bsp1"]
        if password:
            args.append(f"-p{password}")
            if fmt == "7z" and header_encrypt:
                args.append("-mhe=on")
        if volume_size:
            args.append(f"-v{volume_size}")
        return self._run(args, progress_cb)

    def list_archive(self, archive: Path, password: str = "") -> ListResult:
        """7z l -slt 快速列出压缩包内容（只读文件头，不解数据）。

        三个用途：
        1. 密码快速验证：头部加密的压缩包密码不对时立即失败，
           避免对大文件做整包 t 测试才发现密码错误
        2. 顶层条目集合：判断包内是否已有唯一顶层文件夹（避免双重嵌套）
        3. 未压缩总大小：解压前做磁盘空间预检

        注意：非头部加密的包用错误密码 l 也会成功，所以 l 通过只是
        必要条件，真正的密码确认仍靠 7z t。
        """
        cmd = [self.exe_path, "l", str(archive), f"-p{password}",
               "-slt", "-ba", "-sccUTF-8"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            return ListResult(False, f"无法启动 7z.exe：{e}", set(), set(), 0)

        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return ListResult(False, output, set(), set(), 0)

        # -slt 输出为「键 = 值」块，逐行提取 Path / Size / Attributes
        top_names: set[str] = set()
        top_dirs: set[str] = set()
        total_size = 0
        encrypted = False      # 包内是否存在加密条目（Encrypted = +）
        cur_path = ""          # 当前块的 Path（-slt 中 Path 总在块首）
        cur_is_top = False     # 当前块的条目是否是顶层条目
        for line in proc.stdout.splitlines():
            if line.startswith("Encrypted = ") and \
                    line[len("Encrypted = "):].strip() == "+":
                encrypted = True
            if line.startswith("Path = "):
                rel = line[len("Path = "):].strip()
                parts = re.split(r"[\\/]", rel, maxsplit=1)
                cur_path = parts[0]
                cur_is_top = len(parts) == 1
                if rel:
                    # 顶层名 = 第一个路径段（7z 在 Windows 上用反斜杠）
                    top_names.add(parts[0])
                    if not cur_is_top:
                        top_dirs.add(parts[0])  # 有子路径 → 顶层段必是文件夹
            elif line.startswith("Size = "):
                v = line[len("Size = "):].strip()
                if v.isdigit():
                    total_size += int(v)
            elif line.startswith("Folder = ") and cur_is_top:
                if line[len("Folder = "):].strip() == "+":
                    top_dirs.add(cur_path)
            elif line.startswith("Attributes = ") and cur_is_top:
                # 目录属性以 D 开头（如 "D_ drwxr-xr-x"）
                if line[len("Attributes = "):].strip().startswith("D"):
                    top_dirs.add(cur_path)
        # 成功时不保留原始输出（大压缩包的文件清单可能非常大，且只有
        # 失败时才需要拿输出做原因分类）
        return ListResult(True, "", top_names, top_dirs, total_size, encrypted)

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
