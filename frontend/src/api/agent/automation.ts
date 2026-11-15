import { automationPurpose } from './automationPolicy'
import { createAgentClient, downloadBlob } from './http'

export type AutomationPlatform = 'xianyu' | 'douyin' | 'xiaohongshu' | 'kuaishou'
export type AutomationExecutionMode = 'browser' | 'android'
export type AutomationOperation = 'publish' | 'relist' | 'delist'
export type AutomationPurpose = 'listing' | 'promotion'
export type AutomationRunStatus
  = | 'queued'
    | 'preparing'
    | 'ready'
    | 'publishing'
    | 'succeeded'
    | 'partial'
    | 'failed'
    | 'unknown'
    | 'cancelled'

export interface AutomationConfig {
  name: string
  purpose?: AutomationPurpose | 'creation' | null
  execution_mode?: AutomationExecutionMode
  device_id?: string | null
  operation?: AutomationOperation
  target_url?: string | null
  target_title?: string | null
  source_mode?: 'library' | 'custom'
  copy_mode?: 'ai' | 'manual'
  custom_product_name?: string
  custom_product_description?: string
  product_id?: string | null
  approved_content_id?: string | null
  image_ids: string[]
  video_id?: string | null
  platforms: AutomationPlatform[]
  accounts?: Partial<Record<AutomationPlatform, string>>
  price?: number | null
  condition: string
  shipping_fee: number
  location: string
  instructions: string
  category?: string | null
  auto_publish: boolean
  require_review?: boolean
  image_mode?: 'existing' | 'generate'
  generated_image_count?: number
  video_mode: 'existing' | 'slideshow'
}

export interface AutomationWorkflow {
  id: string
  name: string
  config: AutomationConfig
  created_at: string
  updated_at: string
}

export interface AutomationDraft {
  generation_error?: { stage: 'images' | 'video', message: string } | null
  listing_title?: string
  listing_description?: string
  approved_content_id?: string | null
  image_model?: string | null
  image_count?: number
  video_stale?: boolean
  review?: { revision: number, state: 'pending' | 'approved', feedback?: string, approved_at?: string }
  history?: Array<{ review: { revision: number, feedback?: string }, title: string, description: string }>
  library_ids?: string[]
  model?: string
  title?: string
  description?: string
  category?: string
  price?: number | null
  has_video?: boolean
  video_ready?: boolean
  classifications?: Array<{
    id: string
    role: string
    label?: string
    reason?: string
    confidence?: number
  }>
  source_summary?: { product_name?: string, image_count?: number }
}

export interface ApprovedContent {
  id: string
  run_id: string
  revision: number
  product_id: string
  product_name: string
  title: string
  description: string
  listing_title: string
  listing_description: string
  category: string
  image_ids: string[]
  original_image_ids: string[]
  video_id?: string | null
  approved_at: string
}

export interface AutomationPlatformResult {
  status: string
  operation?: AutomationOperation
  error?: string | null
  receipt?: { url?: string, account_label?: string, status?: string, external_id?: string }
}

export interface AutomationRun {
  id: string
  workflow_id: string
  status: AutomationRunStatus
  config: AutomationConfig
  draft: AutomationDraft
  platform_results: Partial<Record<AutomationPlatform, AutomationPlatformResult>>
  events: Array<{
    id: string
    kind: string
    message: string
    platform?: string
    created_at: string
  }>
  error?: string | null
  created_at: string
  updated_at: string
}

export interface AutomationAccount {
  account_id: string
  label: string
  configured?: boolean
  missing?: string[]
  platforms?: AutomationPlatform[]
}

export interface AutomationBrowserStatus {
  available: boolean
  configured: boolean
  missing: string[]
  platforms: Partial<
    Record<
      AutomationPlatform,
      AutomationConnectionStatus & {
        operations?: Partial<Record<AutomationOperation, AutomationConnectionStatus>>
      }
    >
  >
  accounts?: AutomationAccount[]
}

export interface BrowserPlatformSession {
  platform: AutomationPlatform
  connected: boolean
  state: 'not_opened' | 'login_required' | 'logged_in' | 'unknown' | 'unavailable'
  account_id?: string
  account_label?: string
  publish_ready: boolean
  operations_ready?: Partial<Record<AutomationOperation, boolean>>
  message?: string
  url?: string
}

/** The latest known login of one platform (from the background login monitor). */
export interface BrowserLoginStatus {
  platform: AutomationPlatform
  state: BrowserPlatformSession['state']
  message: string
  account_label: string
  age_seconds: number
  /** When a platform that was logged in was found signed out (seconds since epoch). */
  lost_at: number | null
}

export interface AutomationConnectionStatus {
  configured: boolean
  ready?: boolean
  missing: string[]
  accounts: AutomationAccount[]
}

export interface AutomationPlatformDefinition {
  id: AutomationPlatform
  label: string
  purpose: AutomationPurpose
  operations: AutomationOperation[]
  execution_modes: AutomationExecutionMode[]
  requires_video: boolean
  title_limit: number
  description_limit: number
}

