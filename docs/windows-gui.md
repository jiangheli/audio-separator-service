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

勾选“每个子文件夹处理完成后，各生成一个人声版合集”时，单集文件仍会保留，
并在每个原始视频所在的子文件夹对应输出目录中增加：

```text
子文件夹名_合集_vocals_only.mp4
```

各子文件夹独立合并，按文件名中的数字自然排序（例如第 2 集排在第 10 集前）。
失败的单集会跳过并写入日志，不会混入其他子文件夹的合集。输入根目录中直接存放的
视频使用输入根目录名称生成一个根目录合集。已存在且所有输入都未变化的合集不会
重复构建。

## CPU、GPU、混合运行与实时并发

“运行设备”提供三种模式：

- **仅 CPU**：所有视频由 CPU worker 处理；
- **仅 NVIDIA GPU**：所有视频交给 CUDA worker；
- **CPU + NVIDIA GPU**：两类 worker 同时从同一任务队列领取视频。

CPU 和 GPU 并发数可以在运行期间调整。增加并发后会立即领取更多待处理视频；降低
并发不会强行中断正在处理的视频，而是在当前视频完成后停止补充超出新上限的 worker。
CPU 和 GPU 都可以直接输入任意非负整数，不设固定上限。单项可以设为 `0` 以禁用
该设备，例如 `CPU 0 + GPU 1` 或 `CPU 4 + GPU 0`；两项不能同时为 `0`。

### 常驻 GPU 与三级流水线

GPU 模式启动批次时会先启动长期运行的 CUDA worker，通过
`python-audio-separator` 的公开 Python API 加载一次模型。后续视频复用同一个
进程和模型，不再重复导入 PyTorch、初始化 CUDA 或加载 ONNX 模型。运行日志会
记录 CUDA Provider、显卡名称、模型加载时间、整卡显存使用量、每条推理耗时，
以及 GPU 平均/峰值利用率和 PyTorch 峰值显存。

GPU 视频采用有界三级流水线：

```text
CPU 音频预处理（默认 2）
        ↓
常驻 GPU 模型推理（8 GB 显卡默认 1）
        ↓
CPU 视频合成（默认 2）
```

最多提前准备 2 条音频，避免任务缓存无限占用内存。GUI 的“GPU 流水线”可调整
预处理线程、合成线程、预取数量、GPU 辅助 CPU 线程、GPU 批量和 GPU 分块。
GPU 路径默认采用模型原生的 MDX 分块 `256`，避免分块不匹配触发 ONNX 转
PyTorch 慢路径；默认批量为 `2`，同一轮推理处理更多音频块。RTX 4060/5060 Ti
8 GB 建议从
`CPU模型 0、GPU 1、预处理 2、合成 2、预取 2、GPU辅助CPU 4、批量 2、分块 256`
开始。

如果日志显示显存仍有明显余量且 GPU 平均利用率偏低，可先把“GPU 批量”提高到
`3` 或 `4`；若出现 CUDA out of memory，则退回 `1` 或 `2`。8 GB 显卡通常保持
GPU worker 为 `1`，优先调批量而不是同时加载多个模型。

人声合成首先使用 `-c:v copy` 直接复制原画面，不重新编码、不损失画质。源视频
无法直接封装时，GPU 模式先尝试 `h264_nvenc`，不可用时再回退 `libx264`。
STFT、iSTFT 和频谱张量是否完整留在 CUDA 由底层
`python-audio-separator`/模型实现决定；StemFlow 只设置它公开支持的 CUDA
设备和 ONNX `CUDAExecutionProvider`，不修改上游核心推理逻辑。

GUI 会读取 Windows 当前的物理内存总量、可用内存和 CPU 核心数，并显示：

- 当前可用内存；
- 所选并发数的预计内存占用；
- 建议并发数。

检测到 NVIDIA 显卡时，还会显示显卡型号、可用显存、GPU 并发预计显存和建议值。

内存估算按程序基础占用约 2 GB、每个 MDX 推理任务约 3 GB 计算。实际占用会受
视频长度、音频声道和模型影响，因此这是安全配置参考，不是硬性限制。用户可以选择
任意并发数；若预计需要的内存或显存超过当前可用量，启动前会再次确认，但不会强制
限制用户选择。

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

### CUDA 修复与诊断

StemFlow 1.4.2 会在启用前检查 NVIDIA 驱动版本、离线资源和运行磁盘空间。
RTX 50 系列使用内置 CUDA 12.8 运行环境时，驱动至少需要 570.65。这里只需要
安装 NVIDIA 官方显卡驱动，不需要另外安装 CUDA Toolkit，也不要手工解压
PyTorch wheel。

安装过程会在界面和日志中显示“校验资源、解压 Python、安装组件、验证 GPU”
四个阶段。如果已经安装过 StemFlow 1.4.0 完整离线套件，可以直接覆盖安装
`StemFlow-Repair-1.4.2-x64.exe`。修复包复用原安装目录中的 CUDA wheelhouse，
不需要重新下载约 4 GB 的完整套件。安装前请关闭 StemFlow，并保证系统盘至少
有 12 GB 可用空间；修复后打开程序，点击“重新启用 CUDA”。

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

## 软件内更新

StemFlow 启动后会在后台检查 GitHub 最新正式 Release，也可以点击窗口右上角的
“检查更新”。发现新版本后只下载：

```text
StemFlow-Update-<版本>-x64.exe
StemFlow-Update-<版本>-x64.exe.sha256
```

程序先验证 SHA-256，再启动更新安装程序。更新软件本体不会重新下载 3.3 GB 的
CUDA wheelhouse，也不会删除或重装 `%ProgramData%\StemFlow\gpu-runtime`。
GPU worker 的少量应用代码会自动同步到已有运行环境，但 PyTorch、TorchAudio、
TorchVision 和 ONNX Runtime GPU 继续复用原来的安装。

只有未来 Release 明确声明升级 CUDA/PyTorch 运行时版本时，才需要单独升级 GPU
组件。普通功能修复、界面更新和处理逻辑更新都使用轻量更新包。

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
.\scripts\windows\build-gui-installer.ps1 -Version "1.7.1"
```

生成文件：

```text
dist\installer\StemFlow-Setup-1.7.1-x64.exe
dist\installer\StemFlow-Setup-1.7.1-x64-1.bin
dist\installer\StemFlow-Setup-1.7.1-x64-2.bin
dist\update\StemFlow-Update-1.7.1-x64.exe
dist\update\StemFlow-Update-1.7.1-x64.exe.sha256
```

也可以推送 `gui-v<版本>` 标签触发 GitHub Actions。标签构建成功后会自动创建
GitHub Release，同时上传完整 GPU 安装分卷、轻量更新包和 SHA-256 校验文件。
