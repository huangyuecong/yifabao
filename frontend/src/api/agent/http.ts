import type { AxiosInstance, AxiosRequestConfig } from 'axios'
import type { ProblemDetail } from './types'
import axios, { AxiosHeaders } from 'axios'
import { isEntrySso, reportEntryUnauthorized } from '@/auth/entrySession'
import { isStandaloneMode } from '@/auth/workspaceMode'
import { agentRequestPath } from './apiUrl'

const isAgentDevMode = import.meta.env.VITE_AGENT_DEV_MODE === 'true'

function shellAccessToken(): string | undefined {
  const key = Object.keys(localStorage).find(item => item === 'ACCESS_TOKEN' || item.endsWith('_ACCESS_TOKEN'))
  if (!key) {
    return undefined
  }
  const raw = localStorage.getItem(key)
  if (!raw) {
    return undefined
  }
  try {
    const value = JSON.parse(raw) as string | { val?: string }
    return typeof value === 'string' ? value : value.val
  }
  catch {
    return raw
  }
}

function identityHeaders(): Record<string, string> {
  if (isStandaloneMode || isEntrySso) {
    return {}
  }
  if (!isAgentDevMode) {
    const token = shellAccessToken()
    return token ? { Authorization: `Bearer ${token}` } : {}
  }

  return {
    'X-User-Id': import.meta.env.VITE_AGENT_DEV_USER_ID || 'dev-user',
    'X-User-Name': 'dev-user',
    'X-Department': import.meta.env.VITE_AGENT_DEV_DEPARTMENT || 'digital-center',
    'X-Roles': import.meta.env.VITE_AGENT_DEV_ROLES || 'user,reviewer,admin',
  }
}

async function blobProblemDetail(value: Blob): Promise<ProblemDetail | null> {
  if (!value.type.includes('json')) {
    return null
  }
  try {
    return JSON.parse(await value.text()) as ProblemDetail
  }
  catch {
    return null
  }
}

function normalizeProblem(value: unknown): ProblemDetail | null {
  if (!value || typeof value !== 'object') {
    return null
  }
  return value as ProblemDetail
}

const problemReasonLabels: Record<string, string> = {
  quality_not_completed: '本地质检尚未完成',
  quality_not_passed: '未通过核心质量检查',
  audio_decode_not_evaluated: '音频可解码性尚未确认',
  valid_speech_not_evaluated: '有效语音尚未检测',
  diarization_vad_not_evaluated: '本地有效语音检测尚未完成',
  no_effective_speech: '未检测到有效语音',
  effective_speech_below_15_seconds: '有效语音未达到当前录入策略要求',
  effective_speech_below_minimum: '有效语音未达到当前录入策略要求',
  effective_speech_below_threshold: '有效语音未达到当前录入策略要求',
  total_clean_speech_below_threshold: '累计有效语音未达到当前录入策略要求',
  speech_ratio_not_evaluated: '语音占比尚未检测',
  speech_ratio_below_threshold: '语音占比未达到当前录入策略要求',
  speaker_count_not_evaluated: '说话人数尚未检测',
  speaker_count_not_one: '说话人数估计异常，需人工复核',
  speaker_count_soft_prior_disagreement: '不同人数估计结果存在分歧，需人工复核',
  multiple_speakers_strong_evidence: '确认存在多人讲话，请重新录制',
  dominant_speaker_ratio_not_evaluated: '主说话人占比尚未检测',
  dominant_speaker_ratio_below_0_9: '主说话人占比估计异常，需人工复核',
  dominant_speaker_ratio_below_threshold: '主说话人占比估计异常，需人工复核',
  clipping_ratio_not_evaluated: '削波比例尚未检测',
  clipping_ratio_above_threshold: '削波比例超过当前录入策略上限',
  sample_duration_not_evaluated: '录音时长尚未检测',
  sample_duration_above_30_seconds: '单段录音超过当前策略上限',
  sample_duration_above_maximum: '单段录音超过当前策略上限',
}

