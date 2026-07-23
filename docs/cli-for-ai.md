# StemFlow CLI 与 AI Agent 接入指南

本文档描述 StemFlow CLI 1.0 机器接口。它面向 Codex、Claude Code、OpenAI Function Calling、自定义 Agent、自动化脚本和工作流平台，也可以由人直接使用。

## 1. 能力范围

CLI 暴露系统的完整处理闭环：

1. 发现支持的格式、模型、命令和退出码；
2. 直接处理本机单文件或文件夹；
3. 调用已经启动的 StemFlow 服务创建异步任务；
4. 查询任务、文件级进度和日志；
5. 等待任务进入终态；
6. 获取可播放、可下载的结果 URL；
7. 下载 `vocals.wav` 和 `instrumental.wav` 到 Agent 可访问的目录。

CLI 不复制模型逻辑。所有本机推理继续调用 `AudioSeparatorService`，服务任务继续经过 FastAPI、Redis、worker 和 `python-audio-separator`。

## 2. 两种调用模式

### 2.1 服务模式，推荐给 AI

AI 通过 CLI 调用正在运行的 Web 服务。任务在独立 worker 中执行，即使发起命令退出，任务也不会丢失。

```text
AI Agent -> stemflow CLI -> FastAPI -> Redis -> Worker -> 结果文件
```

适合：长任务、批处理、Web 与 AI 共用任务记录、需要查询进度和下载地址。

### 2.2 本机同步模式

CLI 在当前进程直接加载模型，命令返回时分离已经完成。

```text
AI Agent -> stemflow separate -> AudioSeparatorService -> 结果文件
```

适合：无需启动 Web/Redis 的单机脚本、临时批处理。模型推理期间调用进程不能退出。

## 3. 安装和调用入口

### 3.1 Docker 部署

先启动服务：

```bash
docker compose up -d --build
```

项目根目录提供包装命令，自动在 backend 容器中执行 CLI：

macOS / Linux：

```bash
./stemflow health --json
```

Windows：

```powershell
.\stemflow.cmd health --json
```

也可以直接执行：

```bash
docker compose exec -T backend stemflow health --json
```

`-T` 会关闭伪终端，适合 AI 捕获干净的 JSON stdout。

### 3.2 本机 Python 安装

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[cpu]"
stemflow --version
```

Windows 可运行项目根目录的 `setup-cli-windows.cmd`，然后使用：

```powershell
.\.venv-windows\Scripts\stemflow.exe capabilities --json
```

### 3.3 API 地址

服务命令默认连接 `http://localhost:8000`。可使用参数或环境变量覆盖：

```bash
stemflow health --api-url http://localhost:8001 --json
```

```bash
export STEMFLOW_API_URL=http://localhost:8001
stemflow health --json
```

在 Docker backend 容器内部，默认的 `localhost:8000` 正确指向同一容器中的 FastAPI。

当 CLI 访问服务的地址和 AI 最终访问结果链接的地址不同时，使用 `public_api_url` 或 `STEMFLOW_PUBLIC_API_URL`。例如 CLI 在 Docker 容器内访问 `localhost:8000`，而主机通过 `localhost:8001` 打开结果：

```bash
STEMFLOW_PUBLIC_API_URL=http://localhost:8001 stemflow task files TASK_ID --json
```

项目根目录的 `./stemflow` 和 `stemflow.cmd` 会自动把 Compose 暴露端口设置为公开地址。

## 4. 命令总览

| 命令 | 用途 | 是否需要服务 |
| --- | --- | --- |
| `stemflow capabilities` | 返回格式、模型、动作和退出码 | 否 |
| `stemflow separate` | 同步处理本机文件或目录 | 否 |
| `stemflow health` | 检查 FastAPI/任务模式 | 是 |
| `stemflow task list` | 最近任务列表 | 是 |
| `stemflow task create` | 创建后台任务 | 是 |
| `stemflow task get` | 查询任务进度 | 是 |
| `stemflow task wait` | 等待任务完成 | 是 |
| `stemflow task files` | 获取结果及下载 URL | 是 |
| `stemflow task logs` | 获取任务日志 | 是 |
| `stemflow task download` | 下载结果文件 | 是 |
| `stemflow agent` | 执行一个 JSON 动作 | 取决于动作 |

可随时运行 `stemflow <命令> --help` 查看参数。

## 5. AI JSON 协议

### 5.1 调用方法

推荐将请求 JSON 写入 stdin，避免 shell 引号和路径转义问题：