export interface AutomationDevice {
  device_id: string
  label: string
  configured: boolean
  connected?: boolean
  state?: string
  platforms: AutomationPlatform[]
  missing?: string[]
  busy?: boolean
  apps?: Partial<Record<AutomationPlatform, AndroidAppState>>
  storage?: AndroidStorage | null
}

export interface AndroidStorage {
  total_bytes: number
  free_bytes: number
  cache_bytes: number
  cache_limit_bytes: number
  minimum_free_bytes: number
  batch_limit_bytes: number
  retention_hours: number
  protected_files: number
  checked_at: number
}

export interface AndroidAppState {
  runtime_state?: 'running' | 'exited' | 'permission_required'
  installed?: boolean
  version?: string
  state?: 'not_installed' | 'unverified' | 'needs_consent' | 'login_required' | 'logged_in'
  account_id?: string
  account_label?: string
  publish_verified?: boolean
  checked_at?: number
  error?: string
}

export interface AndroidScreen {
  session_id: string
  platform: AutomationPlatform
  draft?: { run_id: string, note: string, media_count: number, files: string[], fields: Record<string, string | number> } | null
  frame_id: string
  width: number
  height: number
  image: string
  app: AndroidAppState
}

export interface AndroidScreenAction {
  kind: 'tap' | 'swipe' | 'text' | 'back' | 'home' | 'reopen'
  frame_id: string
  x?: number
  y?: number
  end_x?: number
  end_y?: number
  text?: string
}

export interface AutomationAndroidStatus {
  configured: boolean
  available: boolean
  ready: boolean
  missing: string[]
  adb_configured: boolean
  devices: AutomationDevice[]
  platforms: Partial<Record<AutomationPlatform, AutomationConnectionStatus>>
}

export interface AutomationStatus {
  browser?: AutomationBrowserStatus
  android?: AutomationAndroidStatus
  available?: boolean
  configured?: boolean
  missing?: string[]
  platforms?: AutomationBrowserStatus['platforms']
}

const { request, client } = createAgentClient(
  import.meta.env.VITE_CONTENT_API_URL || '/content/api',
)
const base = '/api/v1/automation'
const headers = { 'X-Content-Config-Request': '1' }

export { automationPurpose } from './automationPolicy'

// Decimal amounts are serialized as strings by the API; number inputs need numbers.
function normalizeConfig(config: AutomationConfig): AutomationConfig {
  return {
    ...config,
    purpose: config.purpose ?? automationPurpose(config),
    execution_mode: config.execution_mode || 'browser',
    device_id: config.device_id || null,
    operation: config.operation || 'publish',
    source_mode: config.source_mode || 'library',
    price: config.price == null ? null : Number(config.price),
    shipping_fee: Number(config.shipping_fee),
  }
}
function normalizeWorkflow(workflow: AutomationWorkflow): AutomationWorkflow {
  return { ...workflow, config: normalizeConfig(workflow.config) }
}
function normalizeRun(run: AutomationRun): AutomationRun {
  return {
    ...run,
    config: normalizeConfig(run.config),
    draft: { ...run.draft, price: run.draft?.price == null ? null : Number(run.draft.price) },
  }
}

