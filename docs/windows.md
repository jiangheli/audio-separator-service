# Windows 部署指南

StemFlow 支持 Windows 10/11。完整 Web 服务推荐使用 Docker Desktop；只需要命令行批处理时，可以使用 Windows 原生 Python CLI。

## 方案一：Docker Desktop Web 服务

### 准备

1. 安装 [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)。
2. 在 Docker Desktop 中使用 WSL 2 Linux containers 后端。
3. 下载或复制本项目到本机磁盘，避免放在只读目录。

### CPU 启动

双击项目根目录的 `start-windows.cmd`，或者在 PowerShell 中运行：

```powershell
.\start-windows.cmd
```

默认目录：

| Windows 主机 | Web 中填写 |
| --- | --- |
| `<项目>\data\input` | `/input` |
| `<项目>\data\output` | `/output` |

指定其他目录：

```powershell
.\start-windows.cmd `
  -InputPath "D:\Media\Videos" `
  -OutputPath "D:\Media\Separated" `
  -Workers 2
```

脚本会创建不存在的目录，并以只读方式挂载输入目录。输出目录可写，模型和任务数据库保存在项目的 `data` 目录。

启动完成后打开：

- Web：<http://localhost:3000>
- API：<http://localhost:8000/docs>

需要自动监控时，进入 Web 的“自动处理”页面：

1. 监控目录填写 `/input`；
2. 输出目录填写 `/output`；
3. 默认选择“每天固定时间”，设置北京时间，例如 `00:00`；
4. 设置失败重试次数；
5. 勾选“启用后台计划”并保存。

脚本通过 `-InputPath` 指定的 Windows 文件夹会映射为 `/input`。Excel 状态表保存在项目的 `data\processing_status.xlsx`，也可以直接在 Web 下载。定时器运行在后端容器内，关闭浏览器不会影响凌晨任务；自动任务不会删除或覆盖监控目录里的原始文件。

如果端口被占用：

```powershell
.\start-windows.cmd -WebPort 3100 -ApiPort 8100
```

### NVIDIA GPU 启动

Windows GPU 模式需要：

- NVIDIA 显卡及最新的 Windows 驱动；
- WSL 2；
- Docker Desktop Linux containers。

NVIDIA 的 CUDA on WSL 指南说明，Windows 主机只需要安装 Windows NVIDIA 驱动，不应在 WSL 中另外安装 Linux 显示驱动：<https://docs.nvidia.com/cuda/wsl-user-guide/index.html>

启动：

```powershell
.\start-windows.cmd -Mode gpu `
  -InputPath "D:\Media\Videos" `
  -OutputPath "D:\Media\Separated" `
  -Workers 1
```

启动脚本最后会打印：

```text
CUDA available: True
Device: NVIDIA ...
```

只有 `CUDA available: True` 才表示 GPU worker 已经正确使用显卡。如果显示 `False`，先更新 Windows NVIDIA 驱动、执行 `wsl --update`，然后重启 Docker Desktop。

`-Workers` 表示独立推理槽位数量。CPU 默认2，GPU默认1。每增加一个槽位都可能再加载一份模型；GPU显存不足时保持 `-Workers 1`。

### 停止

```powershell
.\stop-windows.cmd
```

停止不会删除 `data/models`、`data/output`、任务数据库或 Redis 数据卷。

## 方案二：Windows 原生 CLI

要求 Python 3.10 或更高版本。双击：

```text
setup-cli-windows.cmd
```

脚本会：

1. 创建 `.venv-windows`；
2. 安装 StemFlow 和 CPU ONNX Runtime；
3. 安装包含 Windows FFmpeg 可执行文件的 `imageio-ffmpeg`；
4. 验证 FFmpeg 路径。

处理文件夹：

```powershell
.\.venv-windows\Scripts\audio-separator-service.exe `
  --input "C:\Users\me\Videos" `
  --output "D:\StemFlow-Results" `
  --model mdx
```

处理单文件：

```powershell
.\.venv-windows\Scripts\audio-separator-service.exe `
  --input "C:\Users\me\Videos\demo.mp4" `
  --output "D:\StemFlow-Results" `
  --model mdx
```

Windows 原生 CLI 直接识别 Windows 绝对路径，不使用 `/input`、`/output`，也不依赖 Redis。

## 常见问题

### Web 提示输入路径不存在

Docker Web 中必须填写 `/input`，不能填写 `D:\Videos`。Windows 路径通过 `start-windows.cmd -InputPath ...` 映射。

### Docker 无法共享 D 盘

确认 Docker Desktop 可以访问该目录，并且目录没有被企业安全软件或 Windows Controlled Folder Access 阻止。也可以先把测试文件复制到项目的 `data\input`。

### GPU 容器启动失败

在 PowerShell 中确认：

```powershell
wsl --status
wsl --update
nvidia-smi
```

然后重新执行 `start-windows.cmd -Mode gpu`。不要在 WSL 里安装第二套 NVIDIA 显示驱动。

### 首次任务很慢

第一次使用模型会下载数十 MB 到数百 MB 的权重并写入 `data\models`。后续任务会复用缓存。
