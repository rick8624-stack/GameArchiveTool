# GameArchiveTool - 游戏压缩包批量处理工具

Windows GUI 工具，对多层文件夹中的游戏压缩包（Xbox360 游戏等）完成
**文件名预处理 → 批量解压（密码池）→ 文件夹批量重命名** 的完整流水线。

## 运行环境

- Windows，Python 3.10+
- 已安装 [7-Zip](https://www.7-zip.org/)（默认路径 `C:\Program Files\7-Zip\7z.exe`，可在界面中修改）
- GUI 基于标准库 tkinter，**无必需第三方依赖**；可选安装 `tkinterdnd2` 启用拖拽文件夹进窗口：

```bash
pip install -r requirements.txt   # 可选，仅为启用拖拽
python main.py
```

## 使用说明

顶部选择（或拖入）**根目录**，然后按需使用三个选项卡。所有耗时操作都在后台线程执行，
可随时点击进度区的 **停止** 按钮中断（当前文件处理完后停止）。

### 1. 预处理（文件名清理）

递归扫描根目录，去掉文件名末尾的多余字符（默认去掉"删"字，
如 `游戏.7z.001删` → `游戏.7z.001`）。

- 规则可改为任意字符，勾选"按正则解释"后按正则表达式匹配（自动锚定到文件名末尾）
- 先点 **预览** 查看「原名 → 新名」列表，确认无误后点 **执行重命名**

### 2. 批量解压

递归扫描所有压缩文件并逐个解压：

- 支持 zip / rar / 7z，以及分卷（`.7z.001`、`.zip.001`、`游戏名.part1.rar`、
  裸命名的 `part1.rar`/`part01.rar`、`.rar`+`.r00` 旧式分卷）。
  分卷只处理首卷（.001 / part1），后续卷由 7z 自动串联；
  裸命名分卷（文件名就是 partN.rar）按所在文件夹归为一组
- **密码池**：先尝试无密码，再按命中次数从高到低逐个用 `7z t` 测试验证，
  验证通过才真正解压；命中的密码计数 +1 并持久化，常用密码自动前移
- 默认解压到压缩包所在文件夹，可勾选"解压到以压缩包名命名的子文件夹"
- 可勾选"解压成功后删除原压缩包"（含分卷全部文件），默认关闭
- 失败的压缩包记入失败清单（区分密码错误 / 压缩包损坏 / 疑似分卷不完整），不中断整体流程
- **断点续传**：每完成一个压缩包即写入 `progress.json`；中途退出后再次开始解压时，
  会询问是否跳过已完成项
- 任务结束后可点 **导出处理报告 CSV**（路径、操作类型、结果、使用的密码、耗时）

### 3. 批量重命名（编号对照表）

读取 CSV 对照表（两列：`编号,英文名`，UTF-8 编码，兼容 Excel 导出的带 BOM 文件），
把 `001.中文名` 式文件夹改名为 `001.英文名`：

- 编号部分保留原样（含前导零），匹配时自动去前导零（文件夹 `001` 匹配对照表的 `1`）
- 支持字母编号（`XB001`、`K001`、`KX01` 等，字母大小写不敏感）
- 新文件夹名自动清除 Windows 非法字符 `\/:*?"<>|`
- 未匹配到对照表的文件夹显示在右侧"待处理清单"
- 同样是先 **预览** 后 **执行**

## 配置文件

程序目录下自动生成（打包后位于 exe 同目录）：

- `config.json`：7z 路径、密码池（含命中计数）、上次使用的根目录、各开关状态
- `progress.json`：批量解压的断点续传记录，任务全部完成后自动清空

## pyinstaller 打包

```bash
pip install pyinstaller

# 未安装 tkinterdnd2（无拖拽）：
pyinstaller --onefile --windowed --name GameArchiveTool main.py

# 已安装 tkinterdnd2（带拖拽，需要收集其 tkdnd 二进制文件）：
pyinstaller --onefile --windowed --name GameArchiveTool --collect-all tkinterdnd2 main.py
```

生成的单文件 exe 位于 `dist\GameArchiveTool.exe`。
注意：exe 需要放在**可写目录**运行（config.json / progress.json 保存在 exe 旁边）。

## 项目结构

```
main.py                 入口
gui.py                  主窗口、线程/事件队列、进度与日志
utils.py                路径工具、非法字符清理、_MEIPASS 兼容
core/
  config.py             config.json 与密码池管理
  progress_store.py     progress.json 断点续传
  sevenzip.py           7z.exe 子进程封装、-bsp1 进度解析、失败分类
  preprocess.py         模块一：文件名清理
  extract.py            模块二：批量解压
  rename.py             模块三：编号对照表重命名
  report.py             处理报告 CSV 导出
```

## 测试与 CI/CD

本地运行测试（无第三方依赖，找不到 7z.exe 时解压流程测试自动跳过）：

```bash
python -m unittest discover -s tests -v
```

GitHub Actions 工作流：

- **CI**（`.github/workflows/ci.yml`）：push 到 main 或提 PR 时，在 windows-latest
  上用 Python 3.10 / 3.12 双版本跑语法检查与全部测试（运行器预装 7-Zip，
  解压流程为真实执行）
- **CD**（`.github/workflows/release.yml`）：推送 `v*` 标签时自动测试 → pyinstaller
  打包单文件 exe → 发布 GitHub Release 并附上 `GameArchiveTool.exe`：

  ```bash
  git tag v1.1.0
  git push origin v1.1.0
  ```

  也可在 GitHub 的 Actions 页面手动触发打包验证（只出构建产物，不发 Release）。

## 后续迭代方向（架构已预留）

- 游戏库管理：扫描解压后的文件夹生成清单（可在 `core/` 下新增模块，复用事件队列推 UI）
- NSZ/XCI 等格式转换：`core/sevenzip.py` 的子进程封装模式可直接套用到其他转换工具
- 多任务并行解压：`gui.py` 的 worker 框架可扩展为线程池，事件队列天然支持多生产者
