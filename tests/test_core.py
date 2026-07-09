# -*- coding: utf-8 -*-
"""核心模块测试（unittest，无第三方依赖）。

- 纯逻辑测试（预处理/识别/重命名/持久化）在任何环境都能跑
- 需要 7z.exe 的解压流程测试：找不到 7z 时自动跳过
  （GitHub Actions 的 windows-latest 运行器预装了 7-Zip）
- GUI 构建测试：无法创建 Tk 窗口的环境（无显示会话）自动跳过

运行：python -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 保证从任意工作目录运行时都能 import 项目模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from core import extract, preprocess
from core import rename as rename_mod
from core.config import Config
from core.progress_store import ProgressStore
from core.report import ReportRecord, export_csv
from core.sevenzip import SevenZip


def find_7z() -> str | None:
    """定位 7z.exe：默认安装路径或 PATH。"""
    default = Path(r"C:\Program Files\7-Zip\7z.exe")
    if default.is_file():
        return str(default)
    return shutil.which("7z")


SEVENZIP = find_7z()


class TempDirTestCase(unittest.TestCase):
    """基类：每个测试一个独立临时目录。"""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="gat_test_"))
        self.addCleanup(shutil.rmtree, self.work, True)

    @staticmethod
    def quiet_log(msg, tag):
        pass


# ================= 模块一：预处理 =================

class TestPreprocess(TempDirTestCase):
    def test_suffix_plans_and_execute(self):
        d = self.work / "子文件夹"
        d.mkdir()
        (d / "游戏A.7z.001删").write_text("x", encoding="utf-8")
        (d / "游戏A.7z.002删").write_text("x", encoding="utf-8")
        (d / "正常文件.zip").write_text("x", encoding="utf-8")
        (d / "冲突.txt删").write_text("x", encoding="utf-8")
        (d / "冲突.txt").write_text("x", encoding="utf-8")  # 目标已存在 → 跳过

        plans, err = preprocess.build_plans(self.work, "删", False)
        self.assertIsNone(err)
        self.assertEqual(len(plans), 3)
        self.assertEqual(sum(1 for p in plans if p.skip), 1)

        result = preprocess.execute_plans(plans, self.quiet_log)
        self.assertEqual((result.renamed, result.skipped, result.failed), (2, 1, 0))
        self.assertTrue((d / "游戏A.7z.001").exists())

    def test_regex_rule(self):
        (self.work / "b.7z[备份]").write_text("x", encoding="utf-8")
        plans, err = preprocess.build_plans(self.work, r"\[备份\]", True)
        self.assertIsNone(err)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].new_name, "b.7z")

    def test_invalid_regex_reports_error(self):
        _, err = preprocess.build_plans(self.work, "[无效", True)
        self.assertIsNotNone(err)

    def test_multiple_rules(self):
        """多条规则用 ; 分隔，按顺序依次应用；清理后残留的末尾空格一并去除。"""
        (self.work / "a.7z.001删").write_text("x", encoding="utf-8")
        (self.work / "b.zip.bak").write_text("x", encoding="utf-8")
        (self.work / "c.7z 删").write_text("x", encoding="utf-8")  # 删 前有空格
        plans, err = preprocess.build_plans(self.work, "删;.bak", False)
        self.assertIsNone(err)
        new_names = sorted(p.new_name for p in plans)
        self.assertEqual(new_names, ["a.7z.001", "b.zip", "c.7z"])


# ================= 模块二：压缩包/分卷识别（不需要 7z） =================

class TestArchiveDetection(TempDirTestCase):
    def touch(self, *names, sub=""):
        d = self.work / sub if sub else self.work
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"dummy")

    def find(self):
        return extract.find_archives(self.work)

    def test_nnn_volumes_only_first(self):
        self.touch("游戏.7z.001", "游戏.7z.002", "游戏.7z.003")
        items = self.find()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "nnn")
        self.assertEqual(items[0].main_file.name, "游戏.7z.001")
        self.assertEqual(len(items[0].volume_files), 3)
        self.assertEqual(items[0].stem, "游戏")

    def test_dotted_part_rar(self):
        self.touch("游戏B.part1.rar", "游戏B.part2.rar", "游戏B.part3.rar")
        items = self.find()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "part")
        self.assertEqual(len(items[0].volume_files), 3)

    def test_bare_part_rar_grouped_by_folder(self):
        """裸命名分卷：part1.rar/part2.rar 归为一组，只处理首卷。"""
        self.touch("part1.rar", "part2.rar", "part3.rar", sub="某游戏文件夹")
        items = self.find()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "bare_part")
        self.assertEqual(items[0].main_file.name, "part1.rar")
        self.assertEqual(len(items[0].volume_files), 3)
        self.assertEqual(items[0].stem, "某游戏文件夹")

    def test_bare_part01_two_digit(self):
        self.touch("part01.rar", "part02.rar")
        items = self.find()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].main_file.name, "part01.rar")

    def test_name_ending_with_part_is_single(self):
        """rampart1.rar 不是分卷，是独立压缩包。"""
        self.touch("rampart1.rar")
        items = self.find()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "single")

    def test_old_style_r00(self):
        self.touch("游戏C.rar", "游戏C.r00", "游戏C.r01")
        items = self.find()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "old_rar")
        self.assertEqual(len(items[0].volume_files), 3)

    def test_old_style_z01(self):
        """旧式 zip 分卷：主卷 .zip + 后续卷 .z01/.z02。"""
        self.touch("游戏D.zip", "游戏D.z01", "游戏D.z02")
        items = self.find()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "old_zip")
        self.assertEqual(items[0].main_file.name, "游戏D.zip")
        self.assertEqual(len(items[0].volume_files), 3)

    def test_plain_numeric_volumes(self):
        """HJSplit 风格纯数字分卷：游戏.001（无 .7z/.zip 中缀）。"""
        self.touch("游戏E.001", "游戏E.002", "游戏E.003")
        items = self.find()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "plain_nnn")
        self.assertEqual(items[0].main_file.name, "游戏E.001")
        self.assertEqual(len(items[0].volume_files), 3)
        self.assertEqual(items[0].stem, "游戏E")

    def test_plain_archives(self):
        self.touch("a.zip", "b.7z", "c.rar")
        items = self.find()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i.kind == "single" for i in items))

    def test_special_chars_in_filenames(self):
        """含 []、【】、()、空格 的文件名必须正确识别并归组。

        分卷收集用 iterdir + re.escape 而非 glob 模式，防止 [] 被当成
        glob 字符类导致同组分卷漏收（漏收会导致删除原包时留下孤儿分卷）。
        """
        # .NNN 分卷：主名含全部特殊字符
        self.touch("游戏[English]【中字】 v1.0.7z.001",
                   "游戏[English]【中字】 v1.0.7z.002",
                   "游戏[English]【中字】 v1.0.7z.003")
        # 带主名 rar 分卷
        self.touch("游戏[US] (Disc 1).part1.rar", "游戏[US] (Disc 1).part2.rar")
        # 旧式 .r00 分卷
        self.touch("游戏【日版】[v2].rar", "游戏【日版】[v2].r00", "游戏【日版】[v2].r01")
        # 特殊字符文件夹里的裸命名分卷
        self.touch("part1.rar", "part2.rar", sub="某游戏 [合集]【2024】")

        items = {i.kind: i for i in self.find()}
        self.assertEqual(len(items), 4)

        nnn = items["nnn"]
        self.assertEqual(nnn.main_file.name, "游戏[English]【中字】 v1.0.7z.001")
        self.assertEqual(len(nnn.volume_files), 3)
        self.assertEqual(nnn.stem, "游戏[English]【中字】 v1.0")

        self.assertEqual(len(items["part"].volume_files), 2)
        self.assertEqual(len(items["old_rar"].volume_files), 3)

        bare = items["bare_part"]
        self.assertEqual(len(bare.volume_files), 2)
        self.assertEqual(bare.stem, "某游戏 [合集]【2024】")


# ================= 模块二：伪装扩展名识别（不需要 7z） =================

class TestSniffDisguise(TempDirTestCase):
    def test_fix_disguised_extensions(self):
        import zipfile
        # 真 zip 伪装成 .jpg → 应修正为 .jpg.zip
        with zipfile.ZipFile(self.work / "伪装照片.jpg", "w") as z:
            z.writestr("a.txt", "x")
        # rar 魔数伪装成 .dat → 应修正为 .dat.rar
        (self.work / "假数据.dat").write_bytes(b"Rar!\x1a\x07\x00" + b"x" * 32)
        # docx 是 zip 容器格式 → 不能动
        (self.work / "文档.docx").write_bytes(b"PK\x03\x04" + b"x" * 32)
        # 普通文本 → 不动
        (self.work / "说明.txt").write_text("hello", encoding="utf-8")
        # 名字已可识别的压缩包 → 不动
        (self.work / "正常.zip").write_bytes(b"PK\x03\x04")
        # 分卷后续卷 → 不动（名字已被分卷规则覆盖）
        (self.work / "游戏.7z.002").write_bytes(b"7z\xbc\xaf\x27\x1c")

        # min_size=0：本用例只验证格式判断，大小门槛单独测
        fixed = extract.fix_disguised_extensions(self.work, self.quiet_log, min_size=0)
        names = sorted(p.name for p in self.work.iterdir())
        self.assertIn("伪装照片.jpg.zip", names)
        self.assertIn("假数据.dat.rar", names)
        self.assertIn("文档.docx", names)
        self.assertIn("说明.txt", names)
        self.assertIn("正常.zip", names)
        self.assertIn("游戏.7z.002", names)
        self.assertEqual(len(fixed), 2)

    def test_game_save_files_not_touched(self):
        """游戏存档防误判：.save/.sav/.pak 即使内容是 zip 也不改名。"""
        import zipfile
        for name in ["进度.save", "存档.sav", "资源.pak"]:
            with zipfile.ZipFile(self.work / name, "w") as z:
                z.writestr("data", "x" * 4096)
        fixed = extract.fix_disguised_extensions(self.work, self.quiet_log, min_size=0)
        self.assertEqual(fixed, [])
        names = sorted(p.name for p in self.work.iterdir())
        self.assertEqual(names, ["存档.sav", "资源.pak", "进度.save"])

    def test_min_size_gate(self):
        """大小门槛：小于阈值的伪装文件不识别（1KB-100KB 的存档类小文件）。"""
        import zipfile
        with zipfile.ZipFile(self.work / "小存档.dat", "w") as z:
            z.writestr("s", "x" * 1024)   # 几 KB 的小 zip
        with zipfile.ZipFile(self.work / "大游戏.dat", "w") as z:
            z.writestr("b", os.urandom(300 * 1024))  # 超过阈值

        fixed = extract.fix_disguised_extensions(
            self.work, self.quiet_log, min_size=200 * 1024)
        self.assertEqual(len(fixed), 1)
        names = sorted(p.name for p in self.work.iterdir())
        self.assertIn("小存档.dat", names)      # 低于阈值，原样保留
        self.assertIn("大游戏.dat.zip", names)  # 超过阈值，正常修正


# ================= 模块二：解压流程（需要 7z.exe） =================

@unittest.skipUnless(SEVENZIP, "未找到 7z.exe，跳过解压流程测试")
class TestExtractFlow(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.work / "src"
        self.src.mkdir()
        (self.src / "游戏数据.txt").write_text("内容", encoding="utf-8")
        self.cfg = Config(self.work / "config.json")
        self.sz = SevenZip(SEVENZIP)

    def run7z(self, *args):
        r = subprocess.run([SEVENZIP, *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def extract_all(self, root):
        return {str(i.main_file): extract.extract_one(i, self.cfg, self.sz, self.quiet_log)
                for i in extract.find_archives(root)}

    def test_plain_zip_no_password(self):
        d = self.work / "ext"
        d.mkdir()
        self.run7z("a", str(d / "普通.zip"), str(self.src / "*"))
        rec = list(self.extract_all(d).values())[0]
        self.assertEqual(rec.result, "成功")
        self.assertEqual(rec.password, "")
        self.assertTrue((d / "游戏数据.txt").exists())

    def test_password_pool_hit_and_persist(self):
        d = self.work / "ext"
        d.mkdir()
        self.run7z("a", str(d / "加密.7z"), str(self.src / "*"), "-psecret1")
        self.cfg.add_password("wrongpass")
        self.cfg.add_password("secret1")
        rec = list(self.extract_all(d).values())[0]
        self.assertEqual(rec.result, "成功")
        self.assertEqual(rec.password, "secret1")
        # 命中计数持久化 + 排序前移
        cfg2 = Config(self.work / "config.json")
        self.assertEqual(cfg2.sorted_passwords()[0],
                         {"password": "secret1", "hits": 1})

    def test_multivolume_and_delete_switch(self):
        d = self.work / "ext"
        d.mkdir()
        (self.src / "随机.bin").write_bytes(os.urandom(250 * 1024))  # 不可压缩 → 必产生多卷
        self.run7z("a", str(d / "分卷.7z"), str(self.src / "*"), "-v100k")
        self.assertGreater(len(list(d.glob("分卷.7z.*"))), 1)
        self.cfg.data["delete_after_extract"] = True
        rec = list(self.extract_all(d).values())[0]
        self.assertEqual(rec.result, "成功")
        self.assertEqual((d / "随机.bin").stat().st_size, 250 * 1024)
        # 分卷全部删除
        self.assertEqual(list(d.glob("分卷.7z.*")), [])

    def test_corrupt_archive_classified(self):
        d = self.work / "ext"
        d.mkdir()
        (d / "损坏.zip").write_bytes(b"not an archive")
        rec = list(self.extract_all(d).values())[0]
        self.assertEqual(rec.result, "失败")
        self.assertIn("损坏", rec.detail)

    def test_missing_volume_classified(self):
        d = self.work / "ext"
        d.mkdir()
        (self.src / "随机.bin").write_bytes(os.urandom(250 * 1024))
        self.run7z("a", str(d / "分卷.7z"), str(self.src / "*"), "-v100k")
        vols = sorted(d.glob("分卷.7z.*"))
        incomplete = self.work / "缺卷"
        incomplete.mkdir()
        shutil.copy(vols[0], incomplete / vols[0].name)  # 只留 .001
        rec = list(self.extract_all(incomplete).values())[0]
        self.assertEqual(rec.result, "失败")
        self.assertIn("分卷", rec.detail)  # 标注"疑似分卷不完整"而非密码错误

    def test_special_chars_end_to_end(self):
        """含 []【】空格 的分卷压缩包：真实解压成功且整组分卷全部删除。"""
        d = self.work / "ext [特殊]"
        d.mkdir()
        (self.src / "随机.bin").write_bytes(os.urandom(250 * 1024))
        archive = d / "游戏[English] 【测试】.7z"
        self.run7z("a", str(archive), str(self.src / "*"), "-v100k")
        vols = [p for p in d.iterdir()]
        self.assertGreater(len(vols), 1, "测试数据应产生多卷")

        self.cfg.data["delete_after_extract"] = True
        records = self.extract_all(d)
        self.assertEqual(len(records), 1)  # 特殊字符分卷归并为一项
        rec = list(records.values())[0]
        self.assertEqual(rec.result, "成功", rec.detail)
        self.assertEqual((d / "随机.bin").stat().st_size, 250 * 1024)
        # 整组分卷（含 [] 的文件名）全部删除，无孤儿分卷残留
        leftovers = [p.name for p in d.iterdir() if ".7z" in p.name]
        self.assertEqual(leftovers, [])

    def test_special_chars_missing_volume_classified(self):
        """含 [] 的缺卷分卷：完整性判断（iterdir+正则）不受特殊字符影响。"""
        d = self.work / "ext"
        d.mkdir()
        (self.src / "随机.bin").write_bytes(os.urandom(250 * 1024))
        self.run7z("a", str(d / "游戏[缺卷测试].7z"), str(self.src / "*"), "-v100k")
        vols = sorted(p for p in d.iterdir() if ".7z." in p.name)
        incomplete = self.work / "缺卷[目录]"
        incomplete.mkdir()
        shutil.copy(vols[0], incomplete / vols[0].name)  # 只留 .001
        rec = list(self.extract_all(incomplete).values())[0]
        self.assertEqual(rec.result, "失败")
        self.assertIn("分卷", rec.detail)

    def test_extract_to_subfolder(self):
        d = self.work / "ext"
        d.mkdir()
        self.run7z("a", str(d / "游戏X.zip"), str(self.src / "*"))
        self.cfg.data["extract_to_subfolder"] = True
        rec = list(self.extract_all(d).values())[0]
        self.assertEqual(rec.result, "成功")
        self.assertTrue((d / "游戏X" / "游戏数据.txt").exists())

    def test_subfolder_avoids_double_nesting(self):
        """包内已有唯一顶层文件夹时，子文件夹模式不再套一层（防 游戏A/游戏A/）。"""
        d = self.work / "ext"
        d.mkdir()
        # 压缩整个 src 目录本身 → 包内唯一顶层条目是 "src" 文件夹
        self.run7z("a", str(d / "游戏Y.7z"), str(self.src))
        self.cfg.data["extract_to_subfolder"] = True
        rec = list(self.extract_all(d).values())[0]
        self.assertEqual(rec.result, "成功", rec.detail)
        self.assertTrue((d / "src" / "游戏数据.txt").exists())
        self.assertFalse((d / "游戏Y").exists())  # 未额外套一层

    def test_preferred_password_tried_first(self):
        """批次内密码局部性：preferred 密码应先于密码池被测试。"""
        d = self.work / "ext"
        d.mkdir()
        self.run7z("a", str(d / "加密.7z"), str(self.src / "*"), "-plocal99")
        # 密码池里放一个命中次数很高的错误密码，若按池序会先试它
        self.cfg.add_password("popular_wrong")
        self.cfg.data["passwords"][0]["hits"] = 100
        self.cfg.add_password("local99")

        logs = []
        items = extract.find_archives(d)
        rec = extract.extract_one(items[0], self.cfg, self.sz,
                                  lambda m, t: logs.append(m),
                                  preferred_passwords=["local99"])
        self.assertEqual(rec.result, "成功")
        self.assertEqual(rec.password, "local99")
        # 日志中 local99 的测试应出现在 popular_wrong 之前
        joined = "\n".join(logs)
        self.assertIn("local99", joined)
        self.assertLess(joined.find("local99"), joined.find("popular_wrong")
                        if "popular_wrong" in joined else len(joined))

    def test_nested_extract(self):
        """嵌套解压：外层 7z 里的 zip 会被继续解压（最多 4 层）。"""
        import zipfile
        root = self.work / "root"
        root.mkdir()
        stage = self.work / "stage"
        stage.mkdir()
        with zipfile.ZipFile(stage / "内层.zip", "w") as z:
            z.writestr("内层文件.txt", "nested!")
        self.run7z("a", str(root / "外层.7z"), str(stage / "*"))

        records, failed, stopped = extract.extract_batch(
            self.cfg, self.sz, self.quiet_log, scan_root=root)
        ex = [r for r in records if r.op == "解压"]
        self.assertEqual(len(ex), 2)  # 外层 + 嵌套的内层
        self.assertTrue(all(r.result == "成功" for r in ex))
        self.assertTrue((root / "内层文件.txt").exists())
        self.assertEqual(failed, [])
        self.assertFalse(stopped)

    def test_nested_disabled(self):
        """关闭嵌套解压时，内层压缩包保持原样。"""
        import zipfile
        root = self.work / "root"
        root.mkdir()
        stage = self.work / "stage"
        stage.mkdir()
        with zipfile.ZipFile(stage / "内层.zip", "w") as z:
            z.writestr("内层文件.txt", "nested!")
        self.run7z("a", str(root / "外层.7z"), str(stage / "*"))
        self.cfg.data["nested_extract"] = False

        records, _, _ = extract.extract_batch(
            self.cfg, self.sz, self.quiet_log, scan_root=root)
        ex = [r for r in records if r.op == "解压"]
        self.assertEqual(len(ex), 1)
        self.assertTrue((root / "内层.zip").exists())
        self.assertFalse((root / "内层文件.txt").exists())

    def test_disguised_archive_fixed_then_extracted(self):
        """智能扩展名修正 + 解压端到端：伪装成 .jpg 的 zip 被修名并解出内容。"""
        import zipfile
        root = self.work / "root"
        root.mkdir()
        with zipfile.ZipFile(root / "截图.jpg", "w") as z:
            z.writestr("真实内容.txt", "hidden")

        self.cfg.data["smart_fix_min_mb"] = 0  # 测试文件很小，关闭大小门槛
        records, _, _ = extract.extract_batch(
            self.cfg, self.sz, self.quiet_log, scan_root=root)
        self.assertTrue(any(r.op == "扩展名修正" for r in records))
        self.assertTrue((root / "截图.jpg.zip").exists())
        self.assertTrue((root / "真实内容.txt").exists())

    def test_nested_skips_small_save_files(self):
        """嵌套解压 + 默认大小门槛：解出的游戏小存档不被误判为压缩包再解一层。"""
        import zipfile
        root = self.work / "root"
        root.mkdir()
        stage = self.work / "stage"
        stage.mkdir()
        # 存档：zip 格式、几 KB、.save 后缀——三重特征都不该被碰
        with zipfile.ZipFile(stage / "进度.save", "w") as z:
            z.writestr("slot1", "x" * 2048)
        # 再放一个伪装但同样小的文件，验证大小门槛在嵌套轮次也生效
        with zipfile.ZipFile(stage / "配置.cfg", "w") as z:
            z.writestr("cfg", "y" * 2048)
        self.run7z("a", str(root / "游戏.7z"), str(stage / "*"))

        # 默认配置：smart_fix_min_mb = 1.0
        records, _, _ = extract.extract_batch(
            self.cfg, self.sz, self.quiet_log, scan_root=root)
        self.assertFalse(any(r.op == "扩展名修正" for r in records))
        ex = [r for r in records if r.op == "解压"]
        self.assertEqual(len(ex), 1)  # 只解了外层，存档/配置原样保留
        self.assertTrue((root / "进度.save").exists())
        self.assertTrue((root / "配置.cfg").exists())

    def test_extract_target_dir(self):
        """目标解压路径：按相对扫描根目录的结构解压到指定目录。"""
        root = self.work / "root"
        sub = root / "分类A"
        sub.mkdir(parents=True)
        self.run7z("a", str(sub / "游戏.zip"), str(self.src / "*"))
        target = self.work / "输出"
        self.cfg.data["extract_target_dir"] = str(target)

        records, _, _ = extract.extract_batch(
            self.cfg, self.sz, self.quiet_log, scan_root=root)
        self.assertTrue((target / "分类A" / "游戏数据.txt").exists())
        # 原目录不产生解压产物（压缩包本身保留）
        self.assertFalse((sub / "游戏数据.txt").exists())

    def test_list_archive_password_gate(self):
        """7z l 快速验证：头部加密包错密码立即失败，正确密码返回内容信息。"""
        d = self.work / "ext"
        d.mkdir()
        # -mhe=on 头部加密：不知道密码连文件列表都看不到
        self.run7z("a", str(d / "头加密.7z"), str(self.src / "*"),
                   "-psecret", "-mhe=on")
        bad = self.sz.list_archive(d / "头加密.7z", "wrong")
        self.assertFalse(bad.ok)
        good = self.sz.list_archive(d / "头加密.7z", "secret")
        self.assertTrue(good.ok)
        self.assertIn("游戏数据.txt", good.top_names)
        self.assertGreater(good.total_size, 0)


# ================= 模块三：批量重命名 =================

class TestRename(TempDirTestCase):
    def test_normalize_code(self):
        self.assertEqual(rename_mod.normalize_code("001"), "1")
        self.assertEqual(rename_mod.normalize_code("XB001"), "XB1")
        self.assertEqual(rename_mod.normalize_code("kx01"), "KX1")
        self.assertIsNone(rename_mod.normalize_code("编号"))

    def test_mapping_and_plans(self):
        for name in ["001.中文游戏名", "023.另一个游戏", "XB001.某游戏",
                     "999.没有对照", "随意文件夹"]:
            (self.work / "分类" / name).mkdir(parents=True)
        csv_path = self.work / "对照表.csv"
        csv_path.write_text("编号,英文名\n1,Halo 3\n23,Gears: of War\nXB1,Forza\n",
                            encoding="utf-8-sig")
        mapping, _ = rename_mod.load_mapping(csv_path)
        self.assertEqual(len(mapping), 3)

        scan = rename_mod.build_plans(self.work, mapping)
        new_names = {p.path.name: p.new_name for p in scan.plans}
        self.assertEqual(new_names["001.中文游戏名"], "001.Halo 3")   # 前导零保留
        self.assertEqual(new_names["023.另一个游戏"], "023.Gears of War")  # 非法字符清除
        self.assertEqual(new_names["XB001.某游戏"], "XB001.Forza")   # 字母编号
        self.assertEqual(len(scan.unmatched), 1)  # 999 进待处理清单

        result = rename_mod.execute_plans(scan.plans, self.quiet_log)
        self.assertEqual(result.renamed, 3)
        self.assertTrue((self.work / "分类" / "001.Halo 3").exists())


class TestRenameDefaultAndUndo(TempDirTestCase):
    def test_default_numbering(self):
        """默认编号模式：一级子文件夹按名称排序改为 1, 2, 3..."""
        for name in ["苹果", "香蕉", "车厘子"]:
            (self.work / name).mkdir()
        scan = rename_mod.build_default_plans(self.work)
        self.assertEqual(sorted(p.new_name for p in scan.plans), ["1", "2", "3"])
        res = rename_mod.execute_plans(scan.plans, self.quiet_log)
        self.assertEqual(res.renamed, 3)
        dirs = sorted(p.name for p in self.work.iterdir() if p.is_dir())
        self.assertEqual(dirs, ["1", "2", "3"])

    def test_history_and_undo(self):
        """重命名历史落盘，回退恢复原名；再次回退提示无历史。"""
        for name in ["甲文件夹", "乙文件夹"]:
            (self.work / name).mkdir()
        scan = rename_mod.build_default_plans(self.work)
        rename_mod.execute_plans(scan.plans, self.quiet_log)
        saved = rename_mod.record_history(self.work, scan.plans)
        self.assertEqual(saved, 2)
        self.assertTrue((self.work / rename_mod.HISTORY_FILE).exists())

        restored, failed = rename_mod.undo_last(self.work, self.quiet_log)
        self.assertEqual((restored, failed), (2, 0))
        dirs = sorted(p.name for p in self.work.iterdir() if p.is_dir())
        self.assertEqual(dirs, ["乙文件夹", "甲文件夹"])
        # 历史用尽后文件删除，再回退返回 (0, 0)
        self.assertFalse((self.work / rename_mod.HISTORY_FILE).exists())
        self.assertEqual(rename_mod.undo_last(self.work, self.quiet_log), (0, 0))


# ================= 模块五：持久化与报告 =================

class TestPersistence(TempDirTestCase):
    def test_progress_store_roundtrip(self):
        store = ProgressStore(self.work / "progress.json")
        store.mark_done("D:/some/archive.7z")
        store2 = ProgressStore(self.work / "progress.json")
        self.assertTrue(store2.is_done("D:/some/archive.7z"))
        store2.clear()
        self.assertFalse((self.work / "progress.json").exists())

    def test_config_corrupt_file_falls_back(self):
        p = self.work / "config.json"
        p.write_text("{broken json", encoding="utf-8")
        cfg = Config(p)  # 不应抛异常
        self.assertEqual(cfg.data["passwords"], [])

    def test_report_csv(self):
        out = self.work / "报告.csv"
        export_csv([ReportRecord("D:/a.7z", "解压", "成功", "", "pwd", 1.234)], out)
        content = out.read_text(encoding="utf-8-sig")
        self.assertIn("文件路径", content)
        self.assertIn("D:/a.7z", content)


# ================= 模块四：GUI 构建 =================

class TestGui(unittest.TestCase):
    def test_build_and_events(self):
        import tkinter as tk
        try:
            from gui import App, create_root
            root = create_root()
        except tk.TclError as e:
            self.skipTest(f"当前环境无法创建 Tk 窗口：{e}")
        try:
            root.withdraw()
            app = App(root)
            # 模拟工作线程事件，验证事件处理不抛异常
            app._emit(type="log", msg="测试", tag="success")
            app._emit(type="total", current=3, total=10, name="x.7z")
            app._emit(type="file", percent=42)
            app._emit(type="count", kind="成功")
            app._emit(type="done")
            import time
            time.sleep(0.3)  # 等 after(100) 轮询消费队列
            root.update()
            self.assertTrue(app.event_queue.empty())
            self.assertEqual(app.counts["成功"], 1)
            self.assertIn("3/10", app.total_label.cget("text"))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
