import {
  Activity,
  AudioLines,
  Check,
  CalendarClock,
  ChevronRight,
  CircleAlert,
  Clock3,
  Download,
  FileAudio,
  FileSpreadsheet,
  FileVideo2,
  FolderOpen,
  Gauge,
  HardDrive,
  History,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  Save,
  Server,
  Sparkles,
  UploadCloud,
  WandSparkles,
  X,
} from 'lucide-react'
import { ChangeEvent, FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom'

import { api, Artifact, AutomationConfig, AutomationFile, Task, TaskItem, TaskLog } from './lib/api'

const STATUS_LABELS: Record<string, string> = {
  waiting: '等待中',
  scanning: '扫描文件',
  running: '处理中',
  extracting_audio: '提取音轨',
  separating: 'AI 分离中',
  completed: '已完成',
  completed_with_errors: '部分完成',
  failed: '失败',
  file_copying: '文件复制中',
  processing: '处理中',
  retrying: '等待重试',
  skipped: '已跳过',
}

const MODEL_OPTIONS = [
  { value: 'default', name: '自动兼容', hint: 'CPU 用 MDX，GPU 用 Roformer' },
  { value: 'mdx', name: '快速分离', hint: 'MDX Inst · 速度优先，可能残留背景' },
  { value: 'demucs', name: 'Demucs v4', hint: '多音轨分离' },
]

function cn(...values: Array<string | false | undefined>) {
  return values.filter(Boolean).join(' ')
}

function formatDuration(seconds: number | null) {
  if (seconds === null) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  return `${Math.floor(seconds / 60)}分 ${Math.round(seconds % 60)}秒`
}

function formatBytes(bytes: number) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`
}

function formatLogTime(value: string) {
  return new Date(value).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function StatusBadge({ status }: { status: string }) {
  const complete = status === 'completed'
  const failed = status === 'failed'
  const active = ['running', 'extracting_audio', 'separating', 'scanning'].includes(status)
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium',
        complete && 'border-emerald-400/20 bg-emerald-400/10 text-emerald-300',
        failed && 'border-red-400/20 bg-red-400/10 text-red-300',
        active && 'border-violet-400/20 bg-violet-400/10 text-violet-300',
        !complete && !failed && !active && 'border-white/10 bg-white/[0.04] text-slate-400',
      )}
    >
      {active ? <LoaderCircle size={12} className="animate-spin" /> : complete ? <Check size={12} /> : failed ? <X size={12} /> : <Clock3 size={12} />}
      {STATUS_LABELS[status] || status}
    </span>
  )
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
      <div className="h-full rounded-full bg-gradient-to-r from-violet-500 to-cyan-400 transition-all duration-500" style={{ width: `${Math.max(2, value)}%` }} />
    </div>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-[#080a0f] text-slate-100">
      <div className="pointer-events-none fixed inset-0 bg-[radial-gradient(circle_at_20%_-10%,rgba(124,58,237,.14),transparent_35%),radial-gradient(circle_at_90%_10%,rgba(34,211,238,.08),transparent_30%)]" />
      <header className="sticky top-0 z-30 border-b border-white/[0.06] bg-[#080a0f]/85 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-[1480px] items-center justify-between px-5 lg:px-8">
          <Link to="/" className="flex items-center gap-3">
            <span className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-violet-500 to-cyan-400 text-white shadow-lg shadow-violet-950/40">
              <AudioLines size={20} />
            </span>
            <span>
              <span className="block text-sm font-semibold tracking-wide">StemFlow</span>
              <span className="block text-[10px] uppercase tracking-[.22em] text-slate-500">Local AI separator</span>
            </span>
          </Link>
          <nav className="flex items-center gap-1 rounded-xl border border-white/[0.07] bg-white/[0.025] p-1">
            <NavLink to="/" end className={({ isActive }) => cn('rounded-lg px-3 py-2 text-sm transition', isActive ? 'bg-white/[0.08] text-white' : 'text-slate-400 hover:text-white')}>新建任务</NavLink>
            <NavLink to="/automation" className={({ isActive }) => cn('rounded-lg px-3 py-2 text-sm transition', isActive ? 'bg-white/[0.08] text-white' : 'text-slate-400 hover:text-white')}>自动处理</NavLink>
            <NavLink to="/tasks" className={({ isActive }) => cn('rounded-lg px-3 py-2 text-sm transition', isActive ? 'bg-white/[0.08] text-white' : 'text-slate-400 hover:text-white')}>任务记录</NavLink>
          </nav>
          <div className="hidden items-center gap-2 text-xs text-emerald-300 sm:flex">
            <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_12px_rgba(52,211,153,.8)]" />
            本地服务
          </div>
        </div>
      </header>
      <main className="relative mx-auto max-w-[1480px] px-5 py-8 lg:px-8 lg:py-12">{children}</main>
    </div>
  )
}

function NewTaskPage() {
  const navigate = useNavigate()
  const [mode, setMode] = useState<'path' | 'upload'>('path')
  const [inputDir, setInputDir] = useState('/input')
  const [outputDir, setOutputDir] = useState('/output')
  const [model, setModel] = useState('default')
  const [files, setFiles] = useState<FileList | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      let resolvedInput = inputDir
      if (mode === 'upload') {
        if (!files?.length) throw new Error('请先选择一个包含音视频的文件夹')
        const upload = await api.uploadFolder(files)
        resolvedInput = upload.input_dir
      }
      const task = await api.createTask({ input_dir: resolvedInput, output_dir: outputDir, model })
      navigate(`/tasks/${task.id}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建任务失败')
    } finally {
      setBusy(false)
    }
  }

  function chooseFolder(event: ChangeEvent<HTMLInputElement>) {
    setFiles(event.target.files)
  }

  return (
    <div className="grid gap-8 xl:grid-cols-[minmax(0,1.2fr)_420px]">
      <section>
        <div className="mb-8 max-w-3xl">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-violet-400/20 bg-violet-400/10 px-3 py-1.5 text-xs font-medium text-violet-300">
            <Sparkles size={13} /> AI 音频处理工作台
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-white md:text-5xl">从混合音轨中，<br /><span className="text-gradient">分离人声与伴奏。</span></h1>
          <p className="mt-5 max-w-2xl text-sm leading-7 text-slate-400 md:text-base">批量处理视频与音频。视频会自动提取音轨，再由本地模型分离出人声和伴奏。</p>
        </div>

        <form onSubmit={submit} className="overflow-hidden rounded-2xl border border-white/[0.08] bg-[#0d1017]/90 shadow-2xl shadow-black/20">
          <div className="border-b border-white/[0.06] px-6 py-5">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="font-medium text-white">输入来源</h2>
                <p className="mt-1 text-xs text-slate-500">使用服务器路径，或从浏览器上传整个文件夹</p>
              </div>
              <div className="flex rounded-lg bg-black/20 p-1 text-xs">
                <button type="button" onClick={() => setMode('path')} className={cn('rounded-md px-3 py-1.5 transition', mode === 'path' ? 'bg-white/10 text-white' : 'text-slate-500')}>本地路径</button>
                <button type="button" onClick={() => setMode('upload')} className={cn('rounded-md px-3 py-1.5 transition', mode === 'upload' ? 'bg-white/10 text-white' : 'text-slate-500')}>上传文件夹</button>
              </div>
            </div>

            {mode === 'path' ? (
              <label className="mt-5 block">
                <span className="mb-2 block text-xs font-medium text-slate-400">Input Folder</span>
                <div className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-black/20 px-4 focus-within:border-violet-400/40">
                  <FolderOpen size={17} className="text-violet-400" />
                  <input value={inputDir} onChange={(event) => setInputDir(event.target.value)} className="h-12 w-full bg-transparent font-mono text-sm text-slate-200 outline-none" aria-label="输入目录" />
                </div>
                <span className="mt-2 block text-[11px] text-slate-600">Docker 会把 Windows、macOS 或 Linux 的来源目录映射为 /input</span>
              </label>
            ) : (
              <label className="mt-5 flex min-h-36 cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed border-white/15 bg-black/15 px-6 text-center transition hover:border-violet-400/40 hover:bg-violet-400/[0.04]">
                <UploadCloud size={26} className="mb-3 text-violet-400" />
                <span className="text-sm font-medium text-slate-200">选择一个音视频文件夹</span>
                <span className="mt-1 text-xs text-slate-500">MP4、MOV、MKV、AVI、MP3、WAV、FLAC、M4A、OGG</span>
                {files?.length ? <span className="mt-3 rounded-full bg-emerald-400/10 px-3 py-1 text-xs text-emerald-300">已选择 {files.length} 个文件</span> : null}
                <input
                  type="file"
                  multiple
                  className="sr-only"
                  onChange={chooseFolder}
                  {...({ webkitdirectory: '', directory: '' } as React.InputHTMLAttributes<HTMLInputElement>)}
                />
              </label>
            )}
          </div>

          <div className="grid gap-6 border-b border-white/[0.06] px-6 py-5 md:grid-cols-2">
            <label>
              <span className="mb-2 block text-xs font-medium text-slate-400">Output Folder</span>
              <div className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-black/20 px-4 focus-within:border-cyan-400/40">
                <HardDrive size={17} className="text-cyan-400" />
                <input value={outputDir} onChange={(event) => setOutputDir(event.target.value)} className="h-12 w-full bg-transparent font-mono text-sm text-slate-200 outline-none" aria-label="输出目录" />
              </div>
              <span className="mt-2 block text-[11px] text-slate-600">处理结果写入宿主机映射的 /output 目录</span>
            </label>
            <label>
              <span className="mb-2 block text-xs font-medium text-slate-400">分离模型</span>
              <select value={model} onChange={(event) => setModel(event.target.value)} className="h-12 w-full rounded-xl border border-white/[0.08] bg-[#0a0d13] px-4 text-sm text-slate-200 outline-none focus:border-violet-400/40">
                {MODEL_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.name} · {option.hint}</option>)}
              </select>
            </label>
          </div>

          <div className="flex flex-col gap-4 px-6 py-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2 text-xs text-slate-500"><Server size={14} /> 模型首次使用时会自动下载并缓存</div>
            <button disabled={busy} className="inline-flex h-11 items-center justify-center gap-2 rounded-xl bg-white px-5 text-sm font-semibold text-slate-950 transition hover:bg-violet-100 disabled:cursor-wait disabled:opacity-60">
              {busy ? <LoaderCircle size={17} className="animate-spin" /> : <WandSparkles size={17} />}
              {busy ? (mode === 'upload' ? '上传并创建…' : '正在创建…') : '开始处理'}
            </button>
          </div>
          {error ? <div className="flex items-center gap-2 border-t border-red-400/10 bg-red-400/[0.06] px-6 py-3 text-sm text-red-300"><CircleAlert size={16} />{error}</div> : null}
        </form>
      </section>

      <aside className="space-y-5 xl:pt-24">
        <div className="rounded-2xl border border-white/[0.08] bg-white/[0.025] p-5">
          <div className="mb-5 flex items-center justify-between"><h3 className="text-sm font-medium">处理流程</h3><Activity size={16} className="text-violet-400" /></div>
          <div className="space-y-1">
            {[
              ['01', '扫描媒体', '识别视频与音频格式'],
              ['02', '提取音轨', '视频使用 FFmpeg 转为 WAV'],
              ['03', 'AI 人声分离', '本地 CPU / CUDA 模型推理'],
              ['04', '整理结果', '生成 vocals 与 instrumental'],
            ].map(([number, title, copy], index) => (
              <div key={number} className="relative flex gap-4 py-3">
                {index < 3 ? <span className="absolute left-[13px] top-10 h-7 w-px bg-white/[0.08]" /> : null}
                <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-white/10 bg-black/20 font-mono text-[10px] text-violet-300">{number}</span>
                <div><div className="text-sm text-slate-200">{title}</div><div className="mt-0.5 text-xs text-slate-500">{copy}</div></div>
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-2xl border border-cyan-400/10 bg-gradient-to-br from-cyan-400/[0.06] to-violet-400/[0.04] p-5">
          <Gauge size={20} className="text-cyan-300" />
          <div className="mt-4 text-sm font-medium">硬件自动适配</div>
          <p className="mt-2 text-xs leading-6 text-slate-400">CPU 可直接运行；CUDA 镜像会自动使用 NVIDIA GPU。模型和任务状态均持久保存。</p>
        </div>
      </aside>
    </div>
  )
}

function useTasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const refresh = useCallback(async () => {
    try {
      setTasks(await api.listTasks())
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取任务')
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => {
    void refresh()
    const timer = window.setInterval(refresh, 2500)
    return () => window.clearInterval(timer)
  }, [refresh])
  return { tasks, loading, error }
}

function TasksPage() {
  const { tasks, loading, error } = useTasks()
  return (
    <section>
      <div className="mb-7 flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div><p className="mb-2 text-xs font-medium uppercase tracking-[.2em] text-violet-400">Processing queue</p><h1 className="text-3xl font-semibold">任务记录</h1><p className="mt-2 text-sm text-slate-500">所有批处理任务与实时进度</p></div>
        <Link to="/" className="inline-flex h-10 items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/[0.04] px-4 text-sm text-slate-200 hover:bg-white/[0.08]"><Plus size={16} />新建任务</Link>
      </div>
      <div className="overflow-hidden rounded-2xl border border-white/[0.08] bg-[#0d1017]/90">
        {loading ? <div className="grid min-h-64 place-items-center text-slate-500"><LoaderCircle className="animate-spin" /></div> : error ? <div className="p-6 text-red-300">{error}</div> : tasks.length === 0 ? (
          <div className="grid min-h-72 place-items-center text-center"><div><History className="mx-auto mb-3 text-slate-600" /><div className="text-sm text-slate-300">还没有任务</div><Link to="/" className="mt-3 inline-block text-xs text-violet-300">开始第一次分离 →</Link></div></div>
        ) : (
          <div className="divide-y divide-white/[0.06]">
            {tasks.map((task) => (
              <Link key={task.id} to={`/tasks/${task.id}`} className="grid gap-4 p-5 transition hover:bg-white/[0.025] md:grid-cols-[minmax(220px,1fr)_140px_180px_24px] md:items-center">
                <div className="min-w-0"><div className="flex items-center gap-2"><span className="truncate text-sm font-medium text-slate-200">{task.current_file || task.input_dir.split('/').pop()}</span><StatusBadge status={task.status} /></div><div className="mt-2 truncate font-mono text-[11px] text-slate-600">{task.id}</div></div>
                <div><div className="mb-2 flex justify-between text-xs text-slate-500"><span>{task.completed_files}/{task.total_files || '—'} 文件</span><span>{Math.round(task.progress)}%</span></div><ProgressBar value={task.progress} /></div>
                <div className="text-xs text-slate-500"><div>{new Date(task.created_at).toLocaleString('zh-CN')}</div><div className="mt-1 truncate text-slate-600">{task.model}</div></div>
                <ChevronRight size={17} className="text-slate-600" />
              </Link>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function TaskDetailPage() {
  const { id = '' } = useParams()
  const [task, setTask] = useState<Task | null>(null)
  const [items, setItems] = useState<TaskItem[]>([])
  const [files, setFiles] = useState<Artifact[]>([])
  const [logs, setLogs] = useState<TaskLog[]>([])
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const [nextTask, nextItems, nextFiles, nextLogs] = await Promise.all([
        api.getTask(id),
        api.getItems(id),
        api.getFiles(id),
        api.getLogs(id),
      ])
      setTask(nextTask)
      setItems(nextItems)
      setFiles(nextFiles)
      setLogs(nextLogs)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取任务')
    }
  }, [id])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(refresh, 2000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const artifactsByItem = useMemo(
    () => files.reduce<Record<string, Artifact[]>>((groups, file) => {
      groups[file.item_id] = [...(groups[file.item_id] || []), file]
      return groups
    }, {}),
    [files],
  )
  const latestLogs = useMemo(() => [...logs].reverse(), [logs])
  if (error) return <div className="rounded-xl border border-red-400/20 bg-red-400/5 p-5 text-red-300">{error}</div>
  if (!task) return <div className="grid min-h-80 place-items-center"><LoaderCircle className="animate-spin text-violet-400" /></div>

  return (
    <section>
      <Link to="/tasks" className="mb-3 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-300">← 返回任务记录</Link>
      <div className="mb-4 rounded-xl border border-white/[0.07] bg-white/[0.025] px-4 py-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <h1 className="shrink-0 text-xl font-semibold">任务详情</h1>
            <StatusBadge status={task.status} />
            <span className="hidden truncate font-mono text-[11px] text-slate-600 sm:block">{task.id}</span>
          </div>
          <div className="flex min-w-0 items-center gap-3 lg:w-[46%]">
            <span className="shrink-0 text-xs text-slate-500">总体进度</span>
            <ProgressBar value={task.progress} />
            <span className="w-10 shrink-0 text-right font-mono text-xs text-slate-200">{Math.round(task.progress)}%</span>
          </div>
        </div>
        <div className="mt-2 flex min-w-0 items-center justify-between gap-4 text-[11px] text-slate-600">
          <span className="truncate font-mono" title={task.input_dir}>{task.input_dir}</span>
          <span className="shrink-0">{task.current_file || `${task.completed_files}/${task.total_files} 个文件已完成`}</span>
        </div>
      </div>

      {task.error ? <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-400/15 bg-red-400/[0.06] px-3 py-2 text-xs text-red-300"><CircleAlert size={15} />{task.error}</div> : null}

      <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-[#0d1017]/90">
        <div className="hidden grid-cols-[minmax(240px,1fr)_80px_135px_85px_16px] gap-3 border-b border-white/[0.06] px-4 py-2 text-[10px] font-semibold uppercase tracking-widest text-slate-600 md:grid">
          <span>文件</span><span>类型</span><span>状态</span><span>耗时</span><span />
        </div>
        <div className="divide-y divide-white/[0.06]">
          {items.length === 0 ? <div className="px-4 py-6 text-center text-sm text-slate-500">正在扫描输入目录…</div> : items.map((item) => {
            const artifacts = artifactsByItem[item.id] || []
            const vocals = artifacts.find((artifact) => artifact.kind === 'vocals')
            const instrumental = artifacts.find((artifact) => artifact.kind === 'instrumental')
            const original = artifacts.find((artifact) => artifact.kind === 'original')
            return (
              <div key={item.id} className="px-4 py-3">
                <div className="grid gap-3 md:grid-cols-[minmax(240px,1fr)_80px_135px_85px_16px] md:items-center">
                  <div className="flex min-w-0 items-center gap-2.5"><span className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-white/[0.04] text-slate-400">{item.media_type === 'video' ? <FileVideo2 size={14} /> : <FileAudio size={14} />}</span><div className="min-w-0"><div className="truncate text-sm text-slate-200">{item.name}</div><div className="truncate text-[10px] text-slate-600">{item.relative_path}</div></div></div>
                  <span className="text-xs text-slate-500">{item.media_type === 'video' ? '视频' : '音频'}</span>
                  <div><StatusBadge status={item.status} />{item.status !== 'completed' && item.status !== 'failed' ? <div className="mt-1.5 w-24"><ProgressBar value={item.progress} /></div> : null}</div>
                  <span className="text-xs text-slate-500">{formatDuration(item.duration_seconds)}</span>
                  <span className={cn('h-2 w-2 rounded-full', item.status === 'completed' ? 'bg-emerald-400' : item.status === 'failed' ? 'bg-red-400' : 'bg-violet-400 animate-pulse')} />
                </div>
                {item.error ? <div className="mt-2 rounded-md bg-red-400/[0.05] px-3 py-1.5 text-xs text-red-300">{item.error}</div> : null}
                {item.status === 'completed' ? (
                  <div className="mt-3 grid gap-2 border-t border-white/[0.06] pt-3 lg:grid-cols-3">
                    {[
                      ['Original', original, '原始文件'],
                      ['Vocals', vocals, '人声轨（可能有残留）'],
                      ['Instrumental', instrumental, '伴奏'],
                    ].map(([label, artifact, subtitle]) => {
                      const file = artifact as Artifact | undefined
                      return (
                        <div key={label as string} className="rounded-lg border border-white/[0.07] bg-black/15 p-3">
                          <div className="mb-2 flex items-center justify-between"><div className="min-w-0"><span className="text-xs font-medium text-slate-200">{label as string}</span><span className="ml-2 text-[10px] text-slate-600">{subtitle as string}{file ? ` · ${formatBytes(file.size)}` : ''}</span></div>{file ? <a href={file.download_url} aria-label={`下载 ${label}`} className="shrink-0 rounded-md p-1.5 text-slate-500 transition hover:bg-white/[0.06] hover:text-white"><Download size={14} /></a> : null}</div>
                          {file?.media_type === 'video' ? <video controls preload="metadata" className="h-9 w-full" src={file.url} /> : file ? <audio controls preload="metadata" className="h-9 w-full opacity-80" src={file.url} /> : <div className="flex h-9 items-center gap-2 text-xs text-slate-600"><Play size={13} />该模型未生成此音轨</div>}
                        </div>
                      )
                    })}
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
      </div>

      <div className="mt-4 overflow-hidden rounded-xl border border-white/[0.08] bg-[#0d1017]/90">
        <div className="flex h-9 items-center justify-between border-b border-white/[0.06] px-4">
          <span className="text-xs font-medium text-slate-300">运行日志</span>
          <span className="font-mono text-[10px] text-slate-600">{logs.length} 条 · 最新优先</span>
        </div>
        <div className="max-h-64 overflow-auto font-mono">
          {latestLogs.length === 0 ? (
            <div className="px-4 py-2 text-[11px] text-slate-600">暂无日志</div>
          ) : latestLogs.map((log, index) => (
            <div
              key={`${log.created_at}-${index}`}
              className="grid min-w-[560px] grid-cols-[68px_52px_minmax(0,1fr)] items-center gap-3 border-b border-white/[0.04] px-4 py-1.5 text-[11px] last:border-b-0"
            >
              <span className="text-slate-600">{formatLogTime(log.created_at)}</span>
              <span className={cn(
                'uppercase',
                log.level === 'error' ? 'text-red-300' : log.level === 'warning' ? 'text-amber-300' : 'text-cyan-400/70',
              )}>{log.level}</span>
              <span className="truncate text-slate-400" title={log.message}>{log.message}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

type AutomationForm = Pick<
  AutomationConfig,
  'enabled' | 'input_dir' | 'output_dir' | 'model' | 'schedule_mode' | 'daily_time' | 'timezone' | 'interval_minutes' | 'stable_seconds' | 'max_retries'
>

const EMPTY_AUTOMATION_FORM: AutomationForm = {
  enabled: false,
  input_dir: '/input',
  output_dir: '/output',
  model: 'default',
  schedule_mode: 'daily',
  daily_time: '00:00',
  timezone: 'Asia/Shanghai',
  interval_minutes: 5,
  stable_seconds: 60,
  max_retries: 3,
}

function AutomationPage() {
  const [config, setConfig] = useState<AutomationConfig | null>(null)
  const [records, setRecords] = useState<AutomationFile[]>([])
  const [form, setForm] = useState<AutomationForm>(EMPTY_AUTOMATION_FORM)
  const [initialized, setInitialized] = useState(false)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const refresh = useCallback(async (initializeForm = false) => {
    try {
      const [nextConfig, nextRecords] = await Promise.all([
        api.getAutomation(),
        api.getAutomationFiles(),
      ])
      setConfig(nextConfig)
      setRecords(nextRecords)
      if (initializeForm) {
        setForm({
          enabled: nextConfig.enabled,
          input_dir: nextConfig.input_dir,
          output_dir: nextConfig.output_dir,
          model: nextConfig.model,
          schedule_mode: nextConfig.schedule_mode,
          daily_time: nextConfig.daily_time,
          timezone: nextConfig.timezone,
          interval_minutes: nextConfig.interval_minutes,
          stable_seconds: nextConfig.stable_seconds,
          max_retries: nextConfig.max_retries,
        })
        setInitialized(true)
      }
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取自动处理配置')
    }
  }, [])

  useEffect(() => {
    void refresh(true)
    const timer = window.setInterval(() => void refresh(false), 5000)
    return () => window.clearInterval(timer)
  }, [refresh])

  async function saveAutomation(event: FormEvent) {
    event.preventDefault()
    setBusy('save')
    setMessage('')
    try {
      const next = await api.updateAutomation(form)
      setConfig(next)
      setMessage(next.enabled ? '自动处理已保存并启用' : '配置已保存，自动处理当前关闭')
      await refresh(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
    } finally {
      setBusy('')
    }
  }

  async function scanNow() {
    setBusy('scan')
    setMessage('')
    try {
      const result = await api.scanAutomation()
      setMessage(`扫描 ${result.scanned_files} 个文件，新入队 ${result.queued_files} 个，重试 ${result.retry_files} 个`)
      await refresh(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '扫描失败')
    } finally {
      setBusy('')
    }
  }

  async function retryFailures() {
    setBusy('retry')
    setMessage('')
    try {
      const result = await api.retryAutomationFailures()
      setMessage(result.reset_files ? `已重新提交 ${result.reset_files} 个失败文件` : '当前没有失败文件')
      await refresh(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '重试失败')
    } finally {
      setBusy('')
    }
  }

  if (!initialized && !error) {
    return <div className="grid min-h-80 place-items-center"><LoaderCircle className="animate-spin text-violet-400" /></div>
  }

  const counts = config?.counts || {}
  const waitingCount = (counts.waiting || 0) + (counts.file_copying || 0)
  const processingCount = (counts.processing || 0) + (counts.retrying || 0)

  return (
    <section>
      <div className="mb-6 flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[.2em] text-violet-300"><CalendarClock size={14} />Unattended workflow</div>
          <h1 className="text-3xl font-semibold">自动处理</h1>
          <p className="mt-2 text-sm text-slate-500">后台按计划扫描固定文件夹；关闭此页面不会影响执行。</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={scanNow} disabled={!!busy} className="inline-flex items-center gap-2 rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-xs text-slate-300 transition hover:bg-white/[0.07] disabled:opacity-50">
            <RefreshCw size={14} className={busy === 'scan' ? 'animate-spin' : ''} />立即扫描
          </button>
          <button type="button" onClick={retryFailures} disabled={!!busy} className="inline-flex items-center gap-2 rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-xs text-slate-300 transition hover:bg-white/[0.07] disabled:opacity-50">
            <History size={14} />重试失败
          </button>
          <a href="/api/automation/report" className="inline-flex items-center gap-2 rounded-lg bg-emerald-400/10 px-3 py-2 text-xs text-emerald-300 transition hover:bg-emerald-400/15">
            <FileSpreadsheet size={14} />下载 Excel
          </a>
        </div>
      </div>

      {error ? <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-400/15 bg-red-400/[0.06] px-3 py-2 text-xs text-red-300"><CircleAlert size={15} />{error}</div> : null}
      {message ? <div className="mb-4 flex items-center gap-2 rounded-lg border border-emerald-400/15 bg-emerald-400/[0.06] px-3 py-2 text-xs text-emerald-300"><Check size={15} />{message}</div> : null}

      <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {[
          ['状态', config?.enabled ? '运行中' : '已关闭', config?.enabled ? 'text-emerald-300' : 'text-slate-500'],
          ['等待', waitingCount, 'text-amber-300'],
          ['处理中', processingCount, 'text-violet-300'],
          ['已完成', counts.completed || 0, 'text-emerald-300'],
          ['失败', counts.failed || 0, 'text-red-300'],
        ].map(([label, value, color]) => (
          <div key={label as string} className="rounded-xl border border-white/[0.07] bg-white/[0.025] px-4 py-3">
            <div className="text-[10px] uppercase tracking-widest text-slate-600">{label}</div>
            <div className={cn('mt-1 text-xl font-semibold', color as string)}>{value}</div>
          </div>
        ))}
      </div>

      <form onSubmit={saveAutomation} className="mb-4 rounded-xl border border-white/[0.08] bg-[#0d1017]/90 p-4">
        <div className="mb-4 flex items-center justify-between gap-4">
          <div>
            <h2 className="text-sm font-medium text-slate-200">监控设置</h2>
            <p className="mt-1 text-[11px] text-slate-600">
              最近扫描：{config?.last_scan_at ? new Date(config.last_scan_at).toLocaleString('zh-CN') : '尚未扫描'}
              {config?.next_scan_at ? ` · 下次执行：${new Date(config.next_scan_at).toLocaleString('zh-CN')}` : ''}
            </p>
          </div>
          <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-300">
            <input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} className="h-4 w-4 accent-violet-500" />
            启用后台计划
          </label>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          <label className="text-xs text-slate-500">监控目录
            <input value={form.input_dir} onChange={(event) => setForm({ ...form, input_dir: event.target.value })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-black/20 px-3 py-2.5 font-mono text-xs text-slate-200 outline-none focus:border-violet-400/50" />
          </label>
          <label className="text-xs text-slate-500">输出目录
            <input value={form.output_dir} onChange={(event) => setForm({ ...form, output_dir: event.target.value })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-black/20 px-3 py-2.5 font-mono text-xs text-slate-200 outline-none focus:border-violet-400/50" />
          </label>
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <label className="text-xs text-slate-500">执行方式
            <select value={form.schedule_mode} onChange={(event) => setForm({ ...form, schedule_mode: event.target.value as AutomationForm['schedule_mode'] })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-[#11151e] px-3 py-2.5 text-xs text-slate-200 outline-none">
              <option value="daily">每天固定时间</option>
              <option value="interval">按固定间隔</option>
            </select>
          </label>
          {form.schedule_mode === 'daily' ? (
            <>
              <label className="text-xs text-slate-500">每天执行时间
                <input type="time" value={form.daily_time} onChange={(event) => setForm({ ...form, daily_time: event.target.value })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-black/20 px-3 py-2.5 text-xs text-slate-200 outline-none" />
              </label>
              <label className="text-xs text-slate-500">计划时区
                <select value={form.timezone} onChange={(event) => setForm({ ...form, timezone: event.target.value })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-[#11151e] px-3 py-2.5 text-xs text-slate-200 outline-none">
                  <option value="Asia/Shanghai">北京时间</option>
                  <option value="UTC">UTC</option>
                  <option value="Asia/Tokyo">东京时间</option>
                  <option value="America/Los_Angeles">洛杉矶时间</option>
                </select>
              </label>
            </>
          ) : (
            <label className="text-xs text-slate-500">扫描间隔（分钟）
              <input type="number" min={1} max={1440} value={form.interval_minutes} onChange={(event) => setForm({ ...form, interval_minutes: Number(event.target.value) })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-black/20 px-3 py-2.5 text-xs text-slate-200 outline-none" />
            </label>
          )}
          <label className="text-xs text-slate-500">文件稳定等待（秒）
            <input type="number" min={0} max={86400} value={form.stable_seconds} onChange={(event) => setForm({ ...form, stable_seconds: Number(event.target.value) })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-black/20 px-3 py-2.5 text-xs text-slate-200 outline-none" />
          </label>
          <label className="text-xs text-slate-500">失败重试次数
            <input type="number" min={0} max={20} value={form.max_retries} onChange={(event) => setForm({ ...form, max_retries: Number(event.target.value) })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-black/20 px-3 py-2.5 text-xs text-slate-200 outline-none" />
          </label>
          <label className="text-xs text-slate-500">分离模型
            <select value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-[#11151e] px-3 py-2.5 text-xs text-slate-200 outline-none">
              {MODEL_OPTIONS.map((model) => <option key={model.value} value={model.value}>{model.name}</option>)}
            </select>
          </label>
        </div>
        <div className="mt-3 rounded-lg bg-cyan-400/[0.04] px-3 py-2 text-[11px] text-cyan-200/60">
          计划由后端服务执行。浏览器和本页面关闭后，任务仍会在设定时间启动。
        </div>
        <div className="mt-4 flex items-center justify-between gap-3 border-t border-white/[0.06] pt-3">
          <span className="truncate font-mono text-[10px] text-slate-600">{config?.report_path}</span>
          <button type="submit" disabled={!!busy} className="inline-flex shrink-0 items-center gap-2 rounded-lg bg-violet-500 px-4 py-2 text-xs font-medium text-white transition hover:bg-violet-400 disabled:opacity-50">
            {busy === 'save' ? <LoaderCircle size={14} className="animate-spin" /> : <Save size={14} />}保存配置
          </button>
        </div>
      </form>

      {config?.last_error ? <div className="mb-4 rounded-lg border border-red-400/15 bg-red-400/[0.05] px-3 py-2 text-xs text-red-300">最近扫描错误：{config.last_error}</div> : null}

      <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-[#0d1017]/90">
        <div className="flex h-10 items-center justify-between border-b border-white/[0.06] px-4">
          <h2 className="text-xs font-medium text-slate-300">文件处理记录</h2>
          <span className="text-[10px] text-slate-600">{records.length} 条</span>
        </div>
        <div className="overflow-x-auto">
          <div className="min-w-[900px]">
            <div className="grid grid-cols-[minmax(250px,1fr)_100px_120px_90px_140px_32px] gap-3 border-b border-white/[0.05] px-4 py-2 text-[10px] uppercase tracking-widest text-slate-600">
              <span>文件</span><span>类型</span><span>状态</span><span>尝试</span><span>完成时间</span><span />
            </div>
            <div className="max-h-[420px] divide-y divide-white/[0.05] overflow-y-auto">
              {records.length === 0 ? <div className="px-4 py-8 text-center text-xs text-slate-600">尚未发现文件，保存配置后可立即扫描。</div> : records.map((record) => (
                <div key={record.id} className="grid grid-cols-[minmax(250px,1fr)_100px_120px_90px_140px_32px] items-center gap-3 px-4 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-xs text-slate-200">{record.relative_path}</div>
                    {record.error ? <div className="mt-0.5 truncate text-[10px] text-red-300" title={record.error}>{record.error}</div> : <div className="mt-0.5 truncate font-mono text-[10px] text-slate-700">{record.vocals_path || record.source_path}</div>}
                  </div>
                  <span className="text-xs text-slate-500">{record.media_type === 'video' ? '视频' : '音频'}</span>
                  <StatusBadge status={record.status} />
                  <span className="text-xs text-slate-500">{record.attempts}</span>
                  <span className="text-[11px] text-slate-600">{record.finished_at ? new Date(record.finished_at).toLocaleString('zh-CN') : '—'}</span>
                  {record.task_id ? <Link to={`/tasks/${record.task_id}`} aria-label={`查看 ${record.relative_path}`} className="rounded-md p-1 text-slate-600 hover:bg-white/[0.05] hover:text-white"><ChevronRight size={15} /></Link> : <span />}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<NewTaskPage />} />
        <Route path="/automation" element={<AutomationPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/tasks/:id" element={<TaskDetailPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  )
}
