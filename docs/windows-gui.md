# StemFlow Windows GUI 安装版

Windows GUI 版面向不需要接触 Python、PowerShell、Web 或命令行的用户。

## 使用方式

1. 运行 `StemFlow-Setup-<版本>-x64.exe`；
2. 从开始菜单或桌面打开 StemFlow；
3. 选择输入文件夹和输出文件夹；
4. 选择仅 CPU、仅 NVIDIA GPU 或 CPU + GPU；
5. 查看内存/显存评估并设置 CPU、GPU 并发数；
6. 点击“开始处理”；
7. 完成后点击“打开输出文件夹”。

输入文件夹可以包含多级子目录。输出目录保留相对结构，并生成：

```text
原文件名_vocals_only.mp4
```

最终视频保留原画面，只使用 AI 分离后的人声音轨，不生成 BGM WAV。

## CPU、GPU、混合运行与实时并发

“运行设备”提供三种模式：

- **仅 CPU**：所有视频由 CPU worker 处理；
- **仅 NVIDIA GPU**：所有视频交给 CUDA worker；
- **CPU + NVIDIA GPU**：两类 worker 同时从同一任务队列领取视频。

CPU 和 GPU 并发数可以在运行期间调整。增加并发后会立即领取更多待处理视频；降低
并发不会强行中断正在处理的视频，而是在当前视频完成后停止补充超出新上限的 worker。
CPU 与 GPU 总并发最多为 8。

GUI 会读取 Windows 当前的物理内存总量、可用内存和 CPU 核心数，并显示：

- 当前可用内存；
- 所选并发数的预计内存占用；
- 建议并发数。

检测到 NVIDIA 显卡时，还会显示显卡型号、可用显存、GPU 并发预计显存和建议值。

内存估算按程序基础占用约 2 GB、每个 MDX 推理任务约 3 GB 计算。实际占用会受
视频长度、音频声道和模型影响，因此这是安全配置参考，不是硬性限制。用户可以选择
1–8 个并发任务；若预计需要的内存超过当前可用内存，启动前会再次确认。

为避免多个推理任务同时占满所有 CPU 核心，程序会根据并发数自动分配每个 worker
可使用的 CPU 线程。例如 16 核 CPU、4 个并发时，每个 worker 使用约 4 个 CPU
线程。

## 逐集实时记录

任务表每 0.75 秒刷新一次，每一集实时显示：

- 当前阶段：排队、提取音轨、分离人声、合成视频、完成或失败；
- 实际执行设备：CPU 或 CUDA；
- 当前耗时或最终耗时；
- 成功输出路径；
- 失败错误原因。

状态持续写入 SQLite，阶段与错误写入滚动日志，并同步更新 Excel 报表。关闭 GUI
后重新打开，历史成功和失败记录仍然存在。

## 离线启用 NVIDIA CUDA

完整离线安装套件同时包含 CPU 环境和兼容的 CUDA PyTorch wheelhouse。检测到
NVIDIA 显卡后，在 GUI 点击“启用内置 CUDA”，程序会校验安装包内每个 wheel 的
SHA-256，再解压到独立运行环境；整个过程不需要联网。内置组件来自以下官方地址：

```text
https://download.pytorch.org/whl/cu128
```

地址会显示在界面中并可复制。CUDA 离线资源约 3.3 GB，安装后约占用 7–10 GB，
独立存放在：

```text
%ProgramData%\StemFlow\gpu-runtime
```

校验、安装和验证过程实时显示，并持久记录在：

```text
%ProgramData%\StemFlow\logs\cuda-install.log
```

CUDA 安装失败不会破坏 CPU 环境。GPU 任务使用独立 CUDA worker 进程，因此混合
模式能够真正让 CPU 和 GPU 同时处理不同视频。NVIDIA 驱动必须预先安装，且
`nvidia-smi` 应能正常显示显卡。

## 安装包包含

- Python 3.12 运行时；
- PyTorch CPU、ONNX Runtime 和 `python-audio-separator`；
- FFmpeg；
- `UVR-MDX-NET-Inst_HQ_3.onnx` 默认模型及模型参数；
- Microsoft Visual C++ 2015–2022 x64 运行库安装程序；
- GUI、SQLite 状态库、Excel 报告和日志功能；
- 完整 CUDA 12.8 PyTorch、TorchAudio、TorchVision、ONNX Runtime GPU 离线
  wheelhouse，以及官方下载来源说明。

安装后首次启动不需要再安装 Python、下载默认模型或下载 CUDA PyTorch。

由于官方 CUDA PyTorch wheel 单文件已超过 3 GB，而 GitHub Release 单文件上限为
2 GiB，完整离线套件由一个 `StemFlow-Setup-*-x64.exe` 和若干同名 `.bin` 分卷
组成。必须把 EXE 和全部 BIN 文件放在同一文件夹，再双击 EXE 安装。

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
├── gpu-runtime\
└── work\
```

卸载程序不会删除处理结果。若要彻底清除运行历史，可在卸载后手工删除
`C:\ProgramData\StemFlow`。

## 系统要求

- Windows 10/11 x64 或 Windows Server 2016/2019/2022/2025；
- 建议 8 核 CPU、16 GB 内存；4 个并发建议至少 16 GB 可用内存；
- 基础安装包约数百 MB，CPU 使用需要预留约 2–4 GB；
- 启用 CUDA 时额外预留 7–10 GB；
- 没有 NVIDIA GPU 时直接选择“仅 CPU”。

## 构建安装包

在 Windows 10/11 或 Windows Server 上安装 Python 3.12 与 Inno Setup 6，
然后运行：

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
.\scripts\windows\build-gui-installer.ps1 -Version "1.4.0"
```

生成文件：

```text
dist\installer\StemFlow-Setup-1.4.0-x64.exe
dist\installer\StemFlow-Setup-1.4.0-x64-1.bin
dist\installer\StemFlow-Setup-1.4.0-x64-2.bin
```

也可以手动触发 GitHub Actions 的 `Build Windows GUI installer` 工作流。
