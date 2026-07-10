# -*- coding: utf-8 -*-
"""模块四：GUI 与进度可视化（一键操作布局）。

主窗口：源目录/目标目录、密码池、三个一键按钮（一键解压 / 一键预处理 /
一键重命名）+ 辅助按钮，双级进度条、彩色实时日志、状态栏计数、停止按钮。
引擎路径、清理规则、各类开关、CSV 对照表等细节配置收纳在「详细设置」对话框。

线程模型：所有耗时操作在工作线程执行，通过 queue.Queue 向 UI 线程
推送事件，UI 线程用 root.after(100) 轮询队列刷新界面，绝不阻塞。
"""

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from core import extract, preprocess
from core import rename as rename_mod
from core.config import Config
from core.progress_store import ProgressStore
from core.report import ReportRecord, export_csv
from core.sevenzip import SevenZip

# 拖拽支持为可选依赖：装了 tkinterdnd2 就启用拖文件夹进窗口，
# 没装则降级为仅"浏览"按钮，程序照常运行（保持标准库可跑）
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


def create_root() -> tk.Tk:
    """创建根窗口：有 tkinterdnd2 时用其增强版 Tk 以支持拖拽。"""
    return TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("GameArchiveTool - 游戏压缩包批量处理工具")
        self.root.geometry("980x760")
        self.root.minsize(820, 620)

        self.config = Config()
        self.progress_store = ProgressStore()

        # 工作线程 → UI 线程的事件队列；停止信号
        self.event_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        # 预览结果暂存（UI 线程持有，确认后交给执行线程）
        self.pre_plans: list[preprocess.RenamePlan] = []
        self.folder_plans: list[rename_mod.FolderRenamePlan] = []
        # 上次批量解压的失败项，供"重试失败项"使用
        self.failed_items: list[extract.ArchiveItem] = []
        self.last_scan_root: Path | None = None   # 重试时计算目标路径映射用

        # 报告记录与状态栏计数
        self.report_records: list[ReportRecord] = []
        self.counts = {"成功": 0, "失败": 0, "跳过": 0}

        self.settings_win: tk.Toplevel | None = None

        self._init_vars()
        self._build_ui()
        self._load_from_config()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_queue)

    def _init_vars(self):
        """全部配置项的 tk 变量（主窗口与设置对话框共享）。"""
        self.root_var = tk.StringVar()          # 源目录
        self.target_var = tk.StringVar()        # 目标解压目录
        self.sz_path_var = tk.StringVar()
        self.winrar_var = tk.StringVar()
        self.csv_var = tk.StringVar()
        self.subfolder_var = tk.BooleanVar()
        self.delete_var = tk.BooleanVar()
        self.skip_existing_var = tk.BooleanVar(value=True)
        self.clean_var = tk.BooleanVar(value=True)
        self.smart_fix_var = tk.BooleanVar(value=True)
        self.smart_fix_min_var = tk.StringVar(value="1")
        self.nested_var = tk.BooleanVar(value=True)
        self.pre_rule_var = tk.StringVar(value="删")
        self.pre_regex_var = tk.BooleanVar(value=False)

    # ================= UI 构建 =================

    def _build_ui(self):
        style = ttk.Style(self.root)
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 11, "bold"),
                        padding=(16, 10))

        self._build_paths()
        self._build_middle()
        self._build_progress()
        self._build_log()
        self._build_statusbar()

    def _build_paths(self):
        frm = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        frm.pack(fill="x")

        r1 = ttk.Frame(frm)
        r1.pack(fill="x")
        ttk.Label(r1, text="源目录：", width=9).pack(side="left")
        ttk.Entry(r1, textvariable=self.root_var).pack(side="left", fill="x",
                                                       expand=True, padx=4)
        ttk.Button(r1, text="浏览...", command=self._browse_root).pack(side="left")
        hint = "（可拖入文件夹）" if DND_AVAILABLE else ""
        ttk.Label(r1, text=hint, foreground="#888").pack(side="left", padx=4)

        r2 = ttk.Frame(frm)
        r2.pack(fill="x", pady=(4, 0))
        ttk.Label(r2, text="目标目录：", width=9).pack(side="left")
        ttk.Entry(r2, textvariable=self.target_var).pack(side="left", fill="x",
                                                         expand=True, padx=4)
        ttk.Button(r2, text="浏览...", command=self._browse_target).pack(side="left")
        ttk.Label(r2, text="（留空=解压到压缩包所在目录）",
                  foreground="#888").pack(side="left", padx=4)

        # 注册整窗口为拖放目标：拖入文件夹即设为源目录
        if DND_AVAILABLE:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)

    def _build_middle(self):
        frm = ttk.Frame(self.root, padding=(8, 6, 8, 0))
        frm.pack(fill="x")

        # 左侧：密码池
        pw_frame = ttk.LabelFrame(frm, text="解压密码池（按命中次数自动排序）", padding=4)
        pw_frame.pack(side="left", fill="both", expand=True)
        self.pw_list = tk.Listbox(pw_frame, height=8)
        self.pw_list.pack(side="left", fill="both", expand=True)
        pw_sb = ttk.Scrollbar(pw_frame, command=self.pw_list.yview)
        pw_sb.pack(side="left", fill="y")
        self.pw_list.configure(yscrollcommand=pw_sb.set)
        pw_btns = ttk.Frame(pw_frame)
        pw_btns.pack(side="left", fill="y", padx=4)
        ttk.Button(pw_btns, text="添加", command=self._pw_add).pack(fill="x", pady=2)
        ttk.Button(pw_btns, text="编辑", command=self._pw_edit).pack(fill="x", pady=2)
        ttk.Button(pw_btns, text="删除", command=self._pw_delete).pack(fill="x", pady=2)

        # 右侧：一键操作 + 辅助按钮
        act = ttk.Frame(frm)
        act.pack(side="left", fill="y", padx=(12, 0))

        self.btn_quick_extract = ttk.Button(
            act, text="一键解压", style="Big.TButton", command=self._start_extract)
        self.btn_quick_extract.pack(fill="x", pady=(0, 4))
        self.btn_quick_prep = ttk.Button(
            act, text="一键预处理（统一分卷名）", style="Big.TButton",
            command=self._start_quick_preprocess)
        self.btn_quick_prep.pack(fill="x", pady=4)
        self.btn_quick_rename = ttk.Button(
            act, text="一键重命名（1, 2, 3...）", style="Big.TButton",
            command=self._start_quick_rename)
        self.btn_quick_rename.pack(fill="x", pady=4)

        aux = ttk.Frame(act)
        aux.pack(fill="x", pady=(6, 0))
        self.btn_retry = ttk.Button(aux, text="重试失败项", state="disabled",
                                    command=self._start_retry_failed)
        self.btn_retry.pack(side="left", expand=True, fill="x", padx=(0, 2))
        self.btn_ren_undo = ttk.Button(aux, text="回退重命名",
                                       command=self._start_ren_undo)
        self.btn_ren_undo.pack(side="left", expand=True, fill="x", padx=2)

        aux2 = ttk.Frame(act)
        aux2.pack(fill="x", pady=(4, 0))
        ttk.Button(aux2, text="导出报告", command=self._export_report).pack(
            side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(aux2, text="详细设置...", command=self._open_settings).pack(
            side="left", expand=True, fill="x", padx=2)

    def _build_progress(self):
        frm = ttk.LabelFrame(self.root, text="进度", padding=6)
        frm.pack(fill="x", padx=8, pady=(6, 0))

        r1 = ttk.Frame(frm)
        r1.pack(fill="x")
        ttk.Label(r1, text="总进度：", width=10).pack(side="left")
        self.total_bar = ttk.Progressbar(r1, maximum=1, value=0)
        self.total_bar.pack(side="left", fill="x", expand=True, padx=4)
        self.total_label = ttk.Label(r1, text="0/0", width=32, anchor="w")
        self.total_label.pack(side="left")

        r2 = ttk.Frame(frm)
        r2.pack(fill="x", pady=(4, 0))
        ttk.Label(r2, text="当前文件：", width=10).pack(side="left")
        self.file_bar = ttk.Progressbar(r2, maximum=100, value=0)
        self.file_bar.pack(side="left", fill="x", expand=True, padx=4)
        self.file_label = ttk.Label(r2, text="0%", width=6, anchor="w")
        self.file_label.pack(side="left")
        self.btn_stop = ttk.Button(r2, text="停止", state="disabled", command=self._request_stop)
        self.btn_stop.pack(side="left", padx=8)

    def _build_log(self):
        frm = ttk.LabelFrame(self.root, text="日志", padding=4)
        frm.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = ScrolledText(frm, height=14, state="disabled", wrap="none")
        self.log_text.pack(fill="both", expand=True)
        # 日志配色：成功绿、失败红、警告橙、信息灰
        self.log_text.tag_config("success", foreground="#008000")
        self.log_text.tag_config("error", foreground="#cc0000")
        self.log_text.tag_config("warn", foreground="#cc7700")
        self.log_text.tag_config("info", foreground="#777777")

    def _build_statusbar(self):
        self.status_label = ttk.Label(self.root, anchor="w", relief="sunken", padding=(6, 2))
        self.status_label.pack(fill="x", side="bottom")
        self._refresh_status()

    # ================= 详细设置对话框 =================

    def _open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        self.settings_win = win = tk.Toplevel(self.root)
        win.title("详细设置")
        win.transient(self.root)
        win.geometry("720x430")
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

        pad = {"padx": 8, "pady": 4}

        # ---- 引擎路径 ----
        eng = ttk.LabelFrame(win, text="解压引擎", padding=6)
        eng.pack(fill="x", **pad)
        self._path_row(eng, "7z.exe 路径：", self.sz_path_var, self._browse_7z)
        self._path_row(eng, "UnRAR 路径：", self.winrar_var, self._browse_winrar,
                       hint="（7z 解 rar 失败时自动回退，可留空）")

        # ---- 解压选项 ----
        opt = ttk.LabelFrame(win, text="解压选项", padding=6)
        opt.pack(fill="x", **pad)
        r1 = ttk.Frame(opt)
        r1.pack(fill="x")
        ttk.Checkbutton(r1, text="解压到以压缩包名命名的子文件夹",
                        variable=self.subfolder_var).pack(side="left")
        ttk.Checkbutton(r1, text="解压成功后删除原压缩包（进回收站）",
                        variable=self.delete_var).pack(side="left", padx=12)
        ttk.Checkbutton(r1, text="跳过已存在的同名文件夹",
                        variable=self.skip_existing_var).pack(side="left", padx=12)
        r2 = ttk.Frame(opt)
        r2.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(r2, text="智能识别伪装扩展名（≥",
                        variable=self.smart_fix_var).pack(side="left")
        ttk.Entry(r2, textvariable=self.smart_fix_min_var,
                  width=5, justify="center").pack(side="left")
        ttk.Label(r2, text="MB 才识别，防存档误判）").pack(side="left")
        ttk.Checkbutton(r2, text="嵌套解压（最多4层）",
                        variable=self.nested_var).pack(side="left", padx=12)

        # ---- 文件名清理 ----
        clean = ttk.LabelFrame(win, text="文件名清理（一键解压前自动执行）", padding=6)
        clean.pack(fill="x", **pad)
        rc = ttk.Frame(clean)
        rc.pack(fill="x")
        ttk.Label(rc, text="去除文件名末尾的：").pack(side="left")
        ttk.Entry(rc, textvariable=self.pre_rule_var, width=20).pack(side="left", padx=4)
        ttk.Label(rc, text="（多条规则用 ; 分隔）", foreground="#888").pack(side="left")
        ttk.Checkbutton(rc, text="按正则解释",
                        variable=self.pre_regex_var).pack(side="left", padx=8)
        ttk.Checkbutton(rc, text="解压前自动清理",
                        variable=self.clean_var).pack(side="left", padx=8)
        rc2 = ttk.Frame(clean)
        rc2.pack(fill="x", pady=(4, 0))
        ttk.Button(rc2, text="预览清理", command=self._start_pre_preview).pack(
            side="left", padx=(0, 4))
        ttk.Button(rc2, text="执行清理", command=self._start_pre_execute).pack(side="left")
        ttk.Label(rc2, text="（预览结果显示在主窗口日志中）",
                  foreground="#888").pack(side="left", padx=8)

        # ---- 对照表重命名（高级） ----
        ren = ttk.LabelFrame(win, text="对照表重命名（高级：编号→英文名，主窗口一键重命名为默认编号模式）",
                             padding=6)
        ren.pack(fill="x", **pad)
        self._path_row(ren, "CSV 对照表：", self.csv_var, self._browse_csv)
        rr = ttk.Frame(ren)
        rr.pack(fill="x", pady=(4, 0))
        ttk.Button(rr, text="预览匹配", command=self._start_ren_preview_csv).pack(
            side="left", padx=(0, 4))
        ttk.Button(rr, text="执行对照表重命名",
                   command=self._start_ren_execute_csv).pack(side="left")
        ttk.Label(rr, text="（结果显示在主窗口日志中，可回退）",
                  foreground="#888").pack(side="left", padx=8)

        ttk.Button(win, text="关闭（设置自动保存）",
                   command=self._close_settings).pack(pady=8)

    @staticmethod
    def _path_row(parent, label, var, browse_cmd, hint=""):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=14).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="浏览...", command=browse_cmd).pack(side="left")
        if hint:
            ttk.Label(row, text=hint, foreground="#888").pack(side="left", padx=4)

    def _close_settings(self):
        self._sync_config()
        if self.settings_win is not None:
            self.settings_win.destroy()
            self.settings_win = None

    # ================= 配置读写 =================

    def _load_from_config(self):
        d = self.config.data
        self.root_var.set(d["last_root"])
        self.target_var.set(d["extract_target_dir"])
        self.sz_path_var.set(d["seven_zip_path"])
        self.winrar_var.set(d["winrar_path"])
        self.csv_var.set(d["last_csv"])
        self.subfolder_var.set(d["extract_to_subfolder"])
        self.delete_var.set(d["delete_after_extract"])
        self.skip_existing_var.set(d["skip_existing_folder"])
        self.clean_var.set(d["clean_before_extract"])
        self.smart_fix_var.set(d["smart_ext_fix"])
        self.smart_fix_min_var.set(str(d["smart_fix_min_mb"]).rstrip("0").rstrip(".")
                                   or "0")
        self.nested_var.set(d["nested_extract"])
        self.pre_rule_var.set(d["preprocess_suffix"])
        self.pre_regex_var.set(d["preprocess_use_regex"])
        self._refresh_pw_list()

    def _sync_config(self):
        """把界面上的当前值写回 config.json。"""
        d = self.config.data
        d["last_root"] = self.root_var.get().strip()
        d["extract_target_dir"] = self.target_var.get().strip()
        d["seven_zip_path"] = self.sz_path_var.get().strip()
        d["winrar_path"] = self.winrar_var.get().strip()
        d["last_csv"] = self.csv_var.get().strip()
        d["extract_to_subfolder"] = self.subfolder_var.get()
        d["delete_after_extract"] = self.delete_var.get()
        d["skip_existing_folder"] = self.skip_existing_var.get()
        d["clean_before_extract"] = self.clean_var.get()
        d["smart_ext_fix"] = self.smart_fix_var.get()
        try:
            d["smart_fix_min_mb"] = max(0.0, float(self.smart_fix_min_var.get()))
        except ValueError:
            pass  # 输入非法时保留原值
        d["nested_extract"] = self.nested_var.get()
        d["preprocess_suffix"] = self.pre_rule_var.get()
        d["preprocess_use_regex"] = self.pre_regex_var.get()
        self.config.save()

    def _on_close(self):
        self._sync_config()
        self.stop_event.set()
        self.root.destroy()

    # ================= 顶部 / 通用控件回调 =================

    def _browse_root(self):
        path = filedialog.askdirectory(title="选择源目录")
        if path:
            self.root_var.set(str(Path(path)))

    def _browse_target(self):
        path = filedialog.askdirectory(title="选择目标解压目录")
        if path:
            self.target_var.set(str(Path(path)))

    def _browse_7z(self):
        path = filedialog.askopenfilename(title="选择 7z.exe",
                                          filetypes=[("7z.exe", "7z.exe"), ("所有文件", "*.*")])
        if path:
            self.sz_path_var.set(str(Path(path)))

    def _browse_winrar(self):
        path = filedialog.askopenfilename(
            title="选择 UnRAR.exe（WinRAR 安装目录内）",
            filetypes=[("UnRAR/WinRAR", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.winrar_var.set(str(Path(path)))

    def _browse_csv(self):
        path = filedialog.askopenfilename(title="选择对照表 CSV",
                                          filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if path:
            self.csv_var.set(str(Path(path)))

    def _on_drop(self, event):
        """拖拽落下：取第一个是文件夹的路径设为源目录。"""
        for raw in self.root.tk.splitlist(event.data):
            p = Path(raw)
            if p.is_dir():
                self.root_var.set(str(p))
                self._log(f"已通过拖拽设置源目录：{p}", "info")
                return

    def _get_root_dir(self) -> Path | None:
        """校验并返回源目录，非法时弹窗提示。"""
        raw = self.root_var.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请先选择源目录")
            return None
        p = Path(raw)
        if not p.is_dir():
            messagebox.showerror("错误", f"源目录不存在：{p}")
            return None
        return p

    # ================= 密码池管理 =================

    def _refresh_pw_list(self):
        self.pw_list.delete(0, tk.END)
        self._pw_order = [p["password"] for p in self.config.sorted_passwords()]
        for p in self.config.sorted_passwords():
            self.pw_list.insert(tk.END, f"{p['password']}    (命中 {p.get('hits', 0)} 次)")

    def _pw_selected(self) -> str | None:
        sel = self.pw_list.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先在密码池中选择一条")
            return None
        return self._pw_order[sel[0]]

    def _pw_add(self):
        pwd = simpledialog.askstring("添加密码", "请输入要加入密码池的密码：", parent=self.root)
        if pwd and self.config.add_password(pwd):
            self._refresh_pw_list()

    def _pw_edit(self):
        old = self._pw_selected()
        if old is None:
            return
        new = simpledialog.askstring("编辑密码", "修改密码为：", initialvalue=old, parent=self.root)
        if new and self.config.update_password(old, new):
            self._refresh_pw_list()

    def _pw_delete(self):
        pwd = self._pw_selected()
        if pwd is None:
            return
        if messagebox.askyesno("确认", f"确定从密码池删除「{pwd}」？"):
            self.config.remove_password(pwd)
            self._refresh_pw_list()

    # ================= 任务启动框架 =================

    def _start_worker(self, target, *args) -> bool:
        """启动工作线程；同一时间只允许一个批量任务。"""
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("提示", "已有任务正在执行，请等待完成或点击停止")
            return False
        self._sync_config()
        self.stop_event.clear()
        self._set_running(True)
        self.worker = threading.Thread(target=self._worker_wrapper,
                                       args=(target, *args), daemon=True)
        self.worker.start()
        return True

    def _worker_wrapper(self, target, *args):
        """工作线程统一入口：兜底捕获异常，保证 done 事件一定发出。"""
        try:
            target(*args)
        except Exception as e:  # noqa: BLE001 兜底，避免线程静默死亡导致 UI 卡在运行态
            self._emit(type="log", msg=f"[异常] 任务意外中止：{e!r}", tag="error")
        finally:
            self._emit(type="done")

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        for btn in (self.btn_quick_extract, self.btn_quick_prep,
                    self.btn_quick_rename, self.btn_ren_undo):
            btn.configure(state=state)
        # 重试按钮只有存在失败项时才恢复可用
        self.btn_retry.configure(
            state="normal" if (not running and self.failed_items) else "disabled")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if running:
            self.file_bar.configure(value=0)
            self.file_label.configure(text="0%")

    def _request_stop(self):
        self.stop_event.set()
        self._log("已请求停止，将在当前文件处理完后停止...", "warn")

    def _reset_counts(self):
        self.counts = {"成功": 0, "失败": 0, "跳过": 0}
        self._refresh_status()

    # ================= 事件队列（工作线程 → UI） =================

    def _emit(self, **ev):
        self.event_queue.put(ev)

    def _wlog(self, msg: str, tag: str = "info"):
        """供工作线程调用的日志函数。"""
        self._emit(type="log", msg=msg, tag=tag)

    def _poll_queue(self):
        try:
            while True:
                self._handle_event(self.event_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_event(self, ev: dict):
        t = ev["type"]
        if t == "log":
            self._log(ev["msg"], ev.get("tag", "info"))
        elif t == "total":
            total = max(ev["total"], 1)
            self.total_bar.configure(maximum=total, value=ev["current"])
            name = ev.get("name", "")
            self.total_label.configure(text=f"{ev['current']}/{ev['total']}  {name}")
        elif t == "file":
            pct = ev["percent"]
            self.file_bar.configure(value=pct)
            self.file_label.configure(text=f"{pct}%")
        elif t == "count":
            self.counts[ev["kind"]] += 1
            self._refresh_status()
        elif t == "pre_plans":
            self.pre_plans = ev["plans"]
        elif t == "volfix":
            self._confirm_volfix(ev["plans"])
        elif t == "ren_scan":
            self._handle_ren_scan(ev)
        elif t == "failures":
            self.failed_items = ev["items"]
        elif t == "done":
            # 扫描类任务可能在 done 事件处理前已由确认框接续启动了执行任务，
            # 此时不能把界面恢复为空闲态
            if not (self.worker and self.worker.is_alive()):
                self._set_running(False)

    def _log(self, msg: str, tag: str = "info"):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_status(self):
        self.status_label.configure(
            text=f"成功：{self.counts['成功']}    失败：{self.counts['失败']}    "
                 f"跳过：{self.counts['跳过']}"
        )

    def _join_worker(self):
        """扫描线程通常已结束，短暂等待避免与接续任务的启动检查竞态。"""
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3)

    # ================= 一键预处理（统一分卷名） =================

    def _start_quick_preprocess(self):
        root_dir = self._get_root_dir()
        if root_dir is None:
            return
        self._start_worker(self._worker_volfix_scan, root_dir)

    def _worker_volfix_scan(self, root_dir: Path):
        self._wlog(f"—— 一键预处理：扫描 {root_dir} 中命名不一致的分卷 ——", "info")
        plans = preprocess.build_volume_fix_plans(root_dir)
        for p in plans:
            note = f"（{p.note}）" if p.skip else ""
            self._wlog(f"    {p.path.name} → {p.new_name} {note}",
                       "warn" if p.skip else "info")
        if not plans:
            self._wlog("未发现需要统一命名的分卷组", "info")
        self._emit(type="volfix", plans=plans)

    def _confirm_volfix(self, plans: list[preprocess.RenamePlan]):
        if not plans:
            return
        self._join_worker()
        n = sum(1 for p in plans if not p.skip)
        if not messagebox.askyesno(
                "一键预处理",
                f"发现 {n} 个分卷文件的主名与首卷不一致（详见日志）。\n\n"
                "统一为首卷的主名？"):
            return
        self._reset_counts()
        self._start_worker(self._worker_pre_execute, plans)

    # ================= 文件名清理（设置对话框内） =================

    def _start_pre_preview(self):
        root_dir = self._get_root_dir()
        if root_dir is None:
            return
        rule = self.pre_rule_var.get()
        use_regex = self.pre_regex_var.get()
        self._start_worker(self._worker_pre_preview, root_dir, rule, use_regex)

    def _worker_pre_preview(self, root_dir: Path, rule: str, use_regex: bool):
        self._wlog(f"—— 文件名清理预览：扫描 {root_dir} ——", "info")
        plans, err = preprocess.build_plans(root_dir, rule, use_regex)
        if err:
            self._wlog(f"[错误] {err}", "error")
            return
        for p in plans:
            note = f"（{p.note}）" if p.note else ""
            self._wlog(f"    {p.path.name} → {p.new_name} {note}",
                       "warn" if p.skip else "info")
        self._wlog(f"扫描完成，共找到 {len(plans)} 个待重命名文件；"
                   "确认无误后点「执行清理」", "info")
        self._emit(type="pre_plans", plans=plans)

    def _start_pre_execute(self):
        if not self.pre_plans:
            messagebox.showinfo("提示", "请先点「预览清理」生成待处理清单")
            return
        n = sum(1 for p in self.pre_plans if not p.skip)
        if not messagebox.askyesno("确认执行", f"将重命名 {n} 个文件，是否继续？"):
            return
        self._reset_counts()
        plans, self.pre_plans = self.pre_plans, []
        self._start_worker(self._worker_pre_execute, plans)

    def _worker_pre_execute(self, plans):
        self._wlog("—— 开始执行预处理重命名 ——", "info")

        def log_and_count(msg, tag):
            self._wlog(msg, tag)
            if msg.startswith("[重命名]"):
                self._emit(type="count", kind="成功")
            elif msg.startswith("[失败]"):
                self._emit(type="count", kind="失败")
            elif msg.startswith("[跳过]"):
                self._emit(type="count", kind="跳过")

        result = preprocess.execute_plans(plans, log_and_count,
                                          should_stop=self.stop_event.is_set)
        for p in plans:
            if p.result:  # 空 result 表示因停止而未执行，不计入报告
                self.report_records.append(ReportRecord(str(p.path), "预处理", p.result, p.note))
        self._wlog(f"—— 预处理完成：重命名 {result.renamed}，跳过 {result.skipped}，"
                   f"失败 {result.failed} ——", "info")

    # ================= 一键解压 =================

    def _start_extract(self):
        root_dir = self._get_root_dir()
        if root_dir is None:
            return
        sz = SevenZip(self.sz_path_var.get().strip())
        if not sz.available():
            messagebox.showerror("错误", f"未找到 7z.exe：{sz.exe_path}\n"
                                         "请在「详细设置」中设置正确路径")
            return

        # 断点续传：progress.json 里有记录时询问是否跳过已完成项
        skip_done = False
        self.progress_store.load()
        if self.progress_store.completed:
            skip_done = messagebox.askyesno(
                "断点续传",
                f"检测到上次任务已完成 {len(self.progress_store.completed)} 个压缩包。\n\n"
                "是否跳过这些已完成项继续？\n"
                "（选择“否”将清空进度记录并重新处理全部）",
            )
            if not skip_done:
                self.progress_store.clear()

        self._reset_counts()
        self.last_scan_root = root_dir
        self._start_worker(self._worker_extract, root_dir, sz, skip_done)

    def _start_retry_failed(self):
        """仅重新处理上次批量解压的失败项（例如补了密码/补齐分卷之后）。"""
        if not self.failed_items:
            return
        sz = SevenZip(self.sz_path_var.get().strip())
        if not sz.available():
            messagebox.showerror("错误", f"未找到 7z.exe：{sz.exe_path}")
            return
        items, self.failed_items = self.failed_items, []
        self._reset_counts()
        self._start_worker(self._worker_extract, None, sz, False, items)

    def _worker_extract(self, root_dir: Path | None, sz: SevenZip,
                        skip_done: bool, retry_items: list | None = None):
        # 解压前流水线第一步：文件名清理（把"游戏.7z.001删"修正为可识别的分卷名）
        if retry_items is None and self.config.data["clean_before_extract"]:
            self._run_cleanup_phase(root_dir)
            if self.stop_event.is_set():
                return

        if retry_items is not None:
            self._wlog(f"—— 重试上次失败的 {len(retry_items)} 个压缩包 ——", "info")

        def on_record(rec: extract.ExtractRecord):
            """每处理完一项：写入报告 + 更新状态栏计数。"""
            self.report_records.append(ReportRecord(
                rec.archive, rec.op, rec.result,
                rec.detail, rec.password, rec.elapsed))
            if rec.op == "解压":
                self._emit(type="count", kind=rec.result)

        # 批量循环（含嵌套解压/目标路径/密码局部性）在 core.extract_batch 中
        records, failed_items, stopped = extract.extract_batch(
            self.config, sz, self._wlog,
            scan_root=root_dir if root_dir is not None else self.last_scan_root,
            items=retry_items,
            should_stop=self.stop_event.is_set,
            file_progress=lambda p: self._emit(type="file", percent=p),
            total_progress=lambda c, t, n: self._emit(
                type="total", current=c, total=t, name=n),
            on_record=on_record,
            completed=set(self.progress_store.completed) if skip_done else None,
            mark_done=self.progress_store.mark_done,
        )

        # 失败清单汇总；失败项交回 UI 线程供"重试失败项"使用
        failures = [r for r in records if r.op == "解压" and r.result == "失败"]
        if failures:
            self._wlog(f"—— 失败清单（{len(failures)} 项，可点「重试失败项」重跑）——", "error")
            for r in failures:
                self._wlog(f"  {r.archive}  →  {r.detail}", "error")
        self._emit(type="failures", items=failed_items)

        if stopped:
            self._wlog("—— 任务已停止（进度已保存，可断点续传）——", "warn")
        else:
            # 全部处理完成，清空断点记录
            self.progress_store.clear()
            self._wlog("—— 批量解压完成，可点「导出报告」保存处理明细 ——", "info")

    def _run_cleanup_phase(self, root_dir: Path):
        """解压流水线的文件名清理阶段：按当前规则直接执行（逐条记日志）。"""
        rules = self.config.data["preprocess_suffix"]
        use_regex = self.config.data["preprocess_use_regex"]
        plans, err = preprocess.build_plans(root_dir, rules, use_regex)
        if err:
            self._wlog(f"[清理] 规则无效，跳过清理阶段：{err}", "warn")
            return
        if not plans:
            return
        self._wlog(f"—— 解压前文件名清理：{len(plans)} 项 ——", "info")
        preprocess.execute_plans(plans, self._wlog,
                                 should_stop=self.stop_event.is_set)
        for p in plans:
            if p.result:
                self.report_records.append(
                    ReportRecord(str(p.path), "预处理", p.result, p.note))

    # ================= 一键重命名（默认编号） / 对照表重命名 =================

    def _start_quick_rename(self):
        """一键重命名：一级子文件夹按名称排序改为 1, 2, 3...（扫描→确认→执行）。"""
        root_dir = self._get_root_dir()
        if root_dir is None:
            return
        self._start_worker(self._worker_ren_scan_seq, root_dir, True)

    def _worker_ren_scan_seq(self, root_dir: Path, auto: bool):
        self._wlog(f"—— 一键重命名预览：{root_dir} 的一级子文件夹 → 1, 2, 3... ——", "info")
        scan = rename_mod.build_default_plans(root_dir)
        for p in scan.plans:
            note = f"（{p.note}）" if p.note else ""
            self._wlog(f"    {p.path.name} → {p.new_name} {note}",
                       "warn" if p.skip else "info")
        if not scan.plans:
            self._wlog("没有需要重命名的文件夹", "info")
        self._emit(type="ren_scan", scan=scan, root=str(root_dir), auto=auto)

    def _start_ren_preview_csv(self):
        root_dir, csv_path = self._get_csv_inputs()
        if root_dir is None:
            return
        self._start_worker(self._worker_ren_scan_csv, root_dir, csv_path, False)

    def _start_ren_execute_csv(self):
        root_dir, csv_path = self._get_csv_inputs()
        if root_dir is None:
            return
        self._start_worker(self._worker_ren_scan_csv, root_dir, csv_path, True)

    def _get_csv_inputs(self):
        root_dir = self._get_root_dir()
        if root_dir is None:
            return None, None
        csv_raw = self.csv_var.get().strip()
        if not csv_raw or not Path(csv_raw).is_file():
            messagebox.showerror("错误", "请先选择有效的 CSV 对照表文件")
            return None, None
        return root_dir, Path(csv_raw)

    def _worker_ren_scan_csv(self, root_dir: Path, csv_path: Path, auto: bool):
        self._wlog(f"—— 对照表重命名：读取 {csv_path.name} ——", "info")
        try:
            mapping, warnings = rename_mod.load_mapping(csv_path)
        except (OSError, UnicodeDecodeError) as e:
            self._wlog(f"[错误] 无法读取 CSV：{e}", "error")
            return
        for w in warnings:
            self._wlog(f"[对照表] {w}", "warn")
        self._wlog(f"对照表加载完成，共 {len(mapping)} 条", "info")

        scan = rename_mod.build_plans(root_dir, mapping)
        for p in scan.plans:
            note = f"（{p.note}）" if p.note else ""
            self._wlog(f"    {p.path.name} → {p.new_name} {note}",
                       "warn" if p.skip else "info")
        if scan.unmatched:
            self._wlog(f"—— 未匹配（待处理清单，{len(scan.unmatched)} 个）——", "warn")
            for p in scan.unmatched:
                self._wlog(f"    {p}", "warn")
        self._wlog(f"匹配到 {len(scan.plans)} 个待重命名文件夹，"
                   f"未匹配 {len(scan.unmatched)} 个", "info")
        self._emit(type="ren_scan", scan=scan, root=str(root_dir), auto=auto)

    def _handle_ren_scan(self, ev: dict):
        scan: rename_mod.RenameScan = ev["scan"]
        self.folder_plans = scan.plans
        if not ev.get("auto") or not scan.plans:
            return
        self._join_worker()
        n = sum(1 for p in scan.plans if not p.skip)
        if not messagebox.askyesno(
                "确认执行",
                f"将重命名 {n} 个文件夹（详见日志），是否继续？\n"
                "（可通过「回退重命名」恢复）"):
            return
        self._reset_counts()
        plans, self.folder_plans = scan.plans, []
        self._start_worker(self._worker_ren_execute, plans, Path(ev["root"]))

    def _start_ren_undo(self):
        root_dir = self._get_root_dir()
        if root_dir is None:
            return
        if not messagebox.askyesno("确认回退", "回退最近一批文件夹重命名（新名改回旧名）？"):
            return
        self._start_worker(self._worker_ren_undo, root_dir)

    def _worker_ren_undo(self, root_dir: Path):
        rename_mod.undo_last(root_dir, self._wlog)

    def _worker_ren_execute(self, plans, root_dir: Path):
        self._wlog("—— 开始执行文件夹重命名 ——", "info")

        def log_and_count(msg, tag):
            self._wlog(msg, tag)
            if msg.startswith("[重命名]"):
                self._emit(type="count", kind="成功")
            elif msg.startswith("[失败]"):
                self._emit(type="count", kind="失败")
            elif msg.startswith("[跳过]"):
                self._emit(type="count", kind="跳过")

        result = rename_mod.execute_plans(plans, log_and_count,
                                          should_stop=self.stop_event.is_set)
        for p in plans:
            if p.result:  # 空 result 表示因停止而未执行，不计入报告
                self.report_records.append(ReportRecord(str(p.path), "重命名", p.result, p.note))
        # 写入重命名历史（根目录下 .rename_history.json），供一键回退
        saved = rename_mod.record_history(root_dir, plans)
        if saved:
            self._wlog(f"已保存重命名历史 {saved} 条，可通过「回退重命名」恢复", "info")
        self._wlog(f"—— 重命名完成：成功 {result.renamed}，跳过 {result.skipped}，"
                   f"失败 {result.failed} ——", "info")

    # ================= 报告导出 =================

    def _export_report(self):
        if not self.report_records:
            messagebox.showinfo("提示", "当前没有可导出的处理记录")
            return
        path = filedialog.asksaveasfilename(
            title="导出处理报告",
            defaultextension=".csv",
            initialfile="GameArchiveTool处理报告.csv",
            filetypes=[("CSV 文件", "*.csv")],
        )
        if not path:
            return
        try:
            export_csv(self.report_records, Path(path))
            self._log(f"报告已导出：{path}（共 {len(self.report_records)} 条）", "success")
        except OSError as e:
            messagebox.showerror("错误", f"导出失败：{e}")