```bash
printf '%s' '{"action":"health"}' | stemflow agent
```

读取文件：

```bash
stemflow agent --request-file request.json
```

显式从 stdin 读取：

```bash
stemflow agent --request-file - < request.json
```

短请求也可以内联：

```bash
stemflow agent --request '{"action":"capabilities"}'
```

### 5.2 成功输出

stdout 始终只有一个 JSON 对象：

```json
{
  "ok": true,
  "action": "health",
  "data": {
    "status": "ok",
    "task_mode": "redis",
    "worker_concurrency": 2
  },
  "error": null,
  "meta": {
    "cli": "stemflow",
    "version": "0.2.0"
  }
}
```

### 5.3 失败输出

```json
{
  "ok": false,
  "action": "task.wait",
  "data": null,
  "error": {
    "code": 5,
    "message": "Timed out waiting for task abc123",
    "details": {
      "task": {
        "id": "abc123",
        "status": "running",
        "progress": 45
      }
    }
  },
  "meta": {
    "cli": "stemflow",
    "version": "0.2.0"
  }
}
```

AI 必须同时检查进程退出码和顶层 `ok`，不能只搜索文字 `completed`。

## 6. Agent 动作定义

### 6.1 `capabilities`

输入：

```json
{"action":"capabilities"}
```

返回支持的音视频扩展名、模型别名、命令、Agent 动作和退出码。Agent 第一次接入或 CLI 版本变化时，应先调用此动作。

### 6.2 `health`

```json
{
  "action": "health",
  "api_url": "http://localhost:8000",
  "request_timeout": 30
}
```

`api_url`、`public_api_url` 和 `request_timeout` 可省略。`api_url` 用于 CLI 连接服务，`public_api_url` 只用于返回给调用者的播放和下载链接。

### 6.3 `separate`

在 CLI 本机同步处理，不经过 FastAPI：

```json
{
  "action": "separate",
  "input": "/absolute/path/input/video.mp4",
  "output": "/absolute/path/results",
  "model": "mdx",
  "chunk_duration": 600
}
```

必填字段：

- `input`：CLI 进程可访问的文件或文件夹；
- `output`：CLI 进程可写的输出目录。

可选字段：

- `model`：默认 `default`；
- `chunk_duration`：长音频分块秒数；省略表示不主动分块。

成功时 `data.results[]` 中包含输入类型、输出目录、耗时和绝对结果路径。

### 6.4 `task.create`

创建服务后台任务后立即返回：

```json
{
  "action": "task.create",
  "input": "/input",
  "output": "/output",
  "model": "default"
}
```

创建并阻塞等待完成：

```json
{
  "action": "task.create",
  "input": "/input",
  "output": "/output",
  "model": "default",
  "wait": true,
  "poll_interval": 2,
  "timeout": 3600,
  "include_files": true
}
```

`input_dir`/`output_dir` 也可作为 `input`/`output` 的别名。

当使用 Docker 服务模式时，这两个路径是容器路径：

- `/input` 对应宿主机映射的输入目录，只读；
- `/output` 对应宿主机映射的结果目录，可写；
- 不要把 `C:\Videos` 或 `/Users/me/Videos` 直接提交给容器 API。

### 6.5 `task.list`

```json
{
  "action": "task.list",
  "limit": 20
}
```

`limit` 范围为 1–200。

### 6.6 `task.get`

```json
{
  "action": "task.get",
  "task_id": "ba9d472536ac478299998cdfb039a75c"
}
```

任务终态：

- `completed`：全部成功；
- `completed_with_errors`：部分文件失败，检查 `failed_files` 和 items/logs；
- `failed`：任务失败，CLI 返回退出码 5。

### 6.7 `task.wait`

```json
{
  "action": "task.wait",
  "task_id": "ba9d472536ac478299998cdfb039a75c",
  "poll_interval": 2,
  "timeout": 3600,
  "include_files": true
}
```

等待不会创建新任务，只轮询已有任务。超时不会取消后台任务；AI 可以稍后继续调用 `task.get` 或 `task.wait`。

### 6.8 `task.files`

```json
{
  "action": "task.files",
  "task_id": "ba9d472536ac478299998cdfb039a75c"
}
```

每个文件记录包含：

- `kind`：`original`、`vocals` 或 `instrumental`；
- `name`、`size`、`media_type`；
- `url`：浏览器播放地址；
- `download_url`：下载地址。

