# StemFlow Windows GUI 安装版

Windows GUI 版面向不需要接触 Python、PowerShell、Web 或命令行的用户。

## 使用方式

1. 运行 `StemFlow-Setup-<版本>-x64.exe`；
2. 从开始菜单或桌面打开 StemFlow；
3. 选择输入文件夹和输出文件夹；
4. 点击“开始处理”；
5. 完成后点击“打开输出文件夹”。

输入文件夹可以包含多级子目录。输出目录保留相对结构，并生成：

```text
原文件名_vocals_only.mp4
```

最终视频保留原画面，只使用 AI 分离后的人声音轨，不生成 BGM WAV。

## 安装包包含

- Python 3.12 运行时；
- PyTorch CPU、ONNX Runtime 和 `python-audio-separator`；
- FFmpeg；
- `UVR-MDX-NET-Inst_HQ_3.onnx` 默认模型及模型参数；
- Microsoft Visual C++ 2015–2022 x64 运行库安装程序；
- GUI、SQLite 状态库、Excel 报告和日志功能。

安装后首次启动不需要再安装 Python，也不需要下载默认模型。

## 定时任务

勾选“每天自动运行”，填写 `HH:MM`，再点击“保存定时”。Windows Server
上需要右键 StemFlow，选择“以管理员身份运行”一次，以便注册 SYSTEM 计划任务。

定时任务会在无人登录时运行。输入和输出目录应使用本机磁盘路径；SYSTEM
账户通常无法访问映射盘符。

## 运行数据

```text
C:\ProgramData\StemFlow\
├── config.json
├── processing.db
├── processing_status.xlsx
├── models\
├── logs\
└── work\
```

卸载程序不会删除处理结果。若要彻底清除运行历史，可在卸载后手工删除
`C:\ProgramData\StemFlow`。

## 系统要求

- Windows 10/11 x64 或 Windows Server 2016/2019/2022/2025；
- 建议 8 核 CPU、16 GB 内存；
- 安装包约数百 MB，安装后需要预留约 2–4 GB；
- CPU 版无需 NVIDIA GPU，也无需 CUDA。

## 构建安装包

在 Windows 10/11 或 Windows Server 上安装 Python 3.12 与 Inno Setup 6，
然后运行：

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
.\scripts\windows\build-gui-installer.ps1 -Version "1.1.0"
```

生成文件：

```text
dist\installer\StemFlow-Setup-1.1.0-x64.exe
```

也可以手动触发 GitHub Actions 的 `Build Windows GUI installer` 工作流。
