import { ref } from 'vue'
import { createAgentClient } from './http'

/* Online-trial accounts (only on the trial server; locally `enabled` is false). */
export interface TrialMe {
  enabled: boolean
  username?: string
  display_name?: string
  is_owner?: boolean
  /** Platform logins happen in a browser on the server; the page shows its screen. */
  remote_browser?: boolean
}
export interface TrialAccount {
  username: string
  display_name: string
  is_owner: boolean
  enabled: boolean
  created_at: string | null
  last_seen_at: string | null
}
export interface TrialOperation {
  at: string
  username: string
  display_name: string
  action: string
  method: string
  path: string
  status: number
  ip: string
}

const { request } = createAgentClient(import.meta.env.VITE_CONTENT_API_URL || '/content/api')
const base = '/api/v1/trial'
const headers = { 'X-Content-Config-Request': '1' }

export const trialApi = {
  me: () => request<TrialMe>({ url: `${base}/me` }),
  accounts: () => request<{ items: TrialAccount[] }>({ url: `${base}/accounts` }),
  create: (data: { username: string, display_name: string, password: string, is_owner: boolean }) =>
    request<TrialAccount>({ url: `${base}/accounts`, method: 'POST', headers, data }),
  update: (username: string, data: Partial<{ display_name: string, password: string, enabled: boolean, is_owner: boolean }>) =>
    request<TrialAccount>({ url: `${base}/accounts/${encodeURIComponent(username)}`, method: 'PATCH', headers, data }),
  remove: (username: string) =>
    request<void>({ url: `${base}/accounts/${encodeURIComponent(username)}`, method: 'DELETE', headers }),
  operations: (params: { username?: string, day?: string, page?: number, page_size?: number }) =>
    request<{ items: TrialOperation[], total: number }>({ url: `${base}/operations`, params }),
}

type RemotePlatform = 'xianyu' | 'douyin' | 'xiaohongshu' | 'kuaishou'
const remote = '/api/v1/remote-login'
export const remoteLoginApi = {
  start: (platform: RemotePlatform) => request<unknown>({ url: `${remote}/${platform}/start`, method: 'POST', headers }),
  screen: (platform: RemotePlatform) => request<Blob>({ url: `${remote}/${platform}/screen`, responseType: 'blob', params: { t: Date.now() } }),
  click: (platform: RemotePlatform, x: number, y: number) =>
    request<unknown>({ url: `${remote}/${platform}/click`, method: 'POST', headers, data: { x, y } }),
  type: (platform: RemotePlatform, text: string, key?: 'Enter' | 'Backspace' | 'Tab') =>
    request<unknown>({ url: `${remote}/${platform}/type`, method: 'POST', headers, data: { text, key } }),
  forget: () => request<void>({ url: `${remote}/forget`, method: 'POST', headers }),
}

/** Who is signed in to the trial, loaded once by the workbench shell. */
export const trialMe = ref<TrialMe>({ enabled: false })
let loading: Promise<void> | undefined
export function loadTrialMe() {
  loading ??= trialApi.me()
    .then((value) => {
      trialMe.value = value
    })
    .catch(() => {
      loading = undefined
    })
  return loading
}
