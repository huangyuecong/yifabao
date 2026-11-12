export interface AgentHealth {
  service: string
  status: string
  version: string
  data_boundary?: string
}

export interface ProviderStatus {
  capability?: string
  name: string
  mode: string
  enabled: boolean
  external?: boolean
  reads_audio_content?: boolean
}

export interface AgentJob {
  id: string
  kind: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | string
  progress?: number
  result?: Record<string, unknown>
  error?: string | null
}

export interface ProblemDetail {
  title?: string
  detail?: unknown
  status?: number
  request_id?: string
  trace_id?: string
  errors?: Array<{ loc?: Array<string | number>, msg?: string }>
}
