export type TaskStatus =
  | 'waiting'
  | 'scanning'
  | 'running'
  | 'completed'
  | 'completed_with_errors'
  | 'failed'

export interface Task {
  id: string
  status: TaskStatus
  progress: number
  current_file: string | null
  input_dir: string
  output_dir: string
  model: string
  total_files: number
  completed_files: number
  failed_files: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface TaskItem {
  id: string
  task_id: string
  name: string
  relative_path: string
  media_type: 'audio' | 'video'
  status: 'waiting' | 'extracting_audio' | 'separating' | 'completed' | 'failed'
  progress: number
  duration_seconds: number | null
  error: string | null
}

export interface Artifact {
  id: string
  task_id: string
  item_id: string
  kind: string
  name: string
  media_type: string
  size: number
  url: string
  download_url: string
}

export interface TaskLog {
  created_at: string
  level: string
  message: string
}

export interface AutomationConfig {
  enabled: boolean
  input_dir: string
  output_dir: string
  model: string
  schedule_mode: 'daily' | 'interval'
  daily_time: string
  timezone: string
  interval_minutes: number
  stable_seconds: number
  max_retries: number
  last_scan_at: string | null
  next_scan_at: string | null
  last_error: string | null
  report_path: string
  counts: Record<string, number>
}

export interface AutomationFile {
  id: string
  source_path: string
  relative_path: string
  media_type: 'audio' | 'video'
  size: number
  modified_at: string
  detected_at: string
  started_at: string | null
  finished_at: string | null
  status: string
  progress: number
  attempts: number
  task_id: string | null
  model: string
  duration_seconds: number | null
  vocals_path: string | null
  instrumental_path: string | null
  error: string | null
}

export interface AutomationScanResult {
  scanned_files: number
  queued_files: number
  waiting_for_copy: number
  skipped_files: number
  retry_files: number
  scan_started_at: string
  scan_finished_at: string
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail || '请求失败')
  }
  return response.json() as Promise<T>
}

export const api = {
  listTasks: () => request<Task[]>('/api/tasks'),
  getTask: (id: string) => request<Task>(`/api/tasks/${id}`),
  getItems: (id: string) => request<TaskItem[]>(`/api/tasks/${id}/items`),
  getFiles: (id: string) => request<Artifact[]>(`/api/tasks/${id}/files`),
  getLogs: (id: string) => request<TaskLog[]>(`/api/tasks/${id}/logs`),
  getAutomation: () => request<AutomationConfig>('/api/automation'),
  getAutomationFiles: () => request<AutomationFile[]>('/api/automation/files'),
  updateAutomation: (payload: {
    enabled: boolean
    input_dir: string
    output_dir: string
    model: string
    schedule_mode: 'daily' | 'interval'
    daily_time: string
    timezone: string
    interval_minutes: number
    stable_seconds: number
    max_retries: number
  }) => request<AutomationConfig>('/api/automation', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  scanAutomation: () => request<AutomationScanResult>('/api/automation/scan', { method: 'POST' }),
  retryAutomationFailures: () => request<{ reset_files: number }>('/api/automation/retry-failed', { method: 'POST' }),
  createTask: (payload: { input_dir: string; output_dir: string; model: string }) =>
    request<Task>('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  uploadFolder: async (files: FileList) => {
    const data = new FormData()
    Array.from(files).forEach((file) => {
      data.append('files', file)
      data.append('relative_paths', file.webkitRelativePath || file.name)
    })
    return request<{ upload_id: string; input_dir: string; file_count: number }>('/api/uploads', {
      method: 'POST',
      body: data,
    })
  },
}