export const automationApi = {
  createBatch: (items: AutomationConfig[], key: string) => request<{ items: AutomationRun[], total: number }>({
    url: `${base}/creation-batches`,
    method: 'POST',
    headers: { ...headers, 'Idempotency-Key': key },
    data: { items },
  }).then(result => ({ ...result, items: result.items.map(normalizeRun) })),
  approvedContents: () => request<{ items: ApprovedContent[], total: number }>({ url: `${base}/approved-contents` }),
  approvedContent: (id: string) => request<ApprovedContent>({ url: `${base}/approved-contents/${id}` }),
  browserSessions: (purpose?: AutomationPurpose) => request<{ items: BrowserPlatformSession[] }>({ url: `${base}/browser/sessions`, params: { purpose } }),
  openBrowser: (platform: BrowserPlatformSession['platform'], page: 'login' | 'publish') => request<BrowserPlatformSession>({ url: `${base}/browser/platforms/${platform}/open`, method: 'POST', headers, data: { page } }),
  inspectBrowser: (platform: BrowserPlatformSession['platform']) => request<BrowserPlatformSession>({ url: `${base}/browser/platforms/${platform}/inspect`, method: 'POST', headers }),
  clearBrowserLogin: (platform: BrowserPlatformSession['platform']) => request<BrowserPlatformSession>({ url: `${base}/browser/platforms/${platform}/clear`, method: 'POST', headers }),
  browserMonitor: () => request<{ items: BrowserLoginStatus[], interval_seconds: number }>({ url: `${base}/browser/monitor` }),
  cleanupAndroidMedia: (device: string) => request<AndroidStorage & { removed_files: number }>({ url: `${base}/android/devices/${device}/cleanup-media`, method: 'POST', headers }),
  openAndroidDraft: (device: string, run: string, platform: AutomationPlatform) => request<AndroidScreen>({ url: `${base}/android/devices/${device}/draft`, method: 'POST', headers, timeout: 300_000, data: { run_id: run, platform } }),
  fillAndroidDraft: (device: string, token: string) => request<AndroidScreen>({ url: `${base}/android/devices/${device}/fill-draft`, method: 'POST', headers: { ...headers, 'X-Android-Session': token } }),
  publishAndroidDraft: (device: string, token: string, run: string, frame: string) => request<AutomationRun>({ url: `${base}/android/devices/${device}/publish-draft`, method: 'POST', headers: { ...headers, 'X-Android-Session': token }, data: { run_id: run, frame_id: frame, media_confirmed: true } }).then(normalizeRun),
  openAndroid: (device: string, platform: AutomationPlatform) => request<AndroidScreen>({ url: `${base}/android/devices/${device}/sessions`, method: 'POST', headers, data: { platform } }),
  androidScreen: (device: string, token: string) => request<AndroidScreen>({ url: `${base}/android/devices/${device}/screen`, headers: { 'X-Android-Session': token } }),
  androidAction: (device: string, token: string, data: AndroidScreenAction) => request<AndroidScreen>({ url: `${base}/android/devices/${device}/actions`, method: 'POST', headers: { ...headers, 'X-Android-Session': token }, data }),
  verifyAndroid: (device: string, token: string) => request<AndroidScreen>({ url: `${base}/android/devices/${device}/verify-account`, method: 'POST', headers: { ...headers, 'X-Android-Session': token } }),
  closeAndroid: (device: string, token: string) => request({ url: `${base}/android/devices/${device}/sessions`, method: 'DELETE', headers: { ...headers, 'X-Android-Session': token } }),
  status: () => request<AutomationStatus>({ url: `${base}/executors` }),
  platforms: () => request<{ items: AutomationPlatformDefinition[] }>({ url: `${base}/platforms` }),
  inspectAndroid: () => request<AutomationAndroidStatus>({ url: `${base}/android/inspect`, method: 'POST', headers }),
  workflows: (purpose?: AutomationPurpose | 'creation' | 'legacy') =>
    request<{ items: AutomationWorkflow[], total: number }>({
      url: `${base}/workflows`,
      params: { purpose },
    }).then(result => ({ ...result, items: result.items.map(normalizeWorkflow) })),
  saveWorkflow: (data: AutomationConfig, id?: string) =>
    request<AutomationWorkflow>({
      url: id ? `${base}/workflows/${id}` : `${base}/workflows`,
      method: id ? 'PUT' : 'POST',
      headers,
      data,
    }).then(normalizeWorkflow),
  startRun: (id: string, idempotencyKey: string) =>
    request<AutomationRun>({
      url: `${base}/workflows/${id}/runs`,
      method: 'POST',
      headers: { ...headers, 'Idempotency-Key': idempotencyKey },
    }).then(normalizeRun),
  runs: (workflowId?: string, purpose?: AutomationPurpose | 'creation' | 'legacy') =>
    request<{ items: AutomationRun[], total: number }>({
      url: `${base}/runs`,
      params: { workflow_id: workflowId, purpose },
    }).then(result => ({ ...result, items: result.items.map(normalizeRun) })),
  run: (id: string) => request<AutomationRun>({ url: `${base}/runs/${id}` }).then(normalizeRun),
  action: (id: string, action: 'retry' | 'cancel' | 'publish') =>
    request<AutomationRun>({
      url: `${base}/runs/${id}/${action}`,
      method: 'POST',
      headers,
    }).then(normalizeRun),
  saveDraft: (
    id: string,
    data: Pick<AutomationDraft, 'title' | 'description' | 'listing_title' | 'listing_description' | 'price' | 'category'> & { expected_revision?: number },
  ) =>
    request<AutomationRun>({
      url: `${base}/runs/${id}/draft`,
      method: 'PUT',
      headers,
      data,
    }).then(normalizeRun),
  video: (id: string) => downloadBlob(client, `${base}/runs/${id}/video`),
  image: (id: string, index: number) => downloadBlob(client, `${base}/runs/${id}/images/${index}`),
  approve: (id: string, revision: number) => request<AutomationRun>({
    url: `${base}/runs/${id}/approve`,
    method: 'POST',
    headers,
    data: { expected_revision: revision },
  }).then(normalizeRun),
  archive: (id: string, revision: number) => request<AutomationRun>({
    url: `${base}/runs/${id}/archive`,
    method: 'POST',
    headers,
    data: { expected_revision: revision },
  }).then(normalizeRun),
  revise: (id: string, revision: number, feedback: string, scope: 'copy' | 'images' | 'video' | 'all' | 'originals') => request<AutomationRun>({
    url: `${base}/runs/${id}/revise`,
    method: 'POST',
    headers,
    data: { expected_revision: revision, feedback, scope },
  }).then(normalizeRun),
}
