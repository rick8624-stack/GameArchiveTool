# -*- coding: utf-8 -*-
"""progress.json：断点续传支持。

批量解压过程中，每完成一个压缩包就把其路径写入 progress.json；
重新启动任务时询问用户是否跳过已完成项，任务全部完成后清空。
"""

import json
from pathlib import Path

from utils import app_dir

PROGRESS_FILE = "progress.json"


class ProgressStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / PROGRESS_FILE)
        self.completed: set[str] = set()
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.completed = set(data.get("completed", []))
            except (json.JSONDecodeError, OSError):
                self.completed = set()

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps({"completed": sorted(self.completed)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def mark_done(self, archive_path: str) -> None:
        """每完成一个压缩包立即落盘，进程中途被杀也不丢进度。"""
        self.completed.add(archive_path)
        self._save()

    def is_done(self, archive_path: str) -> bool:
        return archive_path in self.completed

    def clear(self) -> None:
        self.completed = set()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
