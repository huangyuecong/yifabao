import type { AxiosRequestConfig } from 'axios'
import type { components as ContentOpenApiComponents } from './generated/content.openapi'
import type { AgentHealth } from './types'
import { requestContentGeneration } from './contentGenerationErrors'
import { createAgentClient, downloadBlob } from './http'
import { createImageBatchRequests, imageBatchTimeout } from './imageBatchRequest'

/** Public DTO source generated from contracts/content.openapi.json. */
export type ContentOpenApiSchemas = ContentOpenApiComponents['schemas']
type ContentSchema<Name extends keyof ContentOpenApiSchemas> = ContentOpenApiSchemas[Name]

export type TaskMode = ContentSchema<'TaskResponse'>['mode']
export type ContentLength = ContentSchema<'TaskResponse'>['length']

export type LibraryResourceKind = NonNullable<ContentSchema<'LibraryItemCreate'>['resource_kind']>
export type LibraryDataLevel = NonNullable<ContentSchema<'LibraryItemCreate'>['data_level']>

export type StageKey = ContentSchema<'WorkflowStageResponse'>['key']

export type TaskStageState = ContentSchema<'WorkflowStageResponse'>['state']

export type ArtifactKind
  = | 'topic'
    | 'copy'
    | 'compliance'
    | 'image'
    | 'storyboard'
    | 'voice'
    | 'subtitle'
    | 'ai_video_clip'
    | 'video'
    | string

export interface KnowledgeReference {
  id: string
  title: string
  category: string
  data_level: string
  version: number
  source_uri?: string | null
}

export interface AssetRef {
  id: string
  name?: string
  kind?: string
  storage_key?: string | null
  has_asset?: boolean
  download_url?: string | null
  mime_type?: string | null
  source_uri?: string | null
}

export interface StoryboardShot {
  id?: string
  order: number
  visual: string
  narration: string
  subtitle: string
  duration_seconds: number
  transition: string
  transition_duration_seconds?: number
  fit_mode?: 'contain' | 'cover' | string
  asset_id?: string | null
  [key: string]: unknown
}

export interface SubtitleSegment {
  id?: string
  index: number
  start_seconds: number
  end_seconds: number
  text: string
  timing_source?: 'provider' | 'estimated' | 'edited' | string
}

export interface ArtifactContent {
  candidates?: Array<Record<string, unknown>>
  selected_candidate_index?: number
  shots?: StoryboardShot[]
  segments?: SubtitleSegment[]
  text?: string
  title?: string
  summary?: string
  body?: string
  instruction?: string
  compliance?: ComplianceResultSummary
  passed?: boolean
  hits?: ComplianceHit[]
  references?: KnowledgeReference[]
  reference_item_ids?: string[]
  content_data_level?: string
  generation?: {
    provider_id?: string
    model?: string
    finish_reason?: string
    prompt_version?: string
    usage?: Record<string, unknown>
  }
  [key: string]: unknown
}

export type Artifact<K extends ArtifactKind = ArtifactKind> = Omit<
  ContentSchema<'ArtifactResponse'>,
  'content' | 'kind' | 'stage_key'
> & {
  kind: K
  stage_key?: StageKey | null
  content: ArtifactContent
}

export interface KnowledgeSyncStatus {
  status: string
  can_retry: boolean
  error?: string | null
  sync_enabled?: boolean
  updated_at?: string | null
  original?: {
    status: string
    can_retry: boolean
    error?: string | null
    sync_enabled?: boolean
    size_bytes?: number
    verified_at?: string | null
  }
}

export interface LibraryItem {
  id: string
  owner_id?: string
  resource_kind: LibraryResourceKind
  category: string
  name: string
  text_content: string
  data_level: LibraryDataLevel
  version: number
  active?: boolean
  storage_key?: string | null
  mime_type?: string | null
  source_uri?: string | null
  resource_entry_id?: string | null
  resource_version_id?: string | null
  tags?: string[]
  parent_id?: string | null
  source_url?: string | null
  crawled_at?: string | null
  freshness?: string | null
  facts?: Array<Record<string, unknown> | string>
  notes?: string
  metadata?: Record<string, unknown>
  has_asset?: boolean
  download_url?: string | null
  updated_at?: string
  knowledge_sync?: KnowledgeSyncStatus | null
}

export interface LibraryCategory {
  id: string
  name: string
  resource_count: number
  resource_kinds?: string[]
}

export interface LibraryRelationSummary {
  item_id?: string | null
  resource_entry_id: string
  name: string
  resource_kind: LibraryResourceKind
  category: string
  has_asset?: boolean
  download_url?: string | null
}

export interface LibraryAssetSummary {
  id: string
  media_type: string
  size_bytes?: number | null
  download_url: string
}

export interface LibraryExternalReferenceSummary {
  system: string
  external_id: string
  source_url?: string | null
}

export interface LibraryItemDetail extends LibraryItem {
  parent?: LibraryRelationSummary | null
  children: LibraryRelationSummary[]
  assets: LibraryAssetSummary[]
  external_references: LibraryExternalReferenceSummary[]
}

