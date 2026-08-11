# Windows Server 部署与运维

## 1. 推荐环境

| 场景 | CPU | 内存 | GPU | 建议模型 |
| --- | ---: | ---: | ---: | --- |
| 少量测试 | 4 核 | 8 GB | 无 | MDX |
| 每日批处理 | 8 核以上 | 16 GB | 无 | MDX |
| 高质量人声 | 8 核以上 | 16–32 GB | NVIDIA 8 GB+ | Roformer |

磁盘需要容纳原视频、最终视频、模型和临时 WAV，建议空闲空间至少为待处理视频总量的 3 倍。

服务器必须保持开机。计划任务设置了 `StartWhenAvailable`，如果凌晨关机，服务器恢复后会补跑；但服务器休眠、关机期间无法处理。

## 2. 安装

如果服务器没有 Python 3.10–3.12，而且没有 `winget`，先在资源管理器中右键：

```text
install-python-windows.cmd
```

选择“以管理员身份运行”。脚本会从 Python 官网下载 Python 3.12 x64、验证
安装包数字签名、静默安装并检查版本。安装成功后继续执行下面的项目安装命令。

用管理员 PowerShell：

```powershell
cd C:\StemFlow\audio-separator-service
Set-ExecutionPolicy Bypass -Scope Process -Force

.\install-windows.ps1 `
  -InputPath "D:\IncomingVideos" `
  -OutputPath "E:\VocalsOnlyVideos" `
  -DataPath "E:\StemFlowData" `
  -ScheduleTime "00:00" `
  -Mode "cpu"
```

使用自定义任务名称：

```powershell
.\install-windows.ps1 `
  -InputPath "D:\IncomingVideos" `
  -OutputPath "E:\VocalsOnlyVideos" `
  -TaskName "Nightly-Remove-BGM" `
  -ScheduleTime "01:30"
```

安装但暂不注册计划任务：

```powershell
.\install-windows.ps1 `
  -InputPath "D:\IncomingVideos" `
  -OutputPath "E:\VocalsOnlyVideos" `
  -SkipScheduledTask
```

安装完成后立即跑一批：

```powershell
.\install-windows.ps1 `
  -InputPath "D:\IncomingVideos" `
  -OutputPath "E:\VocalsOnlyVideos" `
  -RunNow
```

## 3. 计划任务安全边界

默认使用 `SYSTEM`：

- 无需用户登录；
- 可以访问本机 NTFS 目录；
- 通常不能访问要求个人账号认证的 SMB/UNC 共享；
- 访问 NAS 时应把计划任务改为专用域账号，并给输入、输出和数据目录授予权限。

计划任务使用 `IgnoreNew`。如果前一天的批次仍未结束，第二天的触发不会创建第二个推理进程。应用内部还有文件锁保护手动执行与计划任务同时启动的情况。

GUI 安装版的 CPU、GPU 并发都允许设置为 `0`，但不能同时为 `0`；手动输入不设
固定上限，并根据当前可用内存、显存和 CPU 核心数显示建议值。多个 worker 各自
加载独立模型实例；内存不足的服务器应选择 1 个并发。计划任务会使用 GUI 中最后
保存的并发数。

## 4. 日常命令

```powershell
# 立即扫描并处理全部待处理视频
.\run-now.ps1

# 查看最近20条状态
.\status.ps1

# JSON输出
.\status.ps1 -Json

# 重新生成Excel
.\.venv-windows\Scripts\stemflow-video.exe report `
  --config .\config\stemflow.json

# 验证FFmpeg和Python运行库
.\.venv-windows\Scripts\stemflow-video.exe validate `
  --config .\config\stemflow.json
```

## 5. 日志和故障排查

默认日志：

```text
data\logs\stemflow-video.log
```

PowerShell 查看最后100行：

```powershell
Get-Content .\data\logs\stemflow-video.log -Tail 100
```

常见错误：

### 没有音轨

FFmpeg 会报告找不到 `0:a:0`。任务记为失败，原视频不会被改动。确认输入视频确实包含音轨。

### Excel 无法更新

桌面 Excel 可能独占 `.xlsx`。关闭工作簿后执行 `report` 命令；视频处理和 SQLite 状态不会丢失。

### GPU 不可用

```powershell
.\.venv-windows\Scripts\python.exe -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
```

如果为 `False`：

1. 更新 NVIDIA Windows Server 驱动；
2. 确认 `nvidia-smi` 正常；
3. 重新执行 GPU 安装；
4. 如 CUDA wheel 版本不适合当前驱动，通过 `-CudaWheelIndex` 指定兼容版本。

### 文件持续显示等待写入

系统默认要求文件最后修改时间超过 120 秒。大文件复制完成后等待下一次扫描，或在配置中降低 `stable_seconds`。

## 6. 输出策略

输出固定为 MP4：

```text
原文件名 + _vocals_only.mp4
```

原画面编码能放入 MP4 时直接复制，速度快且没有画质损失；否则转码为 H.264。原音轨完全移除，只合成人声 stem。环境音、音效及音乐可能一起被模型移除，复杂素材中仍可能有少量 BGM 残留。