CLI 会把 API 返回的相对 URL 转换为绝对 URL。

### 6.9 `task.logs`

```json
{
  "action": "task.logs",
  "task_id": "ba9d472536ac478299998cdfb039a75c"
}
```

失败时优先读取日志和任务的 `error` 字段，不要盲目重复提交同一个任务。

### 6.10 `task.download`

```json
{
  "action": "task.download",
  "task_id": "ba9d472536ac478299998cdfb039a75c",
  "output": "/absolute/local/downloads",
  "kinds": ["vocals", "instrumental"]
}
```

`kinds` 可选值为 `vocals`、`instrumental`、`original`；省略时下载人声和伴奏。为避免批量文件同名，结果保存为：

```text
<output>/<item_id>/vocals.wav
<output>/<item_id>/instrumental.wav
```

注意：如果 CLI 运行在 Docker backend 容器里，`output` 也必须是容器可写路径，例如 `/output/agent-downloads`。如果希望下载到 AI 所在主机的任意目录，应使用主机原生 `stemflow` 命令。

## 7. 人类可读子命令示例

### 本机同步分离

```bash
stemflow separate \
  --input ./video.mp4 \
  --output ./results \
  --model mdx \
  --json
```

### 创建后台任务

```bash
stemflow task create \
  --input /input \
  --output /output \
  --model default \
  --json
```

### 创建、等待并返回结果文件

```bash
stemflow task create \
  --input /input \
  --output /output \
  --model default \
  --wait \
  --include-files \
  --timeout 3600 \
  --json
```

### 下载纯人声

```bash
stemflow task download TASK_ID \
  --kind vocals \
  --output ./downloads \
  --json
```

旧版命令仍然兼容：

```bash
audio-separator-service --input ./video.mp4 --output ./results --json
```

旧命令的 `--json` 保持返回原来的结果数组；新 `stemflow separate --json` 返回统一 JSON 信封。新 Agent 应使用 `stemflow`。

## 8. 推荐的 AI 决策流程

```text
1. capabilities
   |
2. health
   |
3. 判断路径属于主机还是容器
   |
4. task.create(wait=true, include_files=true)
   |
   +-- completed -> 返回 vocals / instrumental
   |
   +-- completed_with_errors -> 返回成功文件并解释失败文件
   |
   +-- failed -> task.logs -> 向用户报告具体错误
   |
   +-- timeout -> 保存 task_id，稍后 task.wait，不要重复创建
```

推荐默认策略：

- 服务已经启动时使用 `task.create`；
- 没有服务且 AI 有本机执行权限时使用 `separate`；
- 默认模型使用 `default`，只有用户明确要求速度或指定模型时才改为 `mdx`/`roformer`；
- 一个用户请求只创建一个任务；超时后继续等待同一个 `task_id`；
- 下载前先检查 `kind` 和 `size`；
- 不把任务 `waiting` 或 `running` 当成失败。

## 9. 通用 Tool Schema

如果 Agent 平台支持自定义工具，可以注册一个执行本地命令的 `stemflow_agent` 工具。工具输入 Schema 可使用：

```json
{
  "name": "stemflow_agent",
  "description": "Separate vocals and instrumental locally or manage a StemFlow background task. Pass the arguments as JSON to `stemflow agent` through stdin and return its JSON stdout.",
  "inputSchema": {
    "type": "object",
    "required": ["action"],
    "properties": {
      "action": {
        "type": "string",
        "enum": [
          "capabilities",
          "separate",
          "health",
          "task.list",
          "task.create",
          "task.get",
          "task.wait",
          "task.files",
          "task.logs",
          "task.download"
        ]
      },
      "input": {"type": "string"},
      "output": {"type": "string"},
      "model": {"type": "string", "default": "default"},
      "task_id": {"type": "string"},
      "wait": {"type": "boolean", "default": false},
      "include_files": {"type": "boolean", "default": false},
      "poll_interval": {"type": "number", "minimum": 0.1, "default": 2},
      "timeout": {"type": "number", "minimum": 1, "default": 3600},
      "limit": {"type": "integer", "minimum": 1, "maximum": 200},
      "kinds": {
        "type": "array",
        "items": {"enum": ["vocals", "instrumental", "original"]}
      },
      "api_url": {"type": "string"},
      "public_api_url": {"type": "string"},
      "request_timeout": {"type": "number", "minimum": 1}
    }
  }
}
```

Schema 只能表达公共字段。工具执行器仍应按本文档中每个 action 的必填字段做校验。

