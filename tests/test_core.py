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

    def test_plain_archives(self):
        self.touch("a.zip", "b.7z", "c.rar")
        items = self.find()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i.kind == "single" for i in items))


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

    def test_extract_to_subfolder(self):
        d = self.work / "ext"
        d.mkdir()
        self.run7z("a", str(d / "游戏X.zip"), str(self.src / "*"))
        self.cfg.data["extract_to_subfolder"] = True
        rec = list(self.extract_all(d).values())[0]
        self.assertEqual(rec.result, "成功")
        self.assertTrue((d / "游戏X" / "游戏数据.txt").exists())


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
