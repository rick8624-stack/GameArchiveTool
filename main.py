# -*- coding: utf-8 -*-
"""GameArchiveTool 入口：创建主窗口并启动事件循环。

pyinstaller 打包命令见 README.md。
"""

from gui import App, create_root


def main():
    root = create_root()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