## 10. 安全的进程调用示例

不要把用户路径拼接成一整段 shell 命令。把 JSON 通过 stdin 传入进程，并关闭 shell 展开。

### Python

```python
import json
import subprocess


def call_stemflow(request: dict) -> dict:
    completed = subprocess.run(
        ["stemflow", "agent"],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        timeout=request.get("timeout", 3600) + 30,
    )
    response = json.loads(completed.stdout)
    if completed.returncode != 0 or not response["ok"]:
        raise RuntimeError(response["error"])
    return response["data"]
```

### Node.js / TypeScript

```typescript
import { spawn } from "node:child_process";

export function callStemFlow(request: object): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const child = spawn("stemflow", ["agent"], {
      stdio: ["pipe", "pipe", "pipe"],
      shell: false,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.on("close", (code) => {
      try {
        const response = JSON.parse(stdout);
        if (code !== 0 || !response.ok) reject(response.error);
        else resolve(response.data);
      } catch (error) {
        reject(new Error(`Invalid StemFlow response: ${stderr || error}`));
      }
    });
    child.stdin.end(JSON.stringify(request));
  });
}
```

## 11. 退出码与重试策略

| 退出码 | 含义 | AI 行为 |
| ---: | --- | --- |
| `0` | 成功 | 读取 `data` |
| `2` | 参数或 JSON 不合法 | 修正请求，不要原样重试 |
| `3` | 本机输入、依赖或推理失败 | 检查路径、FFmpeg、模型和硬件 |
| `4` | API 连接或 HTTP 错误 | 检查服务地址；网络临时错误可退避重试 |
| `5` | 任务失败或等待超时 | 超时继续等待同一任务；失败读取日志 |
| `10` | CLI 内部异常 | 保存 stderr 和请求，交给维护者处理 |

建议只对连接错误做有限次数指数退避，例如 1、2、4 秒。不要自动重复创建分离任务，因为每次创建都会产生新的推理工作和结果目录。

## 12. 路径、权限和数据边界

- CLI 不会主动读取输入路径以外的媒体文件；目录模式会递归扫描支持格式；
- `/input` 在 Docker 中为只读挂载；
- `/output`、`/data/output` 和任务下载目录需要可写；
- Agent 在提交前应确认用户授权处理对应文件；
- 不要把密码、令牌或其他秘密写入任务路径、模型名或日志；
- 下载 URL 当前是本地服务地址，不应直接暴露到不受信任的公网；
- CLI 不自动删除输入、输出、模型或任务记录。

## 13. 并发和资源行为

服务会把目录任务拆成独立的文件级作业，多个推理槽位从同一个 Redis 队列领取作业。因此一个批量任务中的多段音频，以及多个用户同时创建的不同任务，都可以并行执行。

CPU Docker 默认 `worker_concurrency=2`，GPU 默认1。可通过以下变量调整：

```bash
AUDIO_SERVICE_WORKER_CONCURRENCY=4 docker compose up -d --build
AUDIO_SERVICE_GPU_WORKER_CONCURRENCY=2 \
  docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Windows：

```powershell
.\start-windows.cmd -Workers 4
.\start-windows.cmd -Mode gpu -Workers 1
```

每个槽位拥有独立 `AudioSeparatorService` 和模型实例。并发数增加时，吞吐量会提高，但模型内存或GPU显存也会近似按槽位数增加。GPU不要直接照搬CPU并发数；单卡Roformer建议从1开始。

AI 可以并行提交任务，但不应为了加速而重复创建同一输入任务。一个任务超时后应继续等待原 `task_id`。`task.get` 的 `current_file` 表示最近更新的文件；精确的并行文件状态应读取 `/items` 或 Web任务详情。

模型会在每个槽位中分别缓存。CPU 默认 MDX 适合兼容运行；GPU部署的 `default` 使用高质量 Roformer。第一次使用模型需要下载权重，等待时间可能明显长于后续任务。

## 14. 最小验收清单

集成完成后至少验证：

```bash
stemflow capabilities --json
stemflow health --json
stemflow task list --limit 1 --json
```

然后使用一份短音频执行：

```bash
stemflow task create \
  --input /input/test.wav \
  --output /output/agent-acceptance \
  --model mdx \
  --wait \
  --include-files \
  --json
```

验收条件：退出码为 0、`ok=true`、任务状态为 `completed`，并且 files 中同时存在 `kind=vocals` 和 `kind=instrumental`。