function readableProblemValue(value: unknown): string | undefined {
  if (typeof value === 'string') {
    const normalized = value.trim()
    return normalized ? (problemReasonLabels[normalized] || normalized) : undefined
  }
  if (Array.isArray(value)) {
    const items = value.map(readableProblemValue).filter(Boolean)
    return items.length ? items.join('；') : undefined
  }
  if (!value || typeof value !== 'object') {
    return undefined
  }
  const item = value as Record<string, unknown>
  const primary = readableProblemValue(item.message)
    || readableProblemValue(item.detail)
    || readableProblemValue(item.title)
  const sampleFailures = Array.isArray(item.samples)
    ? item.samples.flatMap((sample) => {
        if (!sample || typeof sample !== 'object') {
          return []
        }
        const row = sample as Record<string, unknown>
        const take = typeof row.take_index === 'number' ? `第 ${row.take_index} 段` : '录音片段'
        const reasons = readableProblemValue(row.reason_codes)
        return reasons ? [`${take}：${reasons}`] : []
      })
    : []
  return [primary, ...sampleFailures].filter(Boolean).join('；') || undefined
}

export function createAgentClient(baseURL: string) {
  const client = axios.create({
    baseURL,
    timeout: 120_000,
    withCredentials: isEntrySso,
  })

  // Resolve identity for every request. Tokens can be refreshed or replaced
  // after this client is created, and stale Authorization headers must not be
  // reused across account switches.
  client.interceptors.request.use((config) => {
    config.url = agentRequestPath(config.url || '', config.baseURL || baseURL)
    const identity = identityHeaders()
    const headers = AxiosHeaders.from(config.headers)
    headers.delete('Authorization')
    headers.delete('X-User-Id')
    headers.delete('X-User-Name')
    headers.delete('X-Department')
    headers.delete('X-Roles')
    if (isStandaloneMode || isEntrySso) {
      headers.delete('X-Entry-Identity')
    }
    Object.entries(identity).forEach(([key, value]) => headers.set(key, value))
    config.headers = headers
    return config
  })

  client.interceptors.response.use(response => response, (reason) => {
    if (isEntrySso && axios.isAxiosError(reason) && reason.response?.status === 401) {
      reportEntryUnauthorized()
    }
    return Promise.reject(reason)
  })

  async function request<T>(config: AxiosRequestConfig): Promise<T> {
    try {
      const response = await client.request<T>(config)
      return response.data
    }
    catch (error) {
      if (!axios.isAxiosError(error)) {
        throw error
      }
      const requestId = error.response?.headers?.['x-request-id']
      const data = error.response?.data
      const problem = data instanceof Blob ? await blobProblemDetail(data) : normalizeProblem(data)
      const validation = problem?.errors?.map(item => readableProblemValue(item.msg)).filter(Boolean).join('；')
      const message = validation
        || readableProblemValue(problem?.detail)
        || readableProblemValue(problem?.title)
        || error.message
        || '请求失败'
      const detail = problem?.detail as unknown
      const code = detail && typeof detail === 'object' && 'code' in detail && typeof detail.code === 'string'
        ? detail.code
        : undefined
      throw Object.assign(new Error(requestId ? `${message}（请求编号：${requestId}）` : message), {
        status: error.response?.status,
        code,
      })
    }
  }

  return { client, request }
}

/** Identity headers and credentials for fetch() calls Axios cannot make, such as reading a stream. */
export function agentFetchInit(): RequestInit {
  return { headers: identityHeaders(), credentials: isEntrySso ? 'include' : 'same-origin' }
}

export async function downloadBlob(
  client: AxiosInstance,
  url: string,
): Promise<{ blob: Blob, fileName?: string }> {
  try {
    const response = await client.get<Blob>(url, { responseType: 'blob' })
    const disposition = response.headers['content-disposition'] as string | undefined
    const encoded = disposition?.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
    const plain = disposition?.match(/filename="?([^";]+)"?/i)?.[1]
    return {
      blob: response.data,
      fileName: encoded ? decodeURIComponent(encoded) : plain,
    }
  }
  catch (error) {
    if (axios.isAxiosError(error) && error.response?.data instanceof Blob) {
      const problem = await blobProblemDetail(error.response.data)
      throw new Error(
        readableProblemValue(problem?.detail)
        || readableProblemValue(problem?.title)
        || error.message,
      )
    }
    throw error
  }
}

export function saveBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  URL.revokeObjectURL(url)
}
