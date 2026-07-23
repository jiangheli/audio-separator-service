# StemFlow Video BGM Remover

Windows Server 无人值守视频去 BGM 工具。系统定时扫描固定文件夹，使用
[`python-audio-separator`](https://github.com/nomadkaraoke/python-audio-separator)
分离人声，然后把人声音轨重新合成到原视频画面中。

最终产物是视频，不再交付 `vocals.wav` 或 `instrumental.wav`：

```text
原视频画面 + AI 分离的人声音轨 = *_vocals_only.mp4
```

## 工作流程

```text
Windows Task Scheduler
        ↓
扫描固定输入目录
        ↓
等待文件复制完成
        ↓
临时提取原视频音频
        ↓
python-audio-separator 分离 vocals
        ↓
FFmpeg 删除原音轨并合成 vocals
        ↓
输出 *_vocals_only.mp4
        ↓
更新 SQLite、Excel 和滚动日志
        ↓
删除全部临时 WAV/stems
```

系统没有 Web UI、FastAPI、Redis 或 Docker 依赖。任务由 Windows 计划任务直接调用本机 Python 环境；关闭 PowerShell 窗口不影响以后定时执行。

## Windows Server 一键安装

要求：

- Windows Server 2019/2022/2025 或 Windows 10/11；
- 管理员 PowerShell；
- CPU 模式建议 8 核、16 GB 内存；
- GPU 模式需要 NVIDIA 驱动，建议至少 8 GB 显存；
- 首次安装和首次模型推理需要联网。

在管理员 PowerShell 中进入项目目录：

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force

.\install-windows.ps1 `
  -InputPath "D:\VideoInput" `
  -OutputPath "D:\VideoOutput" `
  -ScheduleTime "00:00" `
  -Mode "cpu"
```

如果 Windows Server 没有 Python 且没有 `winget`，先右键
`install-python-windows.cmd`，选择“以管理员身份运行”。该脚本会从 Python
官网下载 Python 3.12 x64、验证数字签名、静默安装并检查版本。完成后再运行
`install-windows.ps1`。

GPU 安装：

```powershell
.\install-windows.ps1 `
  -InputPath "D:\VideoInput" `
  -OutputPath "D:\VideoOutput" `
  -ScheduleTime "00:00" `
  -Mode "gpu"
```

脚本会：

1. 检测 Python 3.10–3.12，缺失时尝试通过 `winget` 安装 Python 3.12；
2. 创建 `.venv-windows` 隔离环境；
3. 安装 `python-audio-separator`、ONNX Runtime 和内置 FFmpeg；
4. GPU 模式安装 CUDA PyTorch 并验证 `torch.cuda.is_available()`；
5. 生成 `config\stemflow.json`；
6. 注册每天运行的 `StemFlow-Video-BGM-Removal` 计划任务；
7. 设置 `IgnoreNew`，上一批未完成时不会启动重叠任务；
8. 验证配置、模型运行库和 FFmpeg。

默认计划任务以 `SYSTEM` 身份运行，因此服务器无人登录时也能执行。`SYSTEM`
通常无法访问需要个人凭证的网络共享；UNC/NAS 路径请改用专用服务账号。

## 立即运行

安装后无需等待凌晨：

```powershell
.\run-now.ps1
```

兼容入口：

```cmd
start-windows.cmd
```

只处理本轮前两个待处理视频：

```powershell
.\run-now.ps1 -MaxFiles 2
```

## 查看状态

```powershell
.\status.ps1
.\status.ps1 -Json
```

运行状态保存在：

```text
data\
├── processing.db
├── processing_status.xlsx
├── logs\
│   └── stemflow-video.log
├── models\
└── work\
```

日志每天轮换，保留 30 天。Excel 包含源文件、状态、重试次数、处理耗时、输出视频和错误信息。如果 Excel 正在被桌面 Excel 独占打开，本轮视频处理仍会继续，日志会提示关闭文件后重新生成：

```powershell
.\.venv-windows\Scripts\stemflow-video.exe report `
  --config .\config\stemflow.json
```

## 输入和输出

输入：

```text
D:\VideoInput\
├── video1.mp4
├── video2.mov
└── series\
    └── video3.mkv
```

输出保持相对目录结构：

```text
D:\VideoOutput\
├── video1_vocals_only.mp4
├── video2_vocals_only.mp4
└── series\
    └── video3_vocals_only.mp4
```

支持：`mp4`、`mov`、`mkv`、`avi`、`m4v`、`webm`。

视频画面优先使用 FFmpeg stream copy，不重新编码；如果原视频编码无法装入 MP4，自动回退为 H.264。新音轨编码为 AAC 44.1 kHz 双声道。人声长度不足时自动补静音，保持完整视频时长。

## 去重、重试和长任务保护

- 指纹由绝对路径、大小和修改时间生成；
- 相同版本视频成功后不会重复处理；
- 同一路径的视频被替换后会作为新版本处理；
- 默认等待文件最后修改时间稳定 120 秒，避免处理尚未复制完成的视频；
- 默认失败后重试 3 次；
- 进程异常中断的任务会在下一次运行时标记失败并重试；
- Python 文件锁和计划任务 `IgnoreNew` 双重防止批次重叠；
- 输出视频缺失时会自动重建；
- 原视频永远只读，不删除、不移动、不覆盖。

## 配置

安装脚本生成的 `config\stemflow.json` 不进入 Git。模板见
[`config/stemflow.example.json`](config/stemflow.example.json)。

主要字段：

| 字段 | 说明 |
| --- | --- |
| `input_dir` | 固定监控目录 |
| `output_dir` | 最终视频目录 |
| `data_dir` | 日志、模型、状态和临时文件目录 |
| `model` | `mdx`、`roformer` 或上游模型文件名 |
| `schedule_time` | 每天执行时间，24 小时制 |
| `stable_seconds` | 文件稳定等待秒数 |
| `max_retries` | 首次失败后的重试次数 |
| `output_suffix` | 默认 `_vocals_only` |
| `video_copy` | 优先直拷视频画面 |
| `keep_failed_work` | 是否保留失败任务的临时 WAV |

CPU 默认使用 `mdx`，内存压力较低。GPU 默认使用 `roformer`，复杂影视对白中通常能得到更干净的人声。模型推理逻辑没有修改，StemFlow 只负责扫描、状态、FFmpeg 临时提取和视频重组。

## 卸载

仅删除计划任务，保留模型、日志、数据库和结果：

```powershell
.\uninstall-windows.ps1
```

同时删除 Python 环境：

```powershell
.\uninstall-windows.ps1 -RemoveEnvironment
```

删除全部运行数据需要明确添加：

```powershell
.\uninstall-windows.ps1 -RemoveEnvironment -RemoveData
```

卸载脚本支持 PowerShell `-WhatIf`。

## 项目结构

```text
audio-separator-service\
├── backend\
│   ├── app\
│   │   ├── cli\
│   │   ├── services\
│   │   ├── config.py
│   │   ├── repository.py
│   │   ├── report.py
│   │   └── runner.py
│   ├── tests\
│   └── pyproject.toml
├── config\
│   └── stemflow.example.json
├── scripts\windows\
│   ├── install.ps1
│   ├── run-now.ps1
│   ├── status.ps1
│   └── uninstall.ps1
├── install-windows.ps1
├── run-now.ps1
├── status.ps1
└── uninstall-windows.ps1
```

更完整的 Windows Server 运维说明见
[`docs/windows-server.md`](docs/windows-server.md)。