export interface SensitiveRule {
  id: string
  phrase: string
  rule_type: 'keyword' | 'sensitive' | 'forbidden'
  level: 'warning' | 'forbidden' | string
  replacement?: string | null
}

export type ContentTask = ContentSchema<'TaskResponse'> & {
  /** False for tasks available through administrator read access only. */
  can_edit?: boolean
  workflow_status?: string
  archived?: boolean
  archived_at?: string | null
  /** An archive-edit draft is not a listed task until final confirmation. */
  is_edit_draft?: boolean
  source_task_id?: string | null
}

function contentTaskRecency(task: Pick<ContentTask, 'updated_at' | 'created_at'>): number {
  const raw = String(task.updated_at || task.created_at || '').trim()
  if (!raw) {
    return Number.NEGATIVE_INFINITY
  }
  const isoValue = raw.replace(' ', 'T')
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/iu.test(raw)
  const normalized = /^\d{4}-\d{2}-\d{2}T/u.test(isoValue) && !hasZone ? `${isoValue}Z` : isoValue
  const parsed = Date.parse(normalized)
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed
}

export type TaskStage = ContentSchema<'WorkflowStageResponse'>

export type TaskWorkflow = ContentSchema<'WorkflowResponse'> & {
  /** Server-validated artifacts that belong to this task's current export snapshot. */
  export_artifact_ids: string[]
}

export type StageGeneratePayload = ContentSchema<'GenerateRequest'>

export type CopyImportPayload = ContentSchema<'CopyImportRequest'>

export type StoryboardImportPayload = ContentSchema<'StoryboardImportRequest'>

export type GenerationRun = Omit<
  ContentSchema<'GenerationRunResponse'>,
  'artifacts' | 'stage_key'
> & {
  stage_key: StageKey
  artifacts: Artifact[]
}

export interface ComplianceHit {
  rule_id: string
  phrase: string
  rule_type: 'keyword' | 'sensitive' | 'forbidden'
  level: string
  replacement?: string | null
  field?: string | null
  field_label?: string | null
  start?: number | null
  end?: number | null
  prefix?: string | null
  match?: string | null
  suffix?: string | null
  context?: string | null
}

export interface ComplianceResultSummary {
  passed: boolean
  hits: ComplianceHit[]
}

export interface ComplianceResult extends ComplianceResultSummary {
  artifact_id: string
}

export interface ContentAuditLog {
  id: string
  owner_id: string
  action: string
  resource_type: string
  resource_id: string
  details: Record<string, unknown>
  created_at: string
}

/** Content-agent provider contracts stay isolated from the meeting client. */
export type ContentProviderStatus = ContentSchema<'ProviderInfo'>

export type ProviderCapability = ContentSchema<'ProviderCapability'>

export type ContentCapabilities = ContentSchema<'CapabilitiesResponse'>

export type ContentServiceItemStatus = ContentSchema<'ServiceStatusItem'>

export type ContentServiceStatus = ContentSchema<'ServiceStatusResponse'>

export interface EgressGrantPayload {
  provider: string
  allowed_data_levels: string[]
  allowed_source_data_levels: string[]
  allow_task_context: boolean
  allow_compliance_terms: boolean
}

export type EgressStatus = ContentSchema<'EgressStatusResponse'>

export interface ExportManifest {
  task_id: string
  task_version?: number
  artifacts: Array<{
    id: string
    kind: string
    version: number
    path?: string
    checksum?: string
  }>
}

export type VideoProjectTrackKind = 'video' | 'overlay' | 'subtitle' | 'voice' | 'music' | 'sfx'
export type VideoProjectAssetRole = VideoProjectTrackKind | 'logo' | 'intro' | 'outro'
export type VideoUploadStatus = 'uploading' | 'completed' | 'cancelled' | 'failed'

export interface VideoUploadSessionCreate {
  project_id: string
  role: VideoProjectAssetRole
  filename: string
  media_type: string
  total_size: number
  chunk_size: number
  checksum: string
}

export interface VideoUploadSessionResponse {
  id: string
  project_id: string
  owner_id: string
  role: VideoProjectAssetRole
  filename: string
  media_type: string
  media_kind: 'image' | 'video' | 'audio' | 'subtitle'
  total_size: number
  chunk_size: number
  total_chunks: number
  checksum_sha256: string
  status: VideoUploadStatus
  project_asset_id?: string | null
  received_chunks: number[]
  missing_chunks: number[]
  received_size: number
  error?: string | null
  created_at: string
  updated_at: string
  completed_at?: string | null
}

export interface VideoUploadChunkResponse {
  upload_id: string
  index: number
  size_bytes: number
  checksum_sha256: string
  duplicate: boolean
}

export interface VideoUploadCancelResponse {
  upload_id: string
  status: 'cancelled'
}

export interface VideoProjectTimelineItem {
  id: string
  project_asset_id?: string | null
  asset_ref_id?: string | null
  artifact_id?: string | null
  library_item_id?: string | null
  resource_entry_id?: string | null
  label: string
  start_seconds: number
  duration_seconds: number
  trim_in_seconds: number
  trim_out_seconds: number
  volume: number
  speed: number
  enabled: boolean
  content?: string | null
  transform: Record<string, unknown>
  metadata: Record<string, unknown>
}

export interface VideoProjectTimelineTrack {
  id: string
  kind: VideoProjectTrackKind
  name: string
  muted: boolean
  locked: boolean
  items: VideoProjectTimelineItem[]
}

export interface VideoProjectTimeline {
  canvas: {
    width: number
    height: number
    fps: number
    aspect_ratio: string
  }
  tracks: VideoProjectTimelineTrack[]
}

export interface VideoProjectRevision {
  id: string
  project_id: string
  revision: number | null
  parent_revision_id?: string | null
  source: string
  timeline: VideoProjectTimeline
  operations: VideoProjectEditOperation[]
  created_by: string
  created_at: string
}

export interface VideoProjectMediaDerivative {
  id: string
  kind: string
  variant: string
  status: string
  download_url?: string | null
  media_type?: string | null
  checksum?: string | null
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface VideoProjectProviderNativeReference {
  eligible: boolean
  provider_id: string
  transport: string
  reference_kind: string
}

export interface VideoProjectAssetMetadata extends Record<string, unknown> {
  provider_native_reference?: VideoProjectProviderNativeReference | null
}

export interface VideoProjectAsset {
  id: string
  source_type: string
  source_id: string
  asset_ref_id?: string | null
  artifact_id?: string | null
  library_item_id?: string | null
  resource_entry_id?: string | null
  role: string
  label: string
  stream_url?: string | null
  original_url?: string | null
  metadata: VideoProjectAssetMetadata
  active: boolean
  derivatives: VideoProjectMediaDerivative[]
  created_at: string
  updated_at: string
}

export interface VideoProjectResponse {
  id: string
  task_id: string
  current_revision: number
  current_revision_id: string
  revision: VideoProjectRevision
  assets: VideoProjectAsset[]
  created_by: string
  created_at: string
  updated_at: string
}

/** Deliverables supported by the video-project export endpoint. */
export type VideoProjectDeliverable
  = | 'mixed_video'
    | 'silent_video'
    | 'voice'
    | 'music'
    | 'sfx'
    | 'source_audio'
    | 'subtitles'
    | 'clips'
    | 'project_zip'

/** Stable async job response shared by render, export and AI-video generation. */
export interface MediaJobResponse {
  id: string
  task_id: string
  generation_run_id?: string | null
  kind: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  progress: number
  result: Record<string, unknown>
  error?: string | null
  attempt: number
  max_attempts: number
  heartbeat_at?: string | null
  cancel_requested_at?: string | null
  completed_at?: string | null
  created_at: string
  updated_at: string
}

export interface VideoProjectRenderRequest {
  expected_revision: number
  profile?: 'preview-480p' | 'final-480p' | 'final-720p' | 'final-1080p'
  fps?: number
  subtitle_mode?: 'burn' | 'soft' | 'none'
  include_audio?: boolean
  idempotency_key?: string
}

export interface VideoProjectExportRequest {
  expected_revision: number
  deliverables: VideoProjectDeliverable[]
  video_profile?: '480p' | '720p' | '1080p'
  fps?: number
  subtitle_mode?: 'burn' | 'soft' | 'none'
  audio_format?: 'wav' | 'mp3'
  subtitle_format?: 'srt' | 'vtt'
  clip_item_ids?: string[]
  project_track_kinds?: VideoProjectTrackKind[]
  project_asset_ids?: string[] | null
  include_original_assets?: boolean
  idempotency_key?: string
}

export interface VideoProjectVoiceGenerationRequest {
  expected_revision: number
  text: string
  label?: string
  voice?: string | null
  speed?: number
  volume?: number
  audio_format?: 'wav' | 'mp3' | 'ogg_opus'
  sample_rate?: number
  with_timestamps?: boolean
  instruction?: string | null
  parent_artifact_id?: string | null
}

export interface VideoProjectSubtitleSegment {
  sequence: number
  text: string
  start_seconds: number
  end_seconds: number
  timing_source: 'provider' | 'estimated'
}

export interface VideoProjectSubtitleGenerationRequest {
  expected_revision: number
  text?: string | null
  label?: string
  source_project_asset_id?: string | null
  duration_seconds?: number | null
  max_chars_per_line?: number | null
  max_lines?: number | null
  parent_artifact_id?: string | null
}

export interface VideoProjectSubtitleGenerationResponse {
  asset: VideoProjectAsset
  artifact_id: string
  source_project_asset_id?: string | null
  text: string
  duration_seconds: number
  segments: VideoProjectSubtitleSegment[]
  suggested_edit: {
    expected_revision: number
    operations: VideoProjectEditOperation[]
  }
}

export interface VideoProjectImageGenerationRequest {
  expected_revision: number
  prompt: string
  role?: 'video' | 'overlay' | 'logo' | 'intro' | 'outro'
  count?: number
  label?: string
  size?: string | null
  ratio?: string | null
  watermark?: boolean
  seed?: number | null
  reference_asset_ids?: string[]
  parent_artifact_id?: string | null
}

const requestImageBatch = createImageBatchRequests<VideoProjectAsset[]>()

export interface VideoProjectVideoGenerationRequest {
  expected_revision: number
  prompt: string
  label?: string
  source_copy_artifact_id?: string | null
  reference_image_asset_ids?: string[]
  reference_video_asset_ids?: string[]
  duration_seconds?: number | null
  ratio?: string | null
  resolution?: string | null
  generate_audio?: boolean
  return_last_frame?: boolean
  idempotency_key?: string
  parent_artifact_id?: string | null
}

export interface VideoProjectEditOperation {
  op_id: string
  type:
    | 'add'
    | 'remove'
    | 'move'
    | 'trim'
    | 'split'
    | 'update'
    | 'restore'
    | 'update_canvas'
    | 'update_track'
  track_id?: string
  track_kind?: VideoProjectTrackKind
  item_id?: string
  item?: VideoProjectTimelineItem
  index?: number
  start_seconds?: number
  at_seconds?: number
  new_item_id?: string
  trim_in_seconds?: number
  trim_out_seconds?: number
  duration_seconds?: number
  changes?: Record<string, unknown>
  revision_id?: string
  [key: string]: unknown
}

export interface AiEditProposalResponse {
  id: string
  project_id: string
  base_revision_id: string
  candidate_revision_id: string
  prompt: string
  operations: VideoProjectEditOperation[]
  status: 'pending' | 'adopted' | 'rejected'
  selected_operation_ids: string[]
  created_by: string
  resolved_by?: string | null
  created_at: string
  resolved_at?: string | null
}

export interface AiEditProposalCompareResponse {
  proposal: AiEditProposalResponse
  base_revision: VideoProjectRevision
  candidate_revision: VideoProjectRevision
  differences: Array<Record<string, unknown>>
}

export interface AiEditProposalDecisionResponse {
  proposal: AiEditProposalResponse
  project: VideoProjectResponse
}

export type ArtifactLinkResult = ContentSchema<'ArtifactLinkResponse'>

export type LibraryItemPayload = ContentSchema<'LibraryItemCreate'>

export type LibraryItemVersionPayload = ContentSchema<'LibraryItemVersionCreate'>

export type LibraryVersionUploadPayload = Omit<
  LibraryItemVersionPayload,
  'text_content' | 'metadata'
> & { file: File, text_content?: string }

const { client, request } = createAgentClient(
  import.meta.env.VITE_CONTENT_API_URL || '/content/api',
)

function generationRequest<T>(config: AxiosRequestConfig) {
  return requestContentGeneration<T>(client, config)
}

export const contentApi = {
  health: () => request<AgentHealth>({ url: '/health' }),
  providers: () => request<ContentProviderStatus[]>({ url: '/api/v1/providers' }),
  capabilities: () => request<ContentCapabilities>({ url: '/api/v1/capabilities' }),
  serviceStatus: () => request<ContentServiceStatus>({ url: '/api/v1/service-status' }),
  // The service caps each response page at 100 rows.  The library picker and
  // catalog need the complete visible catalog for client-side filtering and
  // product/media counts, so walk the bounded pages here.
  listLibrary: async () => {
    const pageSize = 100
    const resources: LibraryItem[] = []
    for (let page = 1; page <= 1000; page += 1) {
      const current = await request<LibraryItem[]>({
        url: '/api/v1/library/items',
        params: { page, page_size: pageSize },
      })
      resources.push(...current)
      if (current.length < pageSize) {
        break
      }
    }
    return resources
  },
  getLibraryItemDetail: (id: string) =>
    request<LibraryItemDetail>({ url: `/api/v1/library/items/${id}` }),
  getLibraryKnowledgeSync: (id: string) =>
    request<KnowledgeSyncStatus>({ url: `/api/v1/library/items/${id}/knowledge-sync` }),
  retryLibraryKnowledgeSync: (id: string) =>
    request<KnowledgeSyncStatus>({ url: `/api/v1/library/items/${id}/knowledge-sync/retry`, method: 'post' }),
  retryLibraryOriginalArchive: (id: string) =>
    request<KnowledgeSyncStatus>({ url: `/api/v1/library/items/${id}/original-archive/retry`, method: 'post' }),
  listLibraryCategories: () => request<LibraryCategory[]>({ url: '/api/v1/library/categories' }),
  uploadImageReference: (taskId: string, file: File) => {
    const data = new FormData()
    data.append('file', file)
    return request<LibraryItem>({
      url: `/api/v1/tasks/${taskId}/image-references`,
      method: 'POST',
      data,
    })
  },
  createLibrary: (data: LibraryItemPayload) =>
    request<LibraryItem>({ url: '/api/v1/library/items', method: 'POST', data }),
  uploadLibrary: (data: {
    resource_kind: LibraryResourceKind
    category: string
    name: string
    data_level: string
    tags?: string[]
    parent_id?: string | null
    source_url?: string | null
    crawled_at?: string | null
    freshness?: string | null
    facts?: Array<Record<string, unknown> | string>
    notes?: string
    text_content?: string
    file: File
  }) => {
    const form = new FormData()
    form.append('file', data.file)
    if (data.text_content !== undefined) {
      form.append('text_content', data.text_content)
    }
    return request<LibraryItem>({
      url: '/api/v1/library/items/upload',
      timeout: 600_000,
      method: 'POST',
      params: {
        resource_kind: data.resource_kind,
        category: data.category,
        name: data.name,
        data_level: data.data_level,
        tags: data.tags?.join(','),
        parent_id: data.parent_id || undefined,
        source_url: data.source_url || undefined,
        crawled_at: data.crawled_at || undefined,
        freshness: data.freshness || undefined,
        facts: data.facts?.length ? JSON.stringify(data.facts) : undefined,
        notes: data.notes || undefined,
      },
      data: form,
    })
  },
  createLibraryVersion: (id: string, data: LibraryItemVersionPayload) =>
    request<LibraryItem>({
      url: `/api/v1/library/items/${id}/versions`,
      method: 'POST',
      data,
    }),
  uploadLibraryVersion: (id: string, data: LibraryVersionUploadPayload) => {
    const form = new FormData()
    form.append('file', data.file)
    if (data.text_content !== undefined) {
      form.append('text_content', data.text_content)
    }
    return request<LibraryItem>({
      url: `/api/v1/library/items/${id}/versions/upload`,
      timeout: 600_000,
      method: 'POST',
      params: {
        expected_version: data.expected_version,
        resource_kind: data.resource_kind,
        category: data.category,
        name: data.name,
        data_level: data.data_level,
        tags: data.tags?.join(','),
        parent_id: data.parent_id || undefined,
        source_url: data.source_url || undefined,
        crawled_at: data.crawled_at || undefined,
        freshness: data.freshness || undefined,
        facts: data.facts?.length ? JSON.stringify(data.facts) : undefined,
        notes: data.notes || undefined,
      },
      data: form,
    })
  },
  deleteLibrary: (id: string) =>
    request<{ id: string, deleted: boolean }>({
      url: `/api/v1/library/items/${id}`,
      method: 'DELETE',
    }),
  downloadLibraryItem: (id: string) => downloadBlob(client, `/api/v1/library/items/${id}/download`),
  downloadLibraryUrl: (url: string) => downloadBlob(client, url),
  downloadVideoProjectAssetUrl: (url: string) => downloadBlob(client, url),
  listRules: () => request<SensitiveRule[]>({ url: '/api/v1/sensitive-rules' }),
  createRule: (data: {
    phrase: string
    rule_type: 'keyword' | 'sensitive' | 'forbidden'
    replacement?: string
  }) => request<SensitiveRule>({ url: '/api/v1/sensitive-rules', method: 'POST', data }),
  deleteRule: (id: string) =>
    request<{ id: string, deleted: boolean }>({
      url: `/api/v1/sensitive-rules/${id}`,
      method: 'DELETE',
    }),
  // The service paginates task results (and intentionally caps a single page).
  // Keep the home/history views complete by walking the pages instead of
  // silently showing only the newest page of tasks.
  listTasks: async (options: { includeArchived?: boolean } = {}) => {
    const pageSize = 100
    const tasks: ContentTask[] = []
    for (let page = 1; page <= 1000; page += 1) {
      const current = await request<ContentTask[]>({
        url: '/api/v1/tasks',
        params: {
          page,
          page_size: pageSize,
          include_archived: options.includeArchived || undefined,
        },
      })
      tasks.push(...current)
      if (current.length < pageSize) {
        break
      }
    }
    // The service sorts each page independently. Re-rank the concatenated
    // result so home/history “最近更新” is global, not first-page order.
    return tasks.sort((left, right) => {
      const recency = contentTaskRecency(right) - contentTaskRecency(left)
      return recency !== 0 ? recency : right.id.localeCompare(left.id)
    })
  },
  createTask: (data: {
    mode: TaskMode
    name: string
    topic: string
    product: string
    product_resource_id?: string | null
    knowledge_resource_ids: string[]
    keywords: string[]
    audience: string
    content_type: string
    style: string
    length: ContentLength
    requirements: string
  }) => request<ContentTask>({ url: '/api/v1/tasks', method: 'POST', data }),
  getTask: (id: string) => request<ContentTask>({ url: `/api/v1/tasks/${id}` }),
  updateTask: (
    id: string,
    data: Partial<
      Pick<
        ContentTask,
        | 'name'
        | 'topic'
        | 'product'
        | 'product_resource_id'
        | 'knowledge_resource_ids'
        | 'keywords'
        | 'audience'
        | 'content_type'
        | 'style'
        | 'length'
        | 'requirements'
      >
    > & { expected_version: number },
  ) => request<ContentTask>({ url: `/api/v1/tasks/${id}`, method: 'PATCH', data }),
  archiveTask: (id: string, expectedVersion: number) =>
    request<{ id: string, archived: boolean }>({
      url: `/api/v1/tasks/${id}`,
      method: 'DELETE',
      params: { expected_version: expectedVersion },
    }),
  deleteTask: (id: string) =>
    request<{ id: string, deleted: boolean }>({
      url: `/api/v1/tasks/${id}/permanent`,
      method: 'DELETE',
    }),
  restoreTask: (id: string) =>
    request<ContentTask>({
      url: `/api/v1/tasks/${id}/restore`,
      method: 'POST',
    }),
  getWorkflow: (taskId: string) =>
    request<TaskWorkflow>({ url: `/api/v1/tasks/${taskId}/workflow` }),
  updateStageRequirements: (
    taskId: string,
    stage: StageKey,
    data: { requirements: string, expected_version: number },
  ) =>
    request<TaskStage>({
      url: `/api/v1/tasks/${taskId}/stages/${stage}/requirements`,
      method: 'PATCH',
      data,
    }),
  generateStage: (taskId: string, stage: StageKey, data: StageGeneratePayload) =>
    (['topic', 'copy', 'image', 'video_preview'].includes(stage) ? generationRequest : request)<GenerationRun>({
      url: `/api/v1/tasks/${taskId}/stages/${stage}/generate`,
      method: 'POST',
      headers: data.idempotency_key ? { 'Idempotency-Key': data.idempotency_key } : undefined,
      data,
      timeout: stage === 'image' ? imageBatchTimeout(data.count) : undefined,
    }),
  listGenerationRuns: (
    taskId: string,
    options: {
      stageKey?: StageKey
      status?: GenerationRun['status']
      page?: number
      pageSize?: number
    } = {},
  ) => request<GenerationRun[]>({
    url: `/api/v1/tasks/${taskId}/generation-runs`,
    params: {
      stage_key: options.stageKey,
      status: options.status,
      page: options.page,
      page_size: options.pageSize,
    },
  }),
  importCopy: (taskId: string, data: CopyImportPayload) =>
    request<Artifact<'copy'>>({
      url: `/api/v1/tasks/${taskId}/stages/copy/import`,
      method: 'POST',
      data,
    }),
  importStoryboard: (taskId: string, data: StoryboardImportPayload) =>
    request<Artifact<'storyboard'>>({
      url: `/api/v1/tasks/${taskId}/stages/storyboard/import`,
      method: 'POST',
      data,
    }),
  selectStageArtifacts: (
    taskId: string,
    stage: StageKey,
    data: { artifact_ids: string[], expected_version: number },
  ) =>
    request<TaskStage>({
      url: `/api/v1/tasks/${taskId}/stages/${stage}/selection`,
      method: 'PUT',
      data,
    }),
  confirmStage: (
    taskId: string,
    stage: StageKey,
    data: { expected_version: number, artifact_ids?: string[] },
  ) =>
    request<TaskStage>({
      url: `/api/v1/tasks/${taskId}/stages/${stage}/confirm`,
      method: 'POST',
      data,
    }),
  skipStage: (taskId: string, stage: StageKey, expectedVersion: number) =>
    request<TaskStage>({
      url: `/api/v1/tasks/${taskId}/stages/${stage}/skip`,
      method: 'POST',
      data: { expected_version: expectedVersion, reason: '用户选择跳过' },
    }),
  linkArtifacts: (
    targetTaskId: string,
    data: {
      source_task_id: string
      artifact_ids: string[]
      expected_version: number
    },
  ) =>
    request<ArtifactLinkResult>({
      url: `/api/v1/tasks/${targetTaskId}/artifact-links`,
      method: 'POST',
      data,
    }),
  updateTaskAccess: (taskId: string, data: { user_ids: string[], departments: string[] }) =>
    request<{ user_ids: string[], departments: string[] }>({
      url: `/api/v1/tasks/${taskId}/access`,
      method: 'PUT',
      data,
    }),
  approveEgress: (taskId: string, data: EgressGrantPayload) =>
    request<Record<string, unknown>>({
      url: `/api/v1/tasks/${taskId}/egress-grants`,
      method: 'POST',
      data,
    }),
  getEgressStatus: (taskId: string, provider?: string) =>
    request<EgressStatus>({
      url: `/api/v1/tasks/${taskId}/egress-status`,
      params: provider ? { provider } : undefined,
    }),
  getJob: (jobId: string) => request<MediaJobResponse>({ url: `/api/v1/jobs/${jobId}` }),
  cancelJob: (jobId: string) =>
    request<MediaJobResponse>({
      url: `/api/v1/jobs/${jobId}/cancel`,
      method: 'POST',
    }),
  retryJob: (jobId: string) =>
    request<MediaJobResponse>({
      url: `/api/v1/jobs/${jobId}/retry`,
      method: 'POST',
    }),
  listArtifacts: async (taskId: string) => {
    const pageSize = 100
    const artifacts: Artifact[] = []
    for (let page = 1; page <= 1000; page += 1) {
      const current = await request<Artifact[]>({
        url: `/api/v1/tasks/${taskId}/artifacts`,
        params: { page, page_size: pageSize },
      })
      artifacts.push(...current)
      if (current.length < pageSize) {
        break
      }
    }
    return artifacts
  },
  compliance: (artifactId: string) =>
    request<ComplianceResult>({
      url: `/api/v1/artifacts/${artifactId}/compliance-check`,
      method: 'POST',
    }),
  patchArtifact: (
    artifactId: string,
    content: Record<string, unknown>,
    expectedVersion: number,
    targetTaskId?: string,
  ) =>
    request<Artifact>({
      url: `/api/v1/artifacts/${artifactId}`,
      method: 'PATCH',
      data: {
        content,
        expected_version: expectedVersion,
        target_task_id: targetTaskId,
      },
    }),
  deleteArtifact: (artifactId: string) =>
    request<{ id: string, deleted: boolean }>({
      url: `/api/v1/artifacts/${artifactId}`,
      method: 'DELETE',
    }),
  transformCopy: (
    artifactId: string,
    operation: string,
    instruction: string,
    targetTaskId?: string,
  ) =>
    generationRequest<Artifact>({
      url: `/api/v1/artifacts/${artifactId}/transform-copy`,
      method: 'POST',
      data: { operation, instruction, target_task_id: targetTaskId },
    }),
  // Send the unsaved editor draft without creating a persisted content version.
  previewTransformCopy: (
    artifactId: string,
    operation: string,
    instruction: string,
    targetTaskId?: string,
    draftContent?: ArtifactContent,
    expectedVersion?: number,
  ) =>
    generationRequest<{ content: ArtifactContent }>({
      url: `/api/v1/artifacts/${artifactId}/transform-copy`,
      method: 'POST',
      data: {
        operation,
        instruction,
        preview: true,
        target_task_id: targetTaskId,
        draft_content: draftContent,
        expected_version: expectedVersion,
      },
    }),
  confirmArtifact: (artifactId: string) =>
    request<Artifact>({
      url: `/api/v1/artifacts/${artifactId}/confirm`,
      method: 'POST',
    }),
  downloadArtifact: (artifactId: string, format?: string) =>
    downloadBlob(
      client,
      `/api/v1/artifacts/${artifactId}/download${format ? `?format=${encodeURIComponent(format)}` : ''}`,
    ),
  exportTask: (taskId: string) => downloadBlob(client, `/api/v1/tasks/${taskId}/export`),
  createVideoProject: (taskId: string) =>
    request<VideoProjectResponse>({
      url: `/api/v1/tasks/${taskId}/video-project`,
      method: 'POST',
    }),
  getVideoProject: (projectId: string) =>
    request<VideoProjectResponse>({
      url: `/api/v1/video-projects/${projectId}`,
    }),
  listVideoProjectRevisions: (projectId: string) =>
    request<VideoProjectRevision[]>({
      url: `/api/v1/video-projects/${projectId}/revisions`,
    }),
  createVideoProjectRender: (projectId: string, data: VideoProjectRenderRequest) =>
    generationRequest<MediaJobResponse>({
      url: `/api/v1/video-projects/${projectId}/renders`,
      method: 'POST',
      data,
    }),
  createVideoProjectExport: (projectId: string, data: VideoProjectExportRequest) =>
    generationRequest<MediaJobResponse>({
      url: `/api/v1/video-projects/${projectId}/exports`,
      method: 'POST',
      data,
    }),
  listVideoProjectJobs: (projectId: string) =>
    request<MediaJobResponse[]>({
      url: `/api/v1/video-projects/${projectId}/jobs`,
    }),
  uploadVideoProjectAsset: (
    projectId: string,
    data: {
      file: File
      role: VideoProjectAssetRole
      signal?: AbortSignal
      onProgress?: (loaded: number, total: number) => void
    },
  ) => {
    const form = new FormData()
    form.append('file', data.file)
    form.append('role', data.role)
    return request<VideoProjectAsset>({
      url: `/api/v1/video-projects/${projectId}/assets/upload`,
      method: 'POST',
      data: form,
      signal: data.signal,
      onUploadProgress: progress =>
        data.onProgress?.(progress.loaded, progress.total || data.file.size),
    })
  },
  createVideoUpload: (data: VideoUploadSessionCreate) =>
    request<VideoUploadSessionResponse>({
      url: '/api/v1/video-uploads',
      method: 'POST',
      data,
    }),
  getVideoUpload: (uploadId: string) =>
    request<VideoUploadSessionResponse>({
      url: `/api/v1/video-uploads/${uploadId}`,
    }),
  putVideoUploadChunk: (
    uploadId: string,
    chunkIndex: number,
    data: { chunk: Blob, checksum_sha256: string, signal?: AbortSignal },
  ) => {
    const form = new FormData()
    form.append('file', data.chunk, `${chunkIndex}.part`)
    return request<VideoUploadChunkResponse>({
      url: `/api/v1/video-uploads/${uploadId}/chunks/${chunkIndex}`,
      method: 'PUT',
      headers: { 'X-Chunk-Sha256': data.checksum_sha256 },
      data: form,
      signal: data.signal,
    })
  },
  cancelVideoUpload: (uploadId: string) =>
    request<VideoUploadCancelResponse>({
      url: `/api/v1/video-uploads/${uploadId}`,
      method: 'DELETE',
    }),
  completeVideoUpload: (uploadId: string) =>
    request<{
      upload: VideoUploadSessionResponse
      asset: VideoProjectAsset
    }>({
      url: `/api/v1/video-uploads/${uploadId}/complete`,
      method: 'POST',
      timeout: 600_000,
    }),
  generateVideoProjectMusic: (
    projectId: string,
    data: {
      expected_revision: number
      prompt: string
      duration_seconds: number
      audio_format: 'mp3' | 'wav'
      label: string
    },
  ) =>
    request<VideoProjectAsset>({
      url: `/api/v1/video-projects/${projectId}/generate-music`,
      method: 'POST',
      data,
      // Music generation polls the provider before downloading/converting the
      // result; keep this request alive beyond the shared two-minute default.
      timeout: 600_000,
    }),
  generateVideoProjectVoice: (projectId: string, data: VideoProjectVoiceGenerationRequest) =>
    request<VideoProjectAsset>({
      url: `/api/v1/video-projects/${projectId}/generate-voice`,
      method: 'POST',
      data,
    }),
  generateVideoProjectSubtitles: (
    projectId: string,
    data: VideoProjectSubtitleGenerationRequest,
  ) =>
    request<VideoProjectSubtitleGenerationResponse>({
      url: `/api/v1/video-projects/${projectId}/generate-subtitles`,
      method: 'POST',
      data,
    }),
  generateVideoProjectImages: (projectId: string, data: VideoProjectImageGenerationRequest) =>
    requestImageBatch(projectId, data, () => generationRequest<VideoProjectAsset[]>({
      url: `/api/v1/video-projects/${projectId}/generate-images`,
      method: 'POST',
      data,
      timeout: imageBatchTimeout(data.count),
    })),
  generateVideoProjectVideo: (projectId: string, data: VideoProjectVideoGenerationRequest) =>
    generationRequest<MediaJobResponse>({
      url: `/api/v1/video-projects/${projectId}/generate-video`,
      method: 'POST',
      data,
    }),
  confirmVideoProjectBrandAsset: (projectId: string, assetId: string) =>
    request<VideoProjectAsset>({
      url: `/api/v1/video-projects/${projectId}/assets/${assetId}/confirm-brand`,
      method: 'POST',
    }),
  updateVideoProjectAsset: (
    projectId: string,
    assetId: string,
    data: { label?: string, role?: VideoProjectAssetRole, active?: boolean },
  ) =>
    request<VideoProjectAsset>({
      url: `/api/v1/video-projects/${projectId}/assets/${assetId}`,
      method: 'PATCH',
      data,
    }),
  archiveVideoProjectAsset: (projectId: string, assetId: string) =>
    request<VideoProjectAsset>({
      url: `/api/v1/video-projects/${projectId}/assets/${assetId}`,
      method: 'DELETE',
    }),
  downloadJob: (jobId: string) => downloadBlob(client, `/api/v1/jobs/${jobId}/download`),
  editVideoProject: (
    projectId: string,
    data: { expected_revision: number, operations: VideoProjectEditOperation[] },
  ) =>
    request<VideoProjectResponse>({
      url: `/api/v1/video-projects/${projectId}/edits`,
      method: 'POST',
      data,
    }),
  createAiEditProposal: (
    projectId: string,
    data: {
      expected_revision: number
      prompt: string
      operations: VideoProjectEditOperation[]
      target_item_ids: string[]
    },
  ) =>
    request<AiEditProposalResponse>({
      url: `/api/v1/video-projects/${projectId}/ai-edit-proposals`,
      method: 'POST',
      data,
    }),
  getAiEditProposalCompare: (proposalId: string) =>
    request<AiEditProposalCompareResponse>({
      url: `/api/v1/ai-edit-proposals/${proposalId}/compare`,
    }),
  adoptAiEditProposal: (
    proposalId: string,
    data: { expected_revision: number, selected_operation_ids: string[] },
  ) =>
    request<AiEditProposalDecisionResponse>({
      url: `/api/v1/ai-edit-proposals/${proposalId}/adopt`,
      method: 'POST',
      data,
    }),
  rejectAiEditProposal: (proposalId: string, data: { expected_revision: number }) =>
    request<AiEditProposalDecisionResponse>({
      url: `/api/v1/ai-edit-proposals/${proposalId}/reject`,
      method: 'POST',
      data,
    }),
  auditLogs: (params?: { page?: number, page_size?: number, action?: string, resource_type?: string }) =>
    request<ContentAuditLog[]>({ url: '/api/v1/audit-logs', params }),
}
