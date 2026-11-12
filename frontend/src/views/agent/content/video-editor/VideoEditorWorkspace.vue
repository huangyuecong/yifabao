<script setup lang="ts">
/* eslint-disable financial/no-division, financial/no-float-calculation */
import type {
  SubtitleCue,
  VideoTimelineItemState,
  VideoTimelineState,
  VideoTimelineTrackState,
  VideoTrackKind,
} from './timelineState'
import type { VideoGeneratorDraftInput } from './videoGeneratorDraft'
import type { WebAvPreviewStatus } from './webavPreviewAdapter'
import type { WorkflowStageBaseline } from './workflowFinalization'
import type {
  Artifact,
  ContentCapabilities,
  ContentLength,
  ContentServiceStatus,
  ContentTask,
  LibraryItem,
  LibraryResourceKind,
  MediaJobResponse,
  ProviderCapability,
  TaskWorkflow,
  VideoProjectAsset,
  VideoProjectAssetRole,
  VideoProjectEditOperation,
  VideoProjectResponse,
  VideoProjectRevision,
  VideoProjectSubtitleGenerationResponse,
  VideoProjectSubtitleSegment,
} from '@/api/agent/content'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { contentMediaUrl } from '@/api/agent/apiUrl'
import { contentApi } from '@/api/agent/content'
import { ContentGenerationError, formatContentGenerationError } from '@/api/agent/contentGenerationErrors'
import { saveBlob } from '@/api/agent/http'
import ContentLibraryMedia from '../ContentLibraryMedia.vue'
import ContentProductPicker from '../ContentProductPicker.vue'
import { isProductResource } from '../contentResourceCatalog'
import ContentResourcePicker from '../ContentResourcePicker.vue'
import {
  compareContentDateTimeDescending,
  formatDate as formatDateTime,
  friendlyContentError,
  isReferenceImagePrivacyError,
  MIN_AI_VIDEO_DURATION_SECONDS,
  parameterOptionLabel,
} from '../contentWorkflow'
import {
  libraryResourceCanonicalId,
  productVisualReferenceResources,
} from '../imageReferenceLimits'
import { replaceProductKnowledgeSelection } from '../productSelection'
import {
  assetMediaKind,
  isProviderNativeVideoReference,
  mediaMetadataDurationSeconds,
  projectAssetDurationSeconds,
  selectProjectAssetPreviewUrl,
} from './assetMedia'
import { audioPreviewState } from './audioPreview'
import AudioWaveform from './AudioWaveform.vue'
import { fitCanvasInViewport, timelinePixelsPerSecond, timelineRulerStep } from './editorGeometry'
import { imageCandidateParameters } from './imageCandidateParameters'
import { isTerminalMediaJob, previewFps } from './mediaJobState'
import { createPreviewAudioGainController } from './previewAudioGain'
import { createPreviewPlaybackController } from './previewPlayback'
import { cancelProjectAssetUpload, uploadProjectAsset } from './resumableUpload'
import TimelineClip from './TimelineClip.vue'
import {
  addItem,
  cloneTimeline,
  createTimeline,
  duplicateItem,
  findItem,
  findTrack,
  moveAudioItemToTrack,
  moveItemByOffset,
  normalizeItem,
  normalizeTimeline,
  parseSubtitleText,
  rebaseTimelineOperationTrackIds,
  rebaseTimelineTrackIds,
  removeItem,
  reorderItem,
  splitItem,
  timelineDuration,
  TRACK_DEFINITIONS,
  trimPatch,
  updateItem,
} from './timelineState'
import { loadVideoGeneratorDraft, saveVideoGeneratorDraft } from './videoGeneratorDraft'
import { overlayPreviewStyle, subtitlePreviewStyle, videoPreviewTiming, visualFitMode, visualLayers } from './videoPreviewPresentation'
import { WebAvPreviewAdapter } from './webavPreviewAdapter'
import { workflowStageBaseline } from './workflowFinalization'

const props = withDefaults(
  defineProps<{
    taskId?: string
    initialTask?: ContentTask
    workflowMode?: boolean
  }>(),
  {
    taskId: '',
    initialTask: undefined,
    workflowMode: false,
  },
)

const emit = defineEmits<{
  taskCreated: [taskId: string]
  workflowCompleted: [payload: { artifactId: string, baseline: WorkflowStageBaseline }]
  workflowBack: []
  workflowEditCopy: []
  editorHome: []
}>()

type SaveState = 'loading' | 'saved' | 'dirty' | 'saving' | 'error'
type ImportState = 'uploading' | 'ready' | 'error' | 'cancelled'
const MAX_PROJECT_DURATION_SECONDS = 600
const DEFAULT_VIDEO_TASK_NAME = '新建快捷视频'
const AI_VIDEO_TRANSPORT_RECHECK_MS = 15_000
const contentApiBaseUrl = import.meta.env.VITE_CONTENT_API_URL || '/content/api'

interface ImportedAssetView {
  id: string
  name: string
  kind: string
  status: ImportState
  message: string
  projectAssetId?: string
  clipId?: string
  progress?: number
  uploadId?: string
  checkpointKey?: string
  controller?: AbortController
}

const taskForm = reactive({
  name: '',
  topic: '',
  product: '',
  audience: '',
  content_type: '短视频',
  style: '品牌宣传型',
  length: '中' as ContentLength,
  requirements: '',
})
const resolvedTaskId = ref(props.taskId)
const taskKnowledgeResourceIds = ref<string[]>([])
const taskProductResourceId = ref<string | null>(null)
const taskProductLabel = ref('')
const taskKeywords = ref<string[]>([])
const taskVersion = ref(0)
const resolvedTaskMode = ref<ContentTask['mode'] | ''>(props.initialTask?.mode || '')
const savedTaskPayload = ref('')
const taskWorkflow = ref<TaskWorkflow>()
const taskArtifacts = ref<Artifact[]>([])
const taskDirty = ref(false)
const pendingCreatedTaskId = ref('')
const project = ref<VideoProjectResponse>()
const timeline = ref<VideoTimelineState>({
  ...createTimeline(),
  canvas: { width: 1280, height: 720, fps: 25, aspect_ratio: '16:9' },
})
const pendingOperations = ref<VideoProjectEditOperation[]>([])
const saveState = ref<SaveState>(props.taskId ? 'loading' : 'saved')
const saveMessage = ref(props.taskId ? '正在加载视频工程' : '尚未创建，编辑后自动保存')
const loadingError = ref('')
const saveInFlight = ref(false)
const hydrated = ref(!props.taskId)
const selectedItemId = ref('')
const past = ref<VideoTimelineState[]>([])
const future = ref<VideoTimelineState[]>([])
const importedAssets = ref<ImportedAssetView[]>([])
const visualRole
  = ref<Extract<VideoProjectAssetRole, 'video' | 'intro' | 'outro'>>('video')
const fileInput = ref<HTMLInputElement>()
const playhead = ref(0)
const playing = ref(false)
const zoom = ref(1)
const previewVideoElements = new Map<string, HTMLVideoElement>()
const previewStageWrap = ref<HTMLElement>()
const previewViewport = reactive({ width: 0, height: 0 })
const previewAudioElements = new Map<string, HTMLAudioElement>()
const previewGain = createPreviewAudioGainController({
  onWarning: (message: string) => {
    ElMessage.warning(message)
  },
})
const mediaUrls = reactive<Record<string, string>>({})
const previewLoading = reactive<Record<string, boolean>>({})
const imagePreviewUrl = ref('')

function openImagePreview(url: string) {
  if (url) {
    imagePreviewUrl.value = url
  }
}

const musicPrompt = ref('')
const musicDuration = ref(15)
const musicFormat = ref<'mp3' | 'wav'>('mp3')
const musicGenerating = ref(false)
const versionsOpen = ref(false)
const revisions = ref<VideoProjectRevision[]>([])
const revisionsLoading = ref(false)
const restoringRevisionId = ref('')
const exporting = ref(false)
const workflowFinalizing = ref(false)
const workflowCompletionJob = ref<MediaJobResponse>()
const workflowBulkImporting = ref(false)
const exactPreviewOpen = ref(false)
const exactPreviewBusy = ref(false)
const exactPreviewJob = ref<MediaJobResponse>()
const exactPreviewUrl = ref('')
const exactPreviewSubtitleUrl = ref('')
const exportDrawerOpen = ref(false)
const exportJob = ref<MediaJobResponse>()
const assetLibraryRefreshing = ref(false)
const enterpriseLibraryAssets = ref<LibraryItem[]>([])
const enterpriseLibraryLoading = ref(false)
const enterpriseLibraryError = ref('')
const enterpriseSelectionIds = ref<string[]>([])
const contentCapabilities = ref<ContentCapabilities>({ capabilities: [] })
const contentServiceStatus = ref<ContentServiceStatus>()
const aiVideoCapabilityLoading = ref(false)
const aiVideoCapabilityRefreshing = ref(false)
const aiVideoCapabilityError = ref('')
const aiServiceAttemptChecking = ref(false)
const projectAssetActionBusyId = ref('')
const aiGeneratorPanels = ref<string[]>(['video'])
const voiceGenerating = ref(false)
const subtitleGenerating = ref(false)
const imageGenerating = ref(false)
const imageGenerationUncertain = ref(false)
const imageResultRefreshing = ref(false)
const videoGenerating = ref(false)
const aiVideoSubmitting = ref(false)
let aiVideoSubmissionSequence = 0
const aiVideoJob = ref<MediaJobResponse>()
const notifiedAiVideoFailureId = ref('')
const aiVideoJobErrorMessage = computed(() => {
  return aiVideoJob.value?.status === 'failed'
    ? formatContentGenerationError(aiVideoJob.value, 'AI 视频生成失败，服务未返回具体原因，请稍后重试')
    : ''
})
const aiVideoJobNeedsReferenceChange = computed(
  () =>
    aiVideoJob.value?.status === 'failed'
    && isReferenceImagePrivacyError(aiVideoJob.value.error),
)
const aiVideoReferenceError = ref('')
const brandConfirmingAssetId = ref('')
const voiceForm = reactive({
  text: '',
  label: 'AI 配音候选',
  voice: '',
  speed: 1,
  volume: 1,
  audioFormat: 'mp3' as 'wav' | 'mp3' | 'ogg_opus',
  instruction: '',
  generateSubtitles: true,
})
const subtitleForm = reactive({
  sourceMode: 'text' as 'voice' | 'text',
  sourceProjectAssetId: '',
  sourceText: '',
  text: '',
  label: 'AI 字幕',
  maxCharsPerLine: 24,
  maxLines: 2,
})
const imageForm = reactive({
  prompt: '',
  label: 'AI 画面候选',
  role: 'video' as Extract<VideoProjectAssetRole, 'video' | 'overlay' | 'logo' | 'intro' | 'outro'>,
  count: 1,
  size: '',
  ratio: '',
  watermark: false,
})
const aiVideoForm = reactive({
  prompt: '',
  referenceImageAssetIds: [] as string[],
  referenceVideoAssetIds: [] as string[],
  durationSeconds: 5,
  ratio: '',
  resolution: '',
  generateAudio: true,
  returnLastFrame: false,
})
const liveMessage = ref('')
const webAvStatus = ref<WebAvPreviewStatus>({
  state: 'disabled',
  reason: 'WebAV 预览特性未执行预检',
})
const webAvAdapter = new WebAvPreviewAdapter(status => (webAvStatus.value = status))
const ownedObjectUrls = new Set<string>()
let saveTimer: ReturnType<typeof setTimeout> | undefined
let lastSaveErrorNotice = ''
let saveCompletionWaiters: Array<() => void> = []
let playbackFrame = 0
let playbackTimestamp = 0
let playbackRevision = 0
const previewPlayback = createPreviewPlaybackController()
let editorDisposed = false
let leavingToHome = false
let previewResizeObserver: ResizeObserver | undefined
let aiVideoTransportRecheckTimer: ReturnType<typeof setTimeout> | undefined
let videoGeneratorDraftSaveTimer: ReturnType<typeof setTimeout> | undefined
let videoGeneratorDraftHydrated = false
const jobPollTimers = new Map<string, ReturnType<typeof setTimeout>>()
const enterpriseMediaKinds: LibraryResourceKind[] = ['image', 'video', 'audio']

function videoGeneratorDraftSnapshot(): VideoGeneratorDraftInput {
  return {
    voice: { ...voiceForm },
    subtitle: { ...subtitleForm },
    image: { ...imageForm },
    aiVideo: {
      ...aiVideoForm,
      referenceImageAssetIds: [...aiVideoForm.referenceImageAssetIds],
      referenceVideoAssetIds: [...aiVideoForm.referenceVideoAssetIds],
    },
    music: {
      prompt: musicPrompt.value,
      duration: musicDuration.value,
      format: musicFormat.value,
    },
  }
}

function restoreVideoGeneratorDraft() {
  if (typeof sessionStorage === 'undefined') {
    videoGeneratorDraftHydrated = true
    return
  }
  const draft = loadVideoGeneratorDraft(
    sessionStorage,
    resolvedTaskId.value,
    videoGeneratorDraftSnapshot(),
  )
  if (draft) {
    Object.assign(voiceForm, draft.voice)
    Object.assign(subtitleForm, draft.subtitle)
    Object.assign(imageForm, draft.image)
    // Hide new overlay creation without changing any existing timeline clips.
    if (imageForm.role === 'overlay' || imageForm.role === 'logo') {
      imageForm.role = 'video'
    }
    Object.assign(aiVideoForm, draft.aiVideo)
    musicPrompt.value = draft.music.prompt
    musicDuration.value = draft.music.duration
    musicFormat.value = draft.music.format
  }
  videoGeneratorDraftHydrated = true
}

function persistVideoGeneratorDraft() {
  if (
    !videoGeneratorDraftHydrated
    || !resolvedTaskId.value
    || typeof sessionStorage === 'undefined'
  ) {
    return
  }
  saveVideoGeneratorDraft(
    sessionStorage,
    resolvedTaskId.value,
    videoGeneratorDraftSnapshot(),
  )
}

function scheduleVideoGeneratorDraftSave() {
  if (!videoGeneratorDraftHydrated) {
    return
  }
  if (videoGeneratorDraftSaveTimer) {
    clearTimeout(videoGeneratorDraftSaveTimer)
  }
  videoGeneratorDraftSaveTimer = setTimeout(() => {
    videoGeneratorDraftSaveTimer = undefined
    persistVideoGeneratorDraft()
  }, 200)
}

const saveStateLabel = computed(
  () =>
    ({
      loading: '加载中',
      saved: '已保存',
      dirty: '待保存',
      saving: '保存中',
      error: '保存失败',
    })[saveState.value],
)
const saveStateType = computed(() =>
  saveState.value === 'error' ? 'danger' : saveState.value === 'saved' ? 'success' : 'warning',
)
const showToolbarSaveFeedback = computed(() =>
  ['loading', 'saving', 'error'].includes(saveState.value),
)
const showFullWorkflowContext = computed(
  () => props.workflowMode && resolvedTaskMode.value === 'full',
)
const duration = computed(() => Math.max(0, timelineDuration(timeline.value)))
const pixelsPerSecond = computed(() => timelinePixelsPerSecond(zoom.value))
const timelineWidth = computed(() =>
  Math.ceil(Math.max(duration.value, 30) * pixelsPerSecond.value),
)
const zoomPercent = computed(() => Math.round(zoom.value * 100))
const rulerTicks = computed(() => {
  const upper = Math.ceil(Math.max(duration.value, 30))
  const step = Math.max(
    upper > 180 ? 10 : upper > 90 ? 5 : 1,
    timelineRulerStep(pixelsPerSecond.value),
  )
  const values: number[] = []
  for (let value = 0; value <= upper; value += step) {
    values.push(value)
  }
  return values
})
const selectedLocated = computed(() =>
  selectedItemId.value ? findItem(timeline.value, selectedItemId.value) : undefined,
)
const selectedItem = computed(() => selectedLocated.value?.item)
const selectedTrack = computed(() => selectedLocated.value?.track)
const selectedCanTrim = computed(() => Boolean(selectedItem.value && (
  ['voice', 'music', 'sfx'].includes(selectedTrack.value?.kind || '')
  || (selectedTrack.value?.kind === 'video' && isVideoPreviewItem(selectedItem.value))
)))
const selectedHasAudio = computed(() => selectedCanTrim.value && (
  selectedTrack.value?.kind !== 'video'
  || (selectedItem.value?.metadata.source_audio !== 'mute'
    && selectedItem.value?.metadata.source_audio !== 'discard')
))
const subtitleOutputMode = computed<'burn' | 'soft'>(() => {
  const mode = findTrack(timeline.value, 'subtitle')?.items.find(item => ['burn', 'soft'].includes(String(item.metadata.subtitle_mode)))?.metadata.subtitle_mode
  return mode === 'soft' ? 'soft' : 'burn'
})
const selectedAudioTargetKind = computed<'voice' | 'music' | undefined>(() => {
  if (selectedTrack.value?.kind === 'voice') {
    return 'music'
  }
  if (selectedTrack.value?.kind === 'music' || selectedTrack.value?.kind === 'sfx') {
    return 'voice'
  }
  return undefined
})
const selectedAudioTargetTrack = computed(() =>
  selectedAudioTargetKind.value
    ? findTrack(timeline.value, selectedAudioTargetKind.value)
    : undefined,
)
const canMoveSelectedAudioTrack = computed(() => Boolean(
  selectedItem.value
  && selectedTrack.value
  && selectedAudioTargetTrack.value
  && !selectedTrack.value.locked
  && !selectedAudioTargetTrack.value.locked,
))
const selectedAudioMoveLabel = computed(() =>
  selectedAudioTargetKind.value === 'voice' ? '上移到配音' : '下移到背景音效',
)
const selectedAudioMoveHint = computed(() => {
  if (!selectedAudioTargetKind.value || !selectedTrack.value || !selectedItem.value) {
    return ''
  }
  if (selectedTrack.value.locked) {
    return `${timelineTrackName(selectedTrack.value.kind, selectedTrack.value.name)}轨道已锁定，请先解锁`
  }
  if (selectedAudioTargetTrack.value?.locked) {
    return `${timelineTrackName(selectedAudioTargetTrack.value.kind, selectedAudioTargetTrack.value.name)}轨道已锁定，请先解锁`
  }
  return `${selectedAudioMoveLabel.value}，保留开始时间、裁剪和音量设置`
})
const selectedAudioItem = computed(() =>
  selectedTrack.value && ['voice', 'music', 'sfx'].includes(selectedTrack.value.kind)
    ? selectedItem.value
    : undefined,
)
const visibleTimelineTracks = computed(() =>
  timeline.value.tracks.filter(track => track.kind !== 'sfx' && track.kind !== 'overlay'),
)
const videoTrack = computed(() => findTrack(timeline.value, 'video'))
const exportVideoProfile = computed(() => resolveExportVideoProfile())
const currentVisualLayers = computed(() => visualLayers(timeline.value, playhead.value))
const currentVisualItem = computed(() => currentVisualLayers.value.at(-1)?.item)
const currentSubtitles = computed(() => activeItems('subtitle').filter(item => item.content || item.label))
const currentOverlays = computed(() => activeItems('overlay'))
const currentAudio = computed(() => [
  ...activeItems('voice'),
  ...activeItems('music'),
  ...activeItems('sfx'),
])
const previewAspectRatio = computed(
  () => `${timeline.value.canvas.width} / ${timeline.value.canvas.height}`,
)
const previewStageSize = computed(() =>
  fitCanvasInViewport(
    timeline.value.canvas.width,
    timeline.value.canvas.height,
    previewViewport.width,
    previewViewport.height,
  ),
)
const previewStageStyle = computed(() => ({
  aspectRatio: previewAspectRatio.value,
  width: previewStageSize.value.width ? `${previewStageSize.value.width}px` : 'min(100%, 620px)',
  height: previewStageSize.value.height ? `${previewStageSize.value.height}px` : 'auto',
  backgroundColor: currentVisualLayers.value.find(layer => layer.backgroundColor)?.backgroundColor || '#000000',
}))
const musicDurationRange = computed(() => {
  const capability = contentCapabilities.value.capabilities.find(item => item.capability === 'music-generation')
  const raw = capability?.parameters?.duration_seconds
  const range = raw && typeof raw === 'object' && !Array.isArray(raw)
    ? raw as Record<string, unknown>
    : {}
  const declaredMin = Number(range.minimum ?? range.min)
  const declaredMax = Number(range.maximum ?? range.max)
  const min = Number.isFinite(declaredMin) && declaredMin > 0 ? declaredMin : 1
  const max = Math.min(15, Number.isFinite(declaredMax) && declaredMax >= min ? declaredMax : 15)
  const providerMin = Number(range.provider_minimum)
  return { min, max, providerMin: Number.isFinite(providerMin) && providerMin > 0 ? providerMin : capability ? min : 30 }
})
const shortMusicNotice = computed(() => Number(musicDuration.value) >= musicDurationRange.value.min
  && Number(musicDuration.value) < musicDurationRange.value.providerMin
  ? `短配乐会先生成 ${musicDurationRange.value.providerMin} 秒后截取，模型按实际生成时长计费。`
  : '')
const imageCandidateCapability = computed(() => contentCapabilities.value.capabilities.find(item => item.capability === 'image-generation'))
const imageCandidateSettings = computed(() => imageCandidateParameters(imageCandidateCapability.value, imageForm))
const imageCapabilityLoading = ref(false)
const imageCapabilityError = ref('')
const enterpriseMediaAssets = computed(() =>
  enterpriseLibraryAssets.value.filter(
    item =>
      item.active !== false
      && enterpriseMediaKinds.includes(item.resource_kind)
      && Boolean(item.has_asset || item.download_url || item.storage_key),
  ),
)
const enterpriseProjectAssets = computed(() =>
  (project.value?.assets || [])
    .filter(
      asset =>
        asset.active && (asset.source_type === 'library_item' || Boolean(asset.library_item_id)),
    )
    .slice()
    .sort((left, right) => compareContentDateTimeDescending(left.created_at, right.created_at)),
)
const workflowProjectAssets = computed(() =>
  (project.value?.assets || [])
    .filter(asset => asset.active && asset.metadata.source === 'workflow-confirmed')
    .slice()
    .sort((left, right) => compareContentDateTimeDescending(left.created_at, right.created_at)),
)
const workflowProjectAssetsToImport = computed(() =>
  workflowProjectAssets.value.filter(asset => projectAssetReferenceCount(asset) === 0),
)
const localProjectAssets = computed(() =>
  (project.value?.assets || [])
    .filter(
      asset =>
        asset.active
        && (asset.source_type === 'asset_ref' || Boolean(asset.asset_ref_id))
        && asset.metadata.source !== 'ai-generated',
    )
    .slice()
    .sort((left, right) => compareContentDateTimeDescending(left.created_at, right.created_at)),
)
function artifactFromStageMapping(
  kind: 'topic' | 'copy',
  mappedIds: readonly string[],
) {
  const ids = [...new Set(mappedIds)]
  if (ids.length !== 1) {
    return undefined
  }
  return taskArtifacts.value.find(
    artifact =>
      artifact.id === ids[0]
      && isUsableTextArtifact(artifact, kind),
  )
}

function isUsableTextArtifact(artifact: Artifact, kind: 'topic' | 'copy') {
  return artifact.kind === kind
    && !['legacy_mock', 'stale', 'discarded'].includes(artifact.status)
    && artifact.content.legacy_mock !== true
}

function latestUsableTextArtifact(kind: 'topic' | 'copy') {
  return taskArtifacts.value
    .filter(artifact => isUsableTextArtifact(artifact, kind))
    .slice()
    .sort((left, right) =>
      compareContentDateTimeDescending(left.created_at, right.created_at)
      || right.version - left.version,
    )[0]
}

const currentTopicArtifact = computed(() => {
  const stage = taskWorkflow.value?.stages.find(item => item.key === 'topic')
  return stage?.state === 'confirmed'
    ? artifactFromStageMapping('topic', stage.confirmed_artifact_ids.topic ?? [])
    : undefined
})
const currentCopyArtifact = computed(() => {
  const stage = taskWorkflow.value?.stages.find(item => item.key === 'copy')
  if (!stage || ['not_started', 'stale', 'skipped'].includes(stage.state)) {
    return undefined
  }
  // Regenerating a copy keeps the old selected mapping until a replacement is
  // ready. Preserve that current version during in-progress/failed attempts;
  // an initial attempt naturally has no selected id and remains empty.
  return artifactFromStageMapping('copy', stage.selected_artifact_ids.copy ?? [])
})
const historicalCopyArtifact = computed(
  () => currentCopyArtifact.value ? undefined : latestUsableTextArtifact('copy'),
)
const historicalCopyTopicArtifact = computed(() => {
  const sourceTitleId = String(
    historicalCopyArtifact.value?.content.source_title_artifact_id || '',
  ).trim()
  return sourceTitleId
    ? taskArtifacts.value.find(
        artifact => artifact.id === sourceTitleId && isUsableTextArtifact(artifact, 'topic'),
      )
    : undefined
})
const displayedCopyArtifact = computed(
  () => currentCopyArtifact.value ?? historicalCopyArtifact.value,
)
const displayedTopicArtifact = computed(() => {
  if (currentCopyArtifact.value) {
    return currentTopicArtifact.value
  }
  if (historicalCopyArtifact.value) {
    return historicalCopyTopicArtifact.value
  }
  return currentTopicArtifact.value ?? latestUsableTextArtifact('topic')
})
const showingHistoricalText = computed(
  () =>
    Boolean(displayedCopyArtifact.value && !currentCopyArtifact.value)
    || Boolean(displayedTopicArtifact.value && !currentTopicArtifact.value),
)
const historicalTextLabel = computed(() =>
  historicalCopyArtifact.value ? '最近文案' : '最近标题',
)
const visibleImportJobs = computed(() =>
  importedAssets.value.filter(asset => asset.status !== 'ready'),
)
const aiVideoProductKnowledgeResourceIds = computed(() => [
  ...new Set(
    [taskProductResourceId.value, ...taskKnowledgeResourceIds.value]
      .map(value => String(value || '').trim())
      .filter(Boolean),
  ),
])
const selectedAiVideoProductResources = computed(() =>
  aiVideoProductKnowledgeResourceIds.value
    .map(id =>
      enterpriseLibraryAssets.value.find(item => item.id === id || item.resource_entry_id === id),
    )
    .filter((item): item is LibraryItem => Boolean(item && isProductResource(item))),
)
const hasAiVideoProductResource = computed(() =>
  Boolean(taskProductResourceId.value || selectedAiVideoProductResources.value.length),
)
const aiVideoProductVisualReferences = computed(() =>
  productVisualReferenceResources(
    enterpriseLibraryAssets.value,
    aiVideoProductKnowledgeResourceIds.value,
  ),
)
const aiVideoReferenceImageAssets = computed(() =>
  (project.value?.assets || [])
    .filter(asset => asset.active && projectAssetMediaKind(asset) === 'image')
    .slice()
    .sort((left, right) => compareContentDateTimeDescending(left.created_at, right.created_at)),
)
const aiVideoReferenceImageCount = computed(() => aiVideoReferenceImageCanonicalIds().size)
const aiVideoReferenceImageRemaining = computed(() =>
  aiVideoReferenceImageLimit.value == null
    ? undefined
    : Math.max(0, aiVideoReferenceImageLimit.value - aiVideoReferenceImageCount.value),
)
const aiVideoReferenceVideoAssets = computed(() =>
  (project.value?.assets || [])
    .filter(asset => asset.active && projectAssetMediaKind(asset) === 'video')
    .slice()
    .sort((left, right) => compareContentDateTimeDescending(left.created_at, right.created_at)),
)
const selectedAiVideoReferenceVideos = computed(() => {
  const selectedIds = new Set(aiVideoForm.referenceVideoAssetIds)
  return aiVideoReferenceVideoAssets.value.filter(asset => selectedIds.has(asset.id))
})
const aiVideoReferenceFeedback = computed(() => {
  if (aiVideoReferenceError.value) {
    return aiVideoReferenceError.value
  }
  if (aiVideoCapabilityLoading.value) {
    return '正在读取当前模型的参考素材能力……'
  }
  const missingCapabilities: string[] = []
  if (aiVideoReferenceImageLimit.value == null) {
    missingCapabilities.push('参考图片上限')
  }
  if (aiVideoReferenceVideoLimit.value == null) {
    missingCapabilities.push('参考视频上限')
  }
  if (aiVideoReferenceVideoDurationLimit.value == null) {
    missingCapabilities.push('参考视频总时长')
  }
  if (missingCapabilities.length) {
    return `当前模型未声明${missingCapabilities.join('、')}，对应参考素材选择已禁用。`
  }
  if (!aiVideoProjectAssetTransportAvailable.value) {
    // Transport capability is reflected by the compact status tag below.  Do
    // not repeat a long configuration warning in the editor body when there
    // is no user action to take; actionable selection errors are still shown.
    return ''
  }
  const messages: string[] = []
  if (
    aiVideoReferenceImageCount.value > 0
    && aiVideoReferenceImageCount.value === aiVideoReferenceImageLimit.value
  ) {
    messages.push(
      `参考图片已达上限（${aiVideoReferenceImageLimit.value}/${aiVideoReferenceImageLimit.value}），如需更换请先移除一张`,
    )
  }
  if (aiVideoForm.referenceVideoAssetIds.length === aiVideoReferenceVideoLimit.value) {
    messages.push(
      `参考视频已达上限（${aiVideoReferenceVideoLimit.value}/${aiVideoReferenceVideoLimit.value}），如需更换请先移除一段`,
    )
  }
  if (aiVideoReferenceVideoDuration.value === aiVideoReferenceVideoDurationLimit.value) {
    messages.push(`参考视频总时长已达上限（${aiVideoReferenceVideoDurationLimit.value} 秒）`)
  }
  return messages.join('；')
})
const aiCandidateAssets = computed(() =>
  (project.value?.assets || [])
    .filter(asset => asset.active && asset.metadata.source === 'ai-generated')
    .slice()
    .sort((left, right) => compareContentDateTimeDescending(left.created_at, right.created_at)),
)
const voiceProjectAssets = computed(() =>
  (project.value?.assets || [])
    .filter(
      asset => asset.active && asset.role === 'voice' && projectAssetMediaKind(asset) === 'audio',
    )
    .slice()
    .sort((left, right) => compareContentDateTimeDescending(left.created_at, right.created_at)),
)
const aiVideoCandidateAsset = computed(() => {
  const assetId = String(aiVideoJob.value?.result?.project_asset_id || '')
  return assetId ? project.value?.assets.find(asset => asset.id === assetId) : undefined
})
const aiVideoCapability = computed(() =>
  contentCapabilities.value.capabilities.find(item => item.capability === 'ai-video-generation'),
)
const speechCapability = computed(() =>
  contentCapabilities.value.capabilities.find(item => item.capability === 'speech-synthesis'),
)
const subtitleCapability = computed(() =>
  contentCapabilities.value.capabilities.find(item => item.capability === 'subtitle-alignment'),
)
function serviceForCapability(capability: ProviderCapability | undefined) {
  if (!capability) {
    return undefined
  }
  return contentServiceStatus.value?.services.find(
    item =>
      item.capability === capability.capability && item.provider_id === capability.provider_id,
  )
}

const speechService = computed(() => {
  return serviceForCapability(speechCapability.value)
})
const speechVoiceOptions = computed(() =>
  capabilityParameterOptions(speechCapability.value, 'voices'),
)
const speechSpeedRange = computed(() => capabilityParameterRange(speechCapability.value, 'speed'))
const speechGenerationAvailable = computed(() =>
  Boolean(
    speechCapability.value?.enabled
    && speechCapability.value.mode !== 'mock'
    && speechService.value?.status !== 'disabled',
  ),
)
const speechVoiceSelectDisabled = computed(
  () =>
    aiVideoCapabilityLoading.value
    || !speechGenerationAvailable.value
    || !speechVoiceOptions.value.length,
)
const speechVoicePlaceholder = computed(() => {
  if (aiVideoCapabilityLoading.value) {
    return '正在读取可用音色'
  }
  if (!speechCapability.value) {
    return '当前服务未声明配音能力'
  }
  if (!speechGenerationAvailable.value) {
    return '当前配音真实能力未启用'
  }
  if (!speechVoiceOptions.value.length) {
    return '当前模型未提供可选音色'
  }
  return '使用服务默认音色'
})
const subtitleService = computed(() => {
  return serviceForCapability(subtitleCapability.value)
})
const subtitleGenerationAvailable = computed(() =>
  Boolean(
    subtitleCapability.value?.enabled
    && subtitleCapability.value.mode !== 'mock'
    && subtitleService.value?.status !== 'disabled',
  ),
)
const selectedSubtitleVoiceAsset = computed(() =>
  voiceProjectAssets.value.find(asset => asset.id === subtitleForm.sourceProjectAssetId),
)
const subtitleTrackLocked = computed(() => Boolean(findTrack(timeline.value, 'subtitle')?.locked))
const subtitleGenerationDisabled = computed(
  () =>
    aiServiceAttemptChecking.value
    || subtitleTrackLocked.value
    || (subtitleForm.sourceMode === 'voice'
      && (!selectedSubtitleVoiceAsset.value
        || (!String(selectedSubtitleVoiceAsset.value.metadata.text || '').trim()
          && !subtitleForm.sourceText.trim())))
        || (subtitleForm.sourceMode === 'text' && !subtitleForm.text.trim()),
)
const aiVideoCapabilityParameters = computed<Record<string, unknown>>(
  () => aiVideoCapability.value?.parameters || {},
)
const aiVideoReferenceImageLimit = computed(() => capabilityReferenceLimit('reference_images'))
const aiVideoReferenceVideoLimit = computed(() => capabilityReferenceLimit('reference_videos'))
const aiVideoReferenceVideoDurationLimit = computed(capabilityReferenceDurationLimit)
const aiVideoProjectAssetTransportAvailable = computed(
  () => capabilityReferenceConfig('reference_videos')?.project_asset_transport_available === true,
)
const aiVideoProviderNativeReferenceSupported = computed(
  () => capabilityReferenceConfig('reference_videos')?.provider_native_reference_supported === true,
)
const aiVideoProviderNativeReferenceAssets = computed(() =>
  aiVideoReferenceVideoAssets.value.filter(aiVideoReferenceVideoUsesNativeTransport),
)
const aiVideoReferenceVideoDisabled = computed(
  () =>
    aiVideoReferenceVideoLimit.value == null
    || aiVideoReferenceVideoLimit.value === 0,
)
const aiVideoReferenceVideoDuration = computed(() =>
  selectedAiVideoReferenceVideos.value.reduce(
    (total, asset) => total + (projectAssetDurationSeconds(asset) || 0),
    0,
  ),
)
const aiVideoDurations = computed(() => {
  const values = aiVideoCapabilityParameters.value.durations
  if (!Array.isArray(values)) {
    return []
  }
  return [
    ...new Set(
      values.map(value => Number(value)).filter(value => Number.isInteger(value) && value >= MIN_AI_VIDEO_DURATION_SECONDS),
    ),
  ].sort((left, right) => left - right)
})
const aiVideoDurationError = computed(() => aiVideoDurations.value.includes(aiVideoForm.durationSeconds)
  ? ''
  : '新生成视频至少 5 秒，请重新选择当前模型支持的时长。')
const aiVideoRatios = computed(() => {
  const values = aiVideoCapabilityParameters.value.ratios
  return Array.isArray(values)
    ? [...new Set(values.map(value => String(value).trim()).filter(Boolean))]
    : []
})
const aiVideoResolutions = computed(() => {
  const values = aiVideoCapabilityParameters.value.resolutions
  return Array.isArray(values)
    ? [...new Set(values.map(value => String(value).trim()).filter(Boolean))]
    : []
})
const aiVideoTextToVideoSupported = computed(
  () => aiVideoCapabilityParameters.value.text_to_video === true,
)
const aiVideoGenerateAudioSupported = computed(
  () => aiVideoCapabilityParameters.value.generate_audio === true,
)
const aiVideoReturnLastFrameSupported = computed(
  () => aiVideoCapabilityParameters.value.return_last_frame === true,
)
const aiVideoService = computed(() => serviceForCapability(aiVideoCapability.value))
const aiVideoCapabilityReady = computed(() =>
  Boolean(
    aiVideoCapability.value?.enabled
    && aiVideoCapability.value.mode !== 'mock'
    && aiVideoService.value?.status !== 'disabled'
    && aiVideoTextToVideoSupported.value
    && aiVideoDurations.value.length
    && aiVideoRatios.value.length
    && aiVideoResolutions.value.length,
  ),
)
function aiVideoRatioLabel(value: string) {
  return value === 'adaptive' ? '智能匹配' : value
}

function capabilityParameterOptions(capability: ProviderCapability | undefined, key: string) {
  const raw = capability?.parameters?.[key]
  const values = Array.isArray(raw)
    ? raw
    : raw && typeof raw === 'object' && 'options' in raw
      ? ((raw as { options?: unknown[] }).options ?? [])
      : []
  return values
    .filter(value => ['string', 'number'].includes(typeof value))
    .map(value => ({
      label: parameterOptionLabel(key, value as string | number),
      value: String(value),
    }))
}

function capabilityParameterRange(capability: ProviderCapability | undefined, key: string) {
  const raw = capability?.parameters?.[key]
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return undefined
  }
  const range = raw as { min?: unknown, max?: unknown }
  const min = typeof range.min === 'number' ? range.min : undefined
  const max = typeof range.max === 'number' ? range.max : undefined
  return min != null || max != null ? { min, max } : undefined
}

function capabilityReferenceConfig(key: 'reference_images' | 'reference_videos') {
  const value = aiVideoCapabilityParameters.value[key]
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined
}

function capabilityReferenceLimit(key: 'reference_images' | 'reference_videos') {
  const value = Number(capabilityReferenceConfig(key)?.max)
  return Number.isInteger(value) && value >= 0 ? value : undefined
}

function capabilityReferenceDurationLimit() {
  const nested = capabilityReferenceConfig('reference_videos')?.total_duration_seconds
  const value = Number(aiVideoCapabilityParameters.value.total_duration_seconds ?? nested)
  return Number.isFinite(value) && value > 0 ? value : undefined
}

function aiVideoReferenceVideoUsesNativeTransport(asset: VideoProjectAsset) {
  return isProviderNativeVideoReference(
    asset,
    aiVideoProviderNativeReferenceSupported.value,
    aiVideoCapability.value?.provider_id,
  )
}

function aiVideoReferenceVideoAssetSelectable(asset: VideoProjectAsset) {
  return (
    aiVideoProjectAssetTransportAvailable.value || aiVideoReferenceVideoUsesNativeTransport(asset)
  )
}

function aiVideoReferenceVideoOptionLabel(asset: VideoProjectAsset) {
  const label = `${asset.label} · ${projectAssetSourceLabel(asset)}`
  if (aiVideoProjectAssetTransportAvailable.value) {
    return label
  }
  return aiVideoReferenceVideoUsesNativeTransport(asset)
    ? `${label} · AI 原生引用`
    : `${label} · 需 HTTPS 媒体通道`
}

function projectAssetVisualReferenceKey(asset: VideoProjectAsset) {
  const resourceEntryId = String(
    asset.resource_entry_id
    || asset.metadata.resource_entry_id
    || linkedEnterpriseItem(asset)?.resource_entry_id
    || '',
  ).trim()
  return resourceEntryId ? `library:${resourceEntryId}` : `project:${asset.id}`
}

function aiVideoReferenceImageCanonicalIds(projectAssetIds = aiVideoForm.referenceImageAssetIds) {
  const ids = new Set(
    aiVideoProductVisualReferences.value.map(
      item => `library:${libraryResourceCanonicalId(item)}`,
    ),
  )
  const projectAssetsById = new Map(
    aiVideoReferenceImageAssets.value.map(asset => [asset.id, asset]),
  )
  for (const assetId of projectAssetIds) {
    const asset = projectAssetsById.get(assetId)
    ids.add(asset ? projectAssetVisualReferenceKey(asset) : `project:${assetId}`)
  }
  return ids
}

function updateAiVideoImageReferences(value: string[]) {
  const nextIds = [...new Set(value.map(id => String(id).trim()).filter(Boolean))]
  const isRemoval
    = nextIds.length < aiVideoForm.referenceImageAssetIds.length
      && nextIds.every(id => aiVideoForm.referenceImageAssetIds.includes(id))
  const limit = aiVideoReferenceImageLimit.value
  const currentCount = aiVideoReferenceImageCount.value
  const nextCount = aiVideoReferenceImageCanonicalIds(nextIds).size
  if (limit == null && nextCount > currentCount && !isRemoval) {
    aiVideoReferenceError.value = '当前模型未声明参考图片上限，已阻止本次添加。'
    return
  }
  if (limit != null && nextCount > limit && nextCount >= currentCount && !isRemoval) {
    aiVideoReferenceError.value = `参考图片（产品自动引用与手动选择合计）最多 ${limit} 张，当前将达到 ${nextCount} 张；已阻止本次添加。`
    return
  }
  aiVideoForm.referenceImageAssetIds = nextIds
  aiVideoReferenceError.value = ''
}

function updateAiVideoVideoReferences(value: string[]) {
  const nextIds = [...new Set(value.map(id => String(id).trim()).filter(Boolean))]
  const isRemoval
    = nextIds.length < aiVideoForm.referenceVideoAssetIds.length
      && nextIds.every(id => aiVideoForm.referenceVideoAssetIds.includes(id))
  const countLimit = aiVideoReferenceVideoLimit.value
  const durationLimit = aiVideoReferenceVideoDurationLimit.value
  if (countLimit == null && nextIds.length && !isRemoval) {
    aiVideoReferenceError.value = '当前模型未声明参考视频数量能力，已阻止本次添加。'
    return
  }
  if (countLimit != null && nextIds.length > countLimit && !isRemoval) {
    aiVideoReferenceError.value = `参考视频最多选择 ${countLimit} 段，已阻止本次添加；如需更换请先移除一段。`
    return
  }
  const candidates = new Map(aiVideoReferenceVideoAssets.value.map(asset => [asset.id, asset]))
  const selected = nextIds.map(id => candidates.get(id)).filter(Boolean) as VideoProjectAsset[]
  const newlySelected = selected.filter(
    asset => !aiVideoForm.referenceVideoAssetIds.includes(asset.id),
  )
  if (
    !aiVideoProjectAssetTransportAvailable.value
    && newlySelected.some(asset => !aiVideoReferenceVideoUsesNativeTransport(asset))
    && !isRemoval
  ) {
    aiVideoReferenceError.value
      = '工程通用参考视频通道尚未配置；当前只能选择带同模型原生引用标识的 AI 生成视频。'
    return
  }
  const durations = selected.map(projectAssetDurationSeconds)
  if (durations.some(value => value == null) && !isRemoval) {
    aiVideoReferenceError.value = '所选视频缺少可验证的时长信息，已阻止本次添加。'
    return
  }
  const totalDuration = durations.reduce<number>((total, value) => total + (value || 0), 0)
  if (durationLimit != null && totalDuration > durationLimit && !isRemoval) {
    aiVideoReferenceError.value = `参考视频总时长最多 ${durationLimit} 秒，本次选择合计 ${Number(totalDuration.toFixed(2))} 秒，已阻止本次添加。`
    return
  }
  aiVideoForm.referenceVideoAssetIds = nextIds
  aiVideoReferenceError.value = ''
}

function validateAiVideoReferences() {
  const imageLimit = aiVideoReferenceImageLimit.value
  const videoLimit = aiVideoReferenceVideoLimit.value
  const videoDurationLimit = aiVideoReferenceVideoDurationLimit.value
  const referenceImageCount = aiVideoReferenceImageCount.value
  if (referenceImageCount && imageLimit == null) {
    aiVideoReferenceError.value = '当前模型未声明参考图片上限，不能提交产品关联图或手选参考图片。'
    return false
  }
  if (imageLimit != null && referenceImageCount > imageLimit) {
    aiVideoReferenceError.value = `参考图片（产品自动引用与手动选择合计）最多 ${imageLimit} 张，当前为 ${referenceImageCount} 张。`
    return false
  }
  if (
    aiVideoForm.referenceVideoAssetIds.length
    && (videoLimit == null || videoDurationLimit == null)
  ) {
    aiVideoReferenceError.value = '当前模型未完整声明参考视频能力，不能提交所选参考视频。'
    return false
  }
  if (videoLimit != null && aiVideoForm.referenceVideoAssetIds.length > videoLimit) {
    aiVideoReferenceError.value = `参考视频最多选择 ${videoLimit} 段，请先移除多余视频。`
    return false
  }
  const imageIds = new Set(aiVideoReferenceImageAssets.value.map(asset => asset.id))
  const videoIds = new Set(aiVideoReferenceVideoAssets.value.map(asset => asset.id))
  if (aiVideoForm.referenceImageAssetIds.some(id => !imageIds.has(id))) {
    aiVideoReferenceError.value = '所选参考图片已失效，请重新选择。'
    return false
  }
  if (aiVideoForm.referenceVideoAssetIds.some(id => !videoIds.has(id))) {
    aiVideoReferenceError.value = '所选参考视频已失效，请重新选择。'
    return false
  }
  if (
    !aiVideoProjectAssetTransportAvailable.value
    && selectedAiVideoReferenceVideos.value.some(
      asset => !aiVideoReferenceVideoUsesNativeTransport(asset),
    )
  ) {
    aiVideoReferenceError.value
      = '工程通用参考视频通道尚未配置；只能提交带同模型原生引用标识的 AI 生成视频。'
    return false
  }
  const videoDurations = selectedAiVideoReferenceVideos.value.map(projectAssetDurationSeconds)
  if (videoDurations.some(value => value == null)) {
    aiVideoReferenceError.value = '所选参考视频缺少可验证的时长信息，请重新选择。'
    return false
  }
  const totalDuration = videoDurations.reduce<number>((total, value) => total + (value || 0), 0)
  if (videoDurationLimit != null && totalDuration > videoDurationLimit) {
    aiVideoReferenceError.value = `参考视频总时长最多 ${videoDurationLimit} 秒，当前合计 ${Number(totalDuration.toFixed(2))} 秒。`
    return false
  }
  aiVideoReferenceError.value = ''
  return true
}

function syncAiVideoCapabilityDefaults() {
  const ratios = aiVideoRatios.value
  const resolutions = aiVideoResolutions.value
  if (!ratios.includes(aiVideoForm.ratio)) {
    aiVideoForm.ratio = ratios.includes('16:9') ? '16:9' : (ratios[0] ?? '')
  }
  if (!resolutions.includes(aiVideoForm.resolution)) {
    aiVideoForm.resolution = resolutions.includes('720p') ? '720p' : (resolutions[0] ?? '')
  }
  if (!aiVideoGenerateAudioSupported.value) {
    aiVideoForm.generateAudio = false
  }
  if (!aiVideoReturnLastFrameSupported.value) {
    aiVideoForm.returnLastFrame = false
  }
}

function syncSpeechCapabilityDefaults() {
  if (
    voiceForm.voice
    && !speechVoiceOptions.value.some(option => option.value === voiceForm.voice)
  ) {
    voiceForm.voice = ''
  }
  const range = speechSpeedRange.value
  if (range?.min != null && voiceForm.speed < range.min) {
    voiceForm.speed = range.min
  }
  if (range?.max != null && voiceForm.speed > range.max) {
    voiceForm.speed = range.max
  }
}

async function loadAiVideoCapabilities(options: { background?: boolean } = {}) {
  if (aiVideoCapabilityLoading.value || aiVideoCapabilityRefreshing.value) {
    return
  }
  const loadingIndicator = options.background
    ? aiVideoCapabilityRefreshing
    : aiVideoCapabilityLoading
  loadingIndicator.value = true
  if (!options.background) {
    aiVideoCapabilityError.value = ''
    contentServiceStatus.value = undefined
  }
  try {
    const [capabilitiesResult, serviceStatusResult] = await Promise.allSettled([
      contentApi.capabilities(),
      contentApi.serviceStatus(),
    ])
    if (capabilitiesResult.status === 'rejected') {
      throw capabilitiesResult.reason
    }
    contentCapabilities.value = capabilitiesResult.value
    aiVideoCapabilityError.value = ''
    if (serviceStatusResult.status === 'fulfilled') {
      contentServiceStatus.value = serviceStatusResult.value
    }
    syncAiVideoCapabilityDefaults()
    syncSpeechCapabilityDefaults()
    if (
      !aiVideoCapability.value?.enabled
      || aiVideoCapability.value.mode === 'mock'
      || aiVideoService.value?.status === 'disabled'
    ) {
      aiVideoCapabilityError.value = '当前 AI 视频真实能力未启用，暂不能提交生成任务。'
    }
    else if (!aiVideoCapabilityReady.value) {
      aiVideoCapabilityError.value
        = '当前 AI 视频服务未声明完整的文生视频参数，暂不能提交生成任务。'
    }
  }
  catch (error) {
    if (!options.background) {
      aiVideoCapabilityError.value = `AI 视频能力加载失败：${messageOf(error)}`
    }
  }
  finally {
    loadingIndicator.value = false
  }
}

async function ensureEditorAiService(kind: 'voice' | 'subtitle' | 'video') {
  const ready = () => kind === 'voice'
    ? speechGenerationAvailable.value
    : kind === 'subtitle' ? subtitleGenerationAvailable.value : aiVideoCapabilityReady.value
  if (ready()) {
    return true
  }
  if (aiServiceAttemptChecking.value) {
    return false
  }
  aiServiceAttemptChecking.value = true
  try {
    // Re-read missing configuration on each deliberate retry. Do not confuse
    // an untested or previously failed service with a disabled feature.
    await loadAiVideoCapabilities()
    if (!ready()) {
      const message = kind === 'video'
        ? aiVideoCapabilityError.value || '暂时无法读取视频生成设置，请稍后再次尝试'
        : `这项${kind === 'voice' ? '配音' : '字幕'}功能暂时无法调用，请联系管理员检查服务配置，之后可再次尝试`
      ElMessage.warning(message)
      return false
    }
    return true
  }
  finally {
    aiServiceAttemptChecking.value = false
  }
}

function clearAiVideoTransportRecheck() {
  if (aiVideoTransportRecheckTimer) {
    clearTimeout(aiVideoTransportRecheckTimer)
    aiVideoTransportRecheckTimer = undefined
  }
}

function scheduleAiVideoTransportRecheck() {
  clearAiVideoTransportRecheck()
  if (
    editorDisposed
    || aiVideoProjectAssetTransportAvailable.value
    || !aiGeneratorPanels.value.includes('video')
  ) {
    return
  }
  aiVideoTransportRecheckTimer = setTimeout(async () => {
    aiVideoTransportRecheckTimer = undefined
    await loadAiVideoCapabilities({ background: true })
    scheduleAiVideoTransportRecheck()
  }, AI_VIDEO_TRANSPORT_RECHECK_MS)
}

async function recheckAiVideoCapabilities() {
  clearAiVideoTransportRecheck()
  await loadAiVideoCapabilities()
  if (!aiVideoCapabilityError.value) {
    if (aiVideoProjectAssetTransportAvailable.value) {
      ElMessage.success('工程参考视频通道已可用，可以选择已有视频')
    }
    else {
      ElMessage.info('工程参考视频通道仍不可用，系统将继续自动检测')
    }
  }
  scheduleAiVideoTransportRecheck()
}

function messageOf(error: unknown) {
  return error instanceof ContentGenerationError ? error.message : friendlyContentError(error, '请求失败，请稍后重试')
}

function createLocalId(prefix: string) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

function selectedTextCandidate(artifact: Artifact | undefined): Record<string, unknown> {
  if (!artifact) {
    return {}
  }
  const content = artifact.content
  const candidates = Array.isArray(content.candidates) ? content.candidates : []
  const selectedIndex = content.selected_candidate_index
  if (
    typeof selectedIndex === 'number'
    && Number.isInteger(selectedIndex)
    && selectedIndex >= 0
    && selectedIndex < candidates.length
  ) {
    return candidates[selectedIndex]
  }
  if (content.selected_candidate && typeof content.selected_candidate === 'object') {
    return content.selected_candidate as Record<string, unknown>
  }
  return candidates[0] ?? content
}

function currentArtifactTitle(artifact: Artifact | undefined) {
  const candidate = selectedTextCandidate(artifact)
  return String(candidate.title || artifact?.content.title || '').trim()
}

function currentArtifactBody(artifact: Artifact | undefined) {
  if (artifact?.kind !== 'copy') {
    return ''
  }
  const candidate = selectedTextCandidate(artifact)
  return String(
    candidate.body || candidate.text || artifact.content.body || artifact.content.text || '',
  ).trim()
}

function selectTaskProduct(name: string, resourceId: string) {
  const nextKnowledgeResourceIds = replaceProductKnowledgeSelection(
    enterpriseLibraryAssets.value,
    taskKnowledgeResourceIds.value,
    taskProductResourceId.value,
    resourceId,
  )
  if (
    taskForm.product === name
    && (taskProductResourceId.value || '') === resourceId
    && nextKnowledgeResourceIds.join('|') === taskKnowledgeResourceIds.value.join('|')
  ) {
    return
  }
  taskKnowledgeResourceIds.value = nextKnowledgeResourceIds
  taskForm.product = name
  taskProductResourceId.value = resourceId || null
  taskProductLabel.value = name
  markTaskDirty()
}

function operationId() {
  return createLocalId('op')
}

function formatTime(value: number) {
  const safe = Math.max(0, Number(value) || 0)
  const minutes = Math.floor(safe / 60)
  const seconds = safe - minutes * 60
  return `${String(minutes).padStart(2, '0')}:${seconds.toFixed(1).padStart(4, '0')}`
}

function applyTask(value?: ContentTask, preserveLocalFields = false) {
  if (!value) {
    return
  }
  resolvedTaskId.value = value.id
  resolvedTaskMode.value = value.mode
  taskVersion.value = value.version
  if (preserveLocalFields) {
    return
  }
  Object.assign(taskForm, {
    name: value.name === DEFAULT_VIDEO_TASK_NAME ? '' : value.name || '',
    topic: value.topic || '',
    product: value.product || '',
    audience: value.audience || '',
    content_type: value.content_type || '短视频',
    style: value.style || '品牌宣传型',
    length: value.length === '短' || value.length === '长' ? value.length : '中',
    requirements: value.requirements || '',
  })
  taskKnowledgeResourceIds.value = value.knowledge_resource_ids?.length
    ? [...value.knowledge_resource_ids]
    : value.product_resource_id
      ? [value.product_resource_id]
      : []
  taskProductResourceId.value = value.product_resource_id
  taskProductLabel.value = value.product || ''
  taskKeywords.value = [...value.keywords]
  savedTaskPayload.value = JSON.stringify(taskPayload())
}

function acknowledgeTaskSave(value: ContentTask, submittedPayload: ReturnType<typeof taskPayload>) {
  const changedWhileSaving = JSON.stringify(taskPayload()) !== JSON.stringify(submittedPayload)
  // A save acknowledges server identity/version, not a new form to display.
  applyTask(value, true)
  savedTaskPayload.value = JSON.stringify(submittedPayload)
  taskDirty.value = changedWhileSaving
}

function applyProject(
  value: VideoProjectResponse,
  applyServerTimeline = true,
  resetHistory = false,
) {
  project.value = value
  const persistedTimeline = value.revision?.timeline as VideoTimelineState
  if (applyServerTimeline) {
    timeline.value = normalizeTimeline(persistedTimeline)
  }
  else if (persistedTimeline?.tracks?.length) {
    adoptPersistedTrackIds(persistedTimeline)
  }
  if (resetHistory) {
    past.value = []
    future.value = []
  }
  void hydrateVisibleMedia()
}

function adoptPersistedTrackIds(persistedTimeline: VideoTimelineState) {
  // A standalone quick-video draft can receive its first edit before a
  // server project exists. Preserve that edit, but adopt the project's
  // authoritative track ids before saving it.
  const localTimeline = timeline.value
  pendingOperations.value = rebaseTimelineOperationTrackIds(
    pendingOperations.value,
    localTimeline,
    persistedTimeline,
  )
  timeline.value = rebaseTimelineTrackIds(localTimeline, persistedTimeline)
  past.value = past.value.map(value => rebaseTimelineTrackIds(value, persistedTimeline))
  future.value = future.value.map(value => rebaseTimelineTrackIds(value, persistedTimeline))
}

async function initialize() {
  if (!resolvedTaskId.value) {
    // Opening and leaving the editor is not a user change.  Keep the draft
    // entirely local until the first edit, import or explicit save so an empty
    // visit never pollutes the recent-task list.
    saveState.value = 'saved'
    saveMessage.value = '尚未创建，首次编辑或导入素材后自动保存'
    hydrated.value = true
    return
  }
  saveState.value = 'loading'
  loadingError.value = ''
  try {
    const [taskValue, projectValue, workflowValue, artifactValues] = await Promise.all([
      contentApi.getTask(resolvedTaskId.value),
      contentApi.createVideoProject(resolvedTaskId.value),
      contentApi.getWorkflow(resolvedTaskId.value),
      contentApi.listArtifacts(resolvedTaskId.value),
    ])
    applyTask(taskValue)
    taskWorkflow.value = workflowValue
    taskArtifacts.value = artifactValues
    const latest = await contentApi.getVideoProject(projectValue.id)
    applyProject(latest, true, true)
    saveState.value = 'saved'
    saveMessage.value = `已加载版本 ${latest.current_revision}`
    try {
      await restorePersistedVideoJobs(latest)
    }
    catch (error) {
      // The saved project remains fully usable when the job-status endpoint is
      // temporarily unavailable.  A later refresh will retry reconciliation.
      liveMessage.value = `工程已加载，但后台视频任务状态暂未恢复：${messageOf(error)}`
    }
  }
  catch (error) {
    loadingError.value = `视频工程接口不可用：${messageOf(error)}`
    saveState.value = 'error'
    saveMessage.value = loadingError.value
  }
  finally {
    hydrated.value = true
    void nextTick(observePreviewViewport)
  }
}

function markTaskDirty() {
  if (!hydrated.value) {
    return
  }
  taskDirty.value = true
  scheduleSave()
}

function queueOperation(operation: VideoProjectEditOperation) {
  const previous = pendingOperations.value.at(-1)
  const sameTarget
    = previous?.type === operation.type
      && previous.item_id === operation.item_id
      && previous.track_id === operation.track_id
  if (sameTarget && operation.type === 'update' && previous?.type === 'update') {
    previous.changes = { ...(previous.changes || {}), ...(operation.changes || {}) }
    scheduleSave()
    return
  }
  if (sameTarget && operation.type === 'update_canvas' && previous?.type === 'update_canvas') {
    previous.changes = { ...(previous.changes || {}), ...(operation.changes || {}) }
    scheduleSave()
    return
  }
  if (sameTarget && operation.type === 'update_track' && previous?.type === 'update_track') {
    previous.changes = { ...(previous.changes || {}), ...(operation.changes || {}) }
    scheduleSave()
    return
  }
  if (sameTarget && ['trim', 'move'].includes(operation.type)) {
    pendingOperations.value[pendingOperations.value.length - 1] = operation
    scheduleSave()
    return
  }
  pendingOperations.value.push(operation)
  scheduleSave()
}

function queueOperations(operations: VideoProjectEditOperation[]) {
  operations.forEach(queueOperation)
}

function scheduleSave() {
  if (editorDisposed) {
    return
  }
  saveState.value = 'dirty'
  saveMessage.value = '将在 800 毫秒无操作后自动保存'
  if (saveTimer) {
    clearTimeout(saveTimer)
  }
  saveTimer = setTimeout(() => {
    void saveNow()
  }, 800)
}

function taskPayload() {
  return {
    name: taskForm.name.trim() || DEFAULT_VIDEO_TASK_NAME,
    topic: taskForm.topic.trim(),
    product: taskForm.product.trim(),
    product_resource_id:
      taskProductResourceId.value && taskForm.product.trim() === taskProductLabel.value
        ? taskProductResourceId.value
        : null,
    knowledge_resource_ids: [...taskKnowledgeResourceIds.value],
    keywords: [...taskKeywords.value],
    audience: taskForm.audience.trim(),
    content_type: taskForm.content_type,
    style: taskForm.style,
    length: taskForm.length,
    requirements: taskForm.requirements,
  }
}

async function ensureTaskAndProject() {
  if (!resolvedTaskId.value) {
    const submittedPayload = taskPayload()
    const created = await contentApi.createTask({
      mode: 'video',
      ...submittedPayload,
    })
    acknowledgeTaskSave(created, submittedPayload)
    pendingCreatedTaskId.value = created.id
  }
  if (!project.value) {
    const createdProject = await contentApi.createVideoProject(resolvedTaskId.value)
    applyProject(createdProject, !pendingOperations.value.length)
  }
}

async function saveTaskInformation() {
  if (!taskDirty.value || !resolvedTaskId.value) {
    return
  }
  const payload = taskPayload()
  if (savedTaskPayload.value && JSON.stringify(payload) === savedTaskPayload.value) {
    taskDirty.value = false
    return
  }
  const updated = await contentApi.updateTask(resolvedTaskId.value, {
    ...payload,
    expected_version: taskVersion.value,
  })
  acknowledgeTaskSave(updated, payload)
  if (props.workflowMode) {
    // A task update can invalidate the current copy mapping. Hide the cached
    // text immediately, then reconcile it independently: the task update has
    // already succeeded and must never be retried with an obsolete version if
    // this secondary refresh happens to fail.
    taskArtifacts.value = []
    try {
      const [latestWorkflow, latestArtifacts] = await Promise.all([
        contentApi.getWorkflow(resolvedTaskId.value),
        contentApi.listArtifacts(resolvedTaskId.value),
      ])
      taskWorkflow.value = latestWorkflow
      taskArtifacts.value = latestArtifacts
    }
    catch (error) {
      liveMessage.value = `任务信息已保存，当前文案将在刷新后同步：${messageOf(error)}`
      ElMessage.warning(liveMessage.value)
    }
  }
}

function isInvalidProjectAssetSaveError(reason: string) {
  return /工程素材不存在|素材不存在|已经停用|已经归档|project asset.+(?:missing|inactive)/iu.test(
    reason,
  )
}

function reportSaveErrorOnce(message: string) {
  if (lastSaveErrorNotice === message) {
    return
  }
  lastSaveErrorNotice = message
  ElMessage({ type: 'error', message, grouping: true })
}

async function recoverFromInvalidProjectAssetSave() {
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = undefined
  }
  if (!project.value) {
    pendingOperations.value = []
    return { removed: 0, retained: 0 }
  }
  const latest = await contentApi.getVideoProject(project.value.id)
  const serverTimeline = normalizeTimeline(latest.revision?.timeline as VideoTimelineState)
  applyProject(latest, false)
  const sanitized = sanitizeTimelineForActiveProjectAssets(timeline.value)
  timeline.value = sanitized.timeline
  past.value = past.value.map(value => sanitizeTimelineForActiveProjectAssets(value).timeline)
  future.value = future.value.map(value => sanitizeTimelineForActiveProjectAssets(value).timeline)
  pendingOperations.value = timelineTransitionOperations(serverTimeline, timeline.value)
  if (selectedItemId.value && !findItem(timeline.value, selectedItemId.value)) {
    selectedItemId.value = ''
  }
  return { removed: sanitized.removed, retained: pendingOperations.value.length }
}

async function saveNow(): Promise<boolean> {
  if (saveInFlight.value) {
    return false
  }
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = undefined
  }
  saveInFlight.value = true
  saveState.value = 'saving'
  saveMessage.value = '正在保存到视频工程服务'
  let unsavedOperations: VideoProjectEditOperation[] = []
  try {
    await ensureTaskAndProject()
    const persistedTimeline = project.value?.revision?.timeline as VideoTimelineState | undefined
    if (persistedTimeline?.tracks?.length) {
      // Also repairs operations already left dirty by a previous failed save
      // (for example while this page was updated through HMR).
      adoptPersistedTrackIds(persistedTimeline)
    }
    await saveTaskInformation()
    unsavedOperations = pendingOperations.value.splice(0)
    while (unsavedOperations.length) {
      if (!project.value) {
        throw new Error('视频工程未创建，无法保存时间线')
      }
      const batch = unsavedOperations.slice(0, 100)
      const updated = await contentApi.editVideoProject(project.value.id, {
        expected_revision: project.value.current_revision,
        operations: batch,
      })
      unsavedOperations = unsavedOperations.slice(batch.length)
      applyProject(updated, unsavedOperations.length === 0 && pendingOperations.value.length === 0)
    }
    saveState.value = pendingOperations.value.length || taskDirty.value ? 'dirty' : 'saved'
    saveMessage.value
      = saveState.value === 'saved'
        ? `已保存 · 版本 ${project.value?.current_revision ?? 0}`
        : '有新的改动等待保存'
    liveMessage.value = saveMessage.value
    if (pendingCreatedTaskId.value && saveState.value === 'saved') {
      const createdId = pendingCreatedTaskId.value
      pendingCreatedTaskId.value = ''
      // A standalone editor can create its first task while the route leave
      // guard flushes the final autosave.  Do not let that late event
      // redirect an explicit "返回工作台" navigation back into the editor.
      if (!leavingToHome) {
        emit('taskCreated', createdId)
      }
    }
    if (pendingOperations.value.length || taskDirty.value) {
      scheduleSave()
    }
    lastSaveErrorNotice = ''
    return true
  }
  catch (error) {
    const reason = messageOf(error)
    if (isInvalidProjectAssetSaveError(reason)) {
      try {
        const recovery = await recoverFromInvalidProjectAssetSave()
        saveMessage.value = recovery.retained
          ? `自动保存已停止：${reason}。已移除 ${recovery.removed} 个失效素材片段，其余编辑已保留，请点击“重试保存”。`
          : `自动保存已停止：${reason}。已移除 ${recovery.removed} 个失效素材片段并同步到服务器最新版本。`
      }
      catch (recoveryError) {
        pendingOperations.value = []
        saveMessage.value = `自动保存已停止：${reason}。工程刷新失败：${messageOf(recoveryError)}`
      }
    }
    else {
      pendingOperations.value = [...unsavedOperations, ...pendingOperations.value]
      saveMessage.value = /409|conflict|revision|版本|冲突/i.test(reason)
        ? `保存冲突：${reason}。请刷新工程后再编辑。`
        : `自动保存失败：${reason}`
    }
    saveState.value = 'error'
    liveMessage.value = saveMessage.value
    reportSaveErrorOnce(saveMessage.value)
    return false
  }
  finally {
    saveInFlight.value = false
    const waiters = saveCompletionWaiters
    saveCompletionWaiters = []
    waiters.forEach(resolve => resolve())
  }
}

async function manualSave() {
  if (saveInFlight.value) {
    await new Promise<void>((resolve) => {
      saveCompletionWaiters.push(resolve)
    })
  }
  return saveNow()
}

function videoPositionOperations(
  before: VideoTimelineState,
  after: VideoTimelineState,
  primary: VideoProjectEditOperation,
) {
  const operations: VideoProjectEditOperation[] = []
  const beforeTrack = findTrack(before, 'video')
  const afterTrack = findTrack(after, 'video')
  if (!beforeTrack || !afterTrack) {
    return operations
  }
  for (const item of afterTrack.items) {
    const prior = beforeTrack.items.find(candidate => candidate.id === item.id)
    if (!prior || prior.start_seconds === item.start_seconds || primary.item_id === item.id) {
      continue
    }
    operations.push({
      op_id: operationId(),
      type: 'update',
      track_id: afterTrack.id,
      item_id: item.id,
      changes: { start_seconds: item.start_seconds },
    })
  }
  return operations
}

function timelineItemReferencesProjectAsset(item: VideoTimelineItemState, assetId: string) {
  return timelineItemProjectAssetIds(item).includes(assetId)
}

function timelineItemProjectAssetIds(item: VideoTimelineItemState) {
  return [item.project_asset_id, item.metadata.source_project_asset_id]
    .map(value => String(value || '').trim())
    .filter(Boolean)
}

function withoutProjectAssetReferences(value: VideoTimelineState, assetIds: ReadonlySet<string>) {
  let removed = 0
  const next = cloneTimeline(value)
  for (const track of next.tracks) {
    track.items = track.items.filter((item) => {
      const referencesArchivedAsset = [...assetIds].some(assetId =>
        timelineItemReferencesProjectAsset(item, assetId),
      )
      if (referencesArchivedAsset) {
        removed += 1
      }
      return !referencesArchivedAsset
    })
  }
  return { timeline: removed ? normalizeTimeline(next) : next, removed }
}

function sanitizeTimelineForActiveProjectAssets(value: VideoTimelineState) {
  if (!project.value) {
    return { timeline: cloneTimeline(value), removed: 0 }
  }
  const activeIds = new Set(
    project.value.assets.filter(asset => asset.active).map(asset => asset.id),
  )
  const unavailableIds = new Set(
    value.tracks
      .flatMap(track => track.items)
      .flatMap(timelineItemProjectAssetIds)
      .filter(assetId => !activeIds.has(assetId)),
  )
  return unavailableIds.size
    ? withoutProjectAssetReferences(value, unavailableIds)
    : { timeline: cloneTimeline(value), removed: 0 }
}

function purgeProjectAssetFromHistory(assetId: string) {
  const ids = new Set([assetId])
  past.value = past.value.map(value => withoutProjectAssetReferences(value, ids).timeline)
  future.value = future.value.map(value => withoutProjectAssetReferences(value, ids).timeline)
}

function timelineTransitionOperations(before: VideoTimelineState, after: VideoTimelineState) {
  const operations: VideoProjectEditOperation[] = []
  if (JSON.stringify(before.canvas) !== JSON.stringify(after.canvas)) {
    operations.push({
      op_id: operationId(),
      type: 'update_canvas',
      changes: { ...after.canvas },
    })
  }
  const beforeItems = new Map<
    string,
    { track: VideoTimelineTrackState, item: VideoTimelineItemState, index: number }
  >()
  for (const track of before.tracks) {
    track.items.forEach((item, index) => beforeItems.set(item.id, { track, item, index }))
  }
  const afterIds = new Set(after.tracks.flatMap(track => track.items.map(item => item.id)))
  for (const [itemId] of beforeItems) {
    if (!afterIds.has(itemId)) {
      operations.push({ op_id: operationId(), type: 'remove', item_id: itemId })
    }
  }
  for (const track of after.tracks) {
    track.items.forEach((item, index) => {
      const prior = beforeItems.get(item.id)
      if (!prior) {
        operations.push({
          op_id: operationId(),
          type: 'add',
          track_id: track.id,
          track_kind: track.kind,
          index,
          item,
        })
        return
      }
      if (
        prior.track.id !== track.id
        || prior.index !== index
        || prior.item.start_seconds !== item.start_seconds
      ) {
        operations.push({
          op_id: operationId(),
          type: 'move',
          item_id: item.id,
          track_id: track.id,
          index,
          start_seconds: item.start_seconds,
        })
      }
      const changes: Record<string, unknown> = {}
      const editableKeys: Array<keyof VideoTimelineItemState> = [
        'label',
        'start_seconds',
        'duration_seconds',
        'trim_in_seconds',
        'trim_out_seconds',
        'volume',
        'speed',
        'enabled',
        'content',
        'transform',
        'metadata',
      ]
      for (const key of editableKeys) {
        if (JSON.stringify(prior.item[key]) !== JSON.stringify(item[key])) {
          changes[key] = item[key]
        }
      }
      if (Object.keys(changes).length) {
        operations.push({
          op_id: operationId(),
          type: 'update',
          item_id: item.id,
          track_id: track.id,
          changes,
        })
      }
    })
    const priorTrack = before.tracks.find(
      candidate => candidate.id === track.id || candidate.kind === track.kind,
    )
    const trackChanges: Record<string, unknown> = {}
    for (const key of ['name', 'muted', 'locked'] as const) {
      if (priorTrack?.[key] !== track[key]) {
        trackChanges[key] = track[key]
      }
    }
    if (Object.keys(trackChanges).length) {
      operations.push({
        op_id: operationId(),
        type: 'update_track',
        track_id: track.id,
        changes: trackChanges,
      })
    }
  }
  return operations
}

function commitTimeline(
  next: VideoTimelineState,
  operation: VideoProjectEditOperation,
  recordHistory = true,
) {
  if (workflowFinalizing.value) {
    ElMessage.warning('正在合成当前时间线，请完成后再修改')
    return false
  }
  if (timelineDuration(next) > MAX_PROJECT_DURATION_SECONDS) {
    ElMessage.error('视频工程总时长不能超过 10 分钟，请先缩短或删除片段')
    return false
  }
  const before = timeline.value
  if (recordHistory) {
    past.value = [...past.value.slice(-49), cloneTimeline(before)]
    future.value = []
  }
  timeline.value = next
  queueOperations([operation, ...videoPositionOperations(before, next, operation)])
  return true
}

function undo() {
  const previous = past.value.at(-1)
  if (!previous) {
    return
  }
  const current = cloneTimeline(timeline.value)
  future.value = [current, ...future.value.slice(0, 49)]
  past.value = past.value.slice(0, -1)
  const restored = sanitizeTimelineForActiveProjectAssets(previous)
  timeline.value = restored.timeline
  queueOperations(timelineTransitionOperations(current, timeline.value))
  liveMessage.value = restored.removed
    ? `已撤销上一步操作；${restored.removed} 个已归档素材片段未恢复`
    : '已撤销上一步操作'
  if (restored.removed) {
    ElMessage({ type: 'warning', message: liveMessage.value, grouping: true })
  }
}

function redo() {
  const next = future.value[0]
  if (!next) {
    return
  }
  const current = cloneTimeline(timeline.value)
  past.value = [...past.value.slice(-49), current]
  future.value = future.value.slice(1)
  const restored = sanitizeTimelineForActiveProjectAssets(next)
  timeline.value = restored.timeline
  queueOperations(timelineTransitionOperations(current, timeline.value))
  liveMessage.value = restored.removed
    ? `已重做上一步操作；${restored.removed} 个已归档素材片段未恢复`
    : '已重做上一步操作'
  if (restored.removed) {
    ElMessage({ type: 'warning', message: liveMessage.value, grouping: true })
  }
}

function trackId(kind: VideoTrackKind) {
  return findTrack(timeline.value, kind)?.id || `track-${kind}`
}

function timelineTrackName(kind: VideoTrackKind, fallback = '') {
  if (kind === 'music') {
    return '背景音效'
  }
  return fallback || TRACK_DEFINITIONS.find(definition => definition.kind === kind)?.name || ''
}

function addTimelineItem(kind: VideoTrackKind, item: VideoTimelineItemState, index?: number) {
  const next = addItem(timeline.value, kind, item, index)
  const normalized = findItem(next, item.id)?.item || item
  const committed = commitTimeline(next, {
    op_id: operationId(),
    type: 'add',
    track_id: trackId(kind),
    track_kind: kind,
    item: normalized,
    index: findTrack(next, kind)?.items.findIndex(candidate => candidate.id === normalized.id),
  })
  if (!committed) {
    return false
  }
  selectedItemId.value = item.id
  return true
}

function deleteSelected() {
  if (!selectedItem.value || !selectedTrack.value) {
    return
  }
  const id = selectedItem.value.id
  const next = removeItem(timeline.value, id)
  commitTimeline(next, {
    op_id: operationId(),
    type: 'remove',
    track_id: selectedTrack.value.id,
    item_id: id,
  })
  selectedItemId.value = ''
  liveMessage.value = '已删除所选片段，可使用撤销恢复'
}

function duplicateSelected() {
  if (!selectedItem.value || !selectedTrack.value) {
    return
  }
  const id = createLocalId('clip')
  const next = duplicateItem(timeline.value, selectedItem.value.id, id)
  const copy = findItem(next, id)?.item
  if (!copy) {
    return
  }
  commitTimeline(next, {
    op_id: operationId(),
    type: 'add',
    track_id: selectedTrack.value.id,
    item: copy,
    index: findTrack(next, selectedTrack.value.kind)?.items.findIndex(
      candidate => candidate.id === copy.id,
    ),
  })
  if (mediaUrls[selectedItem.value.id]) {
    mediaUrls[id] = mediaUrls[selectedItem.value.id]
  }
  selectedItemId.value = id
}

function splitSelected() {
  if (!selectedItem.value || !selectedTrack.value) {
    return
  }
  const splitAt = playhead.value
  const end = selectedItem.value.start_seconds + selectedItem.value.duration_seconds
  if (splitAt <= selectedItem.value.start_seconds + 0.05 || splitAt >= end - 0.05) {
    ElMessage.warning('请把播放头移动到所选片段内部再分割')
    return
  }
  const newId = createLocalId('clip')
  const next = splitItem(timeline.value, selectedItem.value.id, splitAt, newId)
  commitTimeline(next, {
    op_id: operationId(),
    type: 'split',
    track_id: selectedTrack.value.id,
    item_id: selectedItem.value.id,
    at_seconds: splitAt - selectedItem.value.start_seconds,
    new_item_id: newId,
  })
  if (mediaUrls[selectedItem.value.id]) {
    mediaUrls[newId] = mediaUrls[selectedItem.value.id]
  }
  selectedItemId.value = newId
}

function moveSelected(offset: number) {
  if (!selectedItem.value || !selectedTrack.value) {
    return
  }
  if (selectedTrack.value.kind !== 'video') {
    positionTimelineItem(
      selectedItem.value.id,
      Math.max(0, selectedItem.value.start_seconds + offset * 0.5),
    )
    return
  }
  const index = selectedTrack.value.items.findIndex(item => item.id === selectedItem.value?.id)
  const targetIndex = Math.max(0, Math.min(index + offset, selectedTrack.value.items.length - 1))
  if (targetIndex === index) {
    return
  }
  const next = moveItemByOffset(timeline.value, selectedItem.value.id, offset)
  commitTimeline(next, {
    op_id: operationId(),
    type: 'move',
    track_id: selectedTrack.value.id,
    item_id: selectedItem.value.id,
    index: targetIndex,
    start_seconds: findItem(next, selectedItem.value.id)?.item.start_seconds,
  })
}

function moveSelectedAudioTrack() {
  const item = selectedItem.value
  const sourceTrack = selectedTrack.value
  const targetKind = selectedAudioTargetKind.value
  const targetTrack = selectedAudioTargetTrack.value
  if (!item || !sourceTrack || !targetKind || !targetTrack) {
    return
  }
  if (sourceTrack.locked || targetTrack.locked) {
    ElMessage.warning(selectedAudioMoveHint.value)
    return
  }
  const next = moveAudioItemToTrack(timeline.value, item.id, targetKind)
  const moved = findItem(next, item.id)
  if (!moved || moved.track.kind !== targetKind) {
    return
  }
  const targetIndex = moved.track.items.findIndex(candidate => candidate.id === item.id)
  if (!commitTimeline(next, {
    op_id: operationId(),
    type: 'move',
    track_id: moved.track.id,
    item_id: item.id,
    index: targetIndex,
    start_seconds: moved.item.start_seconds,
  })) {
    return
  }
  liveMessage.value = `“${item.label}”已移至${timelineTrackName(moved.track.kind, moved.track.name)}轨道，可使用撤销恢复`
  ElMessage.success(liveMessage.value)
}

function patchSelected(
  patch: Partial<VideoTimelineItemState>,
  operationType: 'update' | 'trim' = 'update',
) {
  if (!selectedItem.value || !selectedTrack.value || selectedTrack.value.locked) {
    return
  }
  const itemId = selectedItem.value.id
  try {
    const asset = projectAssetFor(selectedItem.value)
    const knownDuration = previewVideoElements.get(itemId)?.duration
      || previewAudioElements.get(itemId)?.duration
      || (asset ? projectAssetDurationSeconds(asset) : undefined)
    patch = trimPatch(selectedItem.value, patch, knownDuration, selectedTrack.value.kind)
  }
  catch (error) {
    ElMessage.warning(messageOf(error))
    return
  }
  const next = updateItem(timeline.value, itemId, patch)
  const updated = findItem(next, itemId)?.item
  if (!updated) {
    return
  }
  commitTimeline(
    next,
    operationType === 'trim'
      ? {
          op_id: operationId(),
          type: 'trim',
          track_id: selectedTrack.value.id,
          item_id: itemId,
          trim_in_seconds: updated.trim_in_seconds,
          trim_out_seconds: updated.trim_out_seconds,
          duration_seconds: updated.duration_seconds,
        }
      : {
          op_id: operationId(),
          type: 'update',
          track_id: selectedTrack.value.id,
          item_id: itemId,
          changes: {
            ...patch,
            duration_seconds: updated.duration_seconds,
            trim_in_seconds: updated.trim_in_seconds,
            trim_out_seconds: updated.trim_out_seconds,
          },
        },
  )
}

function patchSelectedNumber(
  key: keyof VideoTimelineItemState,
  value: unknown,
  operationType: 'update' | 'trim' = 'update',
) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) {
    return
  }
  patchSelected({ [key]: parsed } as Partial<VideoTimelineItemState>, operationType)
}

function updateTransition(value: unknown) {
  updateMetadataField('transition', String(value || 'none'))
}

function updateMetadataField(key: string, value: unknown) {
  patchSelected({
    metadata: {
      ...(selectedItem.value?.metadata || {}),
      [key]: value,
    },
  })
}

function updateSubtitleStyle(key: string, value: unknown) {
  const metadata = selectedItem.value?.metadata || {}
  const style
    = metadata.subtitle_style && typeof metadata.subtitle_style === 'object'
      ? (metadata.subtitle_style as Record<string, unknown>)
      : {}
  updateMetadataField('subtitle_style', { ...style, [key]: value })
}

function updateSubtitleOutputMode(value: unknown) {
  const track = findTrack(timeline.value, 'subtitle')
  if (!track?.items.length || track.locked) {
    return
  }
  const mode = value === 'soft' ? 'soft' : 'burn'
  const next = cloneTimeline(timeline.value)
  const target = findTrack(next, 'subtitle')!
  const operations = target.items.map((item) => {
    item.metadata = { ...item.metadata, subtitle_mode: mode }
    return {
      op_id: operationId(),
      type: 'update' as const,
      track_id: target.id,
      item_id: item.id,
      changes: { metadata: item.metadata },
    }
  })
  if (commitTimeline(next, operations[0]!)) {
    queueOperations(operations.slice(1))
  }
}

function subtitleStyleValue(key: string, fallback: unknown) {
  const value = selectedItem.value?.metadata.subtitle_style
  return value && typeof value === 'object'
    ? ((value as Record<string, unknown>)[key] ?? fallback)
    : fallback
}

function updateTransform(key: string, value: unknown) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) {
    return
  }
  patchSelected({
    transform: {
      ...(selectedItem.value?.transform || {}),
      [key]: parsed,
    },
  })
}

function updateCanvas(patch: Partial<VideoTimelineState['canvas']>) {
  const next = cloneTimeline(timeline.value)
  next.canvas = { ...next.canvas, ...patch }
  commitTimeline(next, {
    op_id: operationId(),
    type: 'update_canvas',
    changes: patch,
  })
}

function applyCanvasPreset(value: string) {
  const presets: Record<string, VideoTimelineState['canvas']> = {
    '9:16': { width: 1080, height: 1920, fps: timeline.value.canvas.fps, aspect_ratio: '9:16' },
    '16:9': { width: 1920, height: 1080, fps: timeline.value.canvas.fps, aspect_ratio: '16:9' },
    '1:1': { width: 1080, height: 1080, fps: timeline.value.canvas.fps, aspect_ratio: '1:1' },
    '4:3': { width: 1440, height: 1080, fps: timeline.value.canvas.fps, aspect_ratio: '4:3' },
  }
  const preset = presets[value]
  if (preset) {
    updateCanvas(preset)
    liveMessage.value = `画布已切换为 ${value}（${preset.width} × ${preset.height}）`
  }
}

function adjustTimelineZoom(offset: number) {
  zoom.value = Math.min(2.5, Math.max(0.5, Math.round((zoom.value + offset) * 10) / 10))
  liveMessage.value = `时间轴缩放已调整为 ${Math.round(zoom.value * 100)}%`
}

function resetTimelineZoom() {
  zoom.value = 1
  liveMessage.value = '时间轴缩放已恢复为 100%'
}

function updatePreviewViewport() {
  const bounds = previewStageWrap.value?.getBoundingClientRect()
  if (!bounds) {
    return
  }
  previewViewport.width = bounds.width
  previewViewport.height = bounds.height
}

function observePreviewViewport() {
  updatePreviewViewport()
  if (typeof ResizeObserver === 'undefined' || !previewStageWrap.value) {
    return
  }
  previewResizeObserver?.disconnect()
  previewResizeObserver = new ResizeObserver(updatePreviewViewport)
  previewResizeObserver.observe(previewStageWrap.value)
}

function toggleTrack(track: VideoTimelineTrackState, key: 'muted' | 'locked') {
  const next = cloneTimeline(timeline.value)
  const target = findTrack(next, track.kind)
  if (!target) {
    return
  }
  target[key] = !target[key]
  commitTimeline(next, {
    op_id: operationId(),
    type: 'update_track',
    track_id: target.id,
    changes: { [key]: target[key] },
  })
}

function selectItem(item: VideoTimelineItemState) {
  selectedItemId.value = item.id
  pausePlayback()
  setPlayhead(item.start_seconds)
  void ensureItemPreview(item)
}

function reorderVideoItem(itemId: string, targetIndex: number) {
  const track = findTrack(timeline.value, 'video')
  if (!track || track.locked) {
    return
  }
  const currentIndex = track.items.findIndex(item => item.id === itemId)
  if (currentIndex < 0 || currentIndex === targetIndex) {
    return
  }
  const next = reorderItem(timeline.value, track.kind, itemId, targetIndex)
  commitTimeline(next, {
    op_id: operationId(),
    type: 'move',
    track_id: track.id,
    item_id: itemId,
    index: targetIndex,
    start_seconds: findItem(next, itemId)?.item.start_seconds,
  })
}

function trimTimelineItem(
  itemId: string,
  patch: Pick<VideoTimelineItemState, 'duration_seconds' | 'trim_in_seconds' | 'trim_out_seconds'>,
) {
  const located = findItem(timeline.value, itemId)
  if (!located || located.track.locked) {
    return
  }
  selectedItemId.value = itemId
  const knownDuration = previewVideoElements.get(itemId)?.duration
    || projectAssetDurationSeconds(projectAssetFor(located.item) || located.item)
  const next = updateItem(timeline.value, itemId, patch, knownDuration)
  const updated = findItem(next, itemId)?.item
  if (!updated || ['duration_seconds', 'trim_in_seconds', 'trim_out_seconds'].every(key =>
    Math.abs(Number(updated[key as keyof VideoTimelineItemState]) - Number(located.item[key as keyof VideoTimelineItemState])) <= 0.001)) {
    return
  }
  commitTimeline(next, {
    op_id: operationId(),
    type: 'trim',
    track_id: located.track.id,
    item_id: itemId,
    duration_seconds: updated.duration_seconds,
    trim_in_seconds: updated.trim_in_seconds,
    trim_out_seconds: updated.trim_out_seconds,
  })
}

function positionTimelineItem(itemId: string, startSeconds: number) {
  const located = findItem(timeline.value, itemId)
  if (!located || located.track.locked || located.track.kind === 'video') {
    return
  }
  const normalizedStart = Math.max(0, Number(startSeconds) || 0)
  if (Math.abs(normalizedStart - located.item.start_seconds) <= 0.001) {
    return
  }
  selectedItemId.value = itemId
  const next = updateItem(timeline.value, itemId, { start_seconds: normalizedStart })
  commitTimeline(next, {
    op_id: operationId(),
    type: 'update',
    track_id: located.track.id,
    item_id: itemId,
    changes: { start_seconds: normalizedStart },
  })
}

function resizeTimelineItem(
  itemId: string,
  patch: Pick<
    VideoTimelineItemState,
    'start_seconds' | 'duration_seconds' | 'trim_in_seconds' | 'trim_out_seconds' | 'speed'
  >,
) {
  const located = findItem(timeline.value, itemId)
  if (!located || located.track.locked || located.track.kind === 'video') {
    return
  }
  selectedItemId.value = itemId
  const next = updateItem(timeline.value, itemId, patch)
  const updated = findItem(next, itemId)?.item
  if (!updated) {
    return
  }
  commitTimeline(next, {
    op_id: operationId(),
    type: 'update',
    track_id: located.track.id,
    item_id: itemId,
    changes: {
      start_seconds: updated.start_seconds,
      duration_seconds: updated.duration_seconds,
      trim_in_seconds: updated.trim_in_seconds,
      trim_out_seconds: updated.trim_out_seconds,
      speed: updated.speed,
    },
  })
}

function updateSelectedVoiceSpeed(value: unknown) {
  if (!selectedItem.value || selectedTrack.value?.kind !== 'voice') {
    return
  }
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) {
    return
  }
  const nextSpeed = Math.max(0.1, Math.min(8, parsed))
  const sourceSpan = Math.max(0.1, selectedItem.value.duration_seconds * selectedItem.value.speed)
  patchSelected({
    speed: nextSpeed,
    duration_seconds: Math.max(0.1, sourceSpan / nextSpeed),
  })
}

function removeTimelineItem(itemId: string) {
  selectedItemId.value = itemId
  deleteSelected()
}

function moveTimelineItem(itemId: string, offset: number) {
  selectedItemId.value = itemId
  moveSelected(offset)
}

function activeItems(kind: VideoTrackKind) {
  const track = findTrack(timeline.value, kind)
  if (!track || track.muted) {
    return []
  }
  return track.items.filter(
    item =>
      item.enabled
      && playhead.value >= item.start_seconds
      && playhead.value < item.start_seconds + item.duration_seconds,
  )
}

function projectAssetFor(item: VideoTimelineItemState) {
  return project.value?.assets.find(asset => asset.id === item.project_asset_id)
}

function isVideoPreviewItem(item: VideoTimelineItemState) {
  const asset = projectAssetFor(item)
  if (asset) {
    const kind = projectAssetMediaKind(asset)
    if (kind === 'image' || kind === 'video') {
      return kind === 'video'
    }
  }
  return item.metadata.media_kind === 'video'
    || item.metadata.resource_kind === 'video'
    || String(item.metadata.mime_type || item.metadata.media_type || '').startsWith('video/')
}

function projectAssetUrl(asset: VideoProjectAsset) {
  return (
    proxiedContentUrl(selectProjectAssetPreviewUrl(asset))
    || proxiedContentUrl(
      asset.metadata.preview_url || asset.metadata.download_url || asset.metadata.source_url,
    )
  )
}

function projectAssetPrompt(asset: VideoProjectAsset) {
  const provenance = asset.metadata.provenance as Record<string, unknown> | undefined
  const request = provenance?.request as Record<string, unknown> | undefined
  return String(
    asset.metadata.prompt || asset.metadata.text || request?.prompt || request?.text || '',
  )
}

function resolutionProfileFromPixels(
  width: number,
  height: number,
): '480p' | '720p' | '1080p' | undefined {
  const shortEdge = Math.min(width, height)
  if (!Number.isFinite(shortEdge) || shortEdge <= 0) {
    return undefined
  }
  if (shortEdge >= 1080) {
    return '1080p'
  }
  if (shortEdge >= 720) {
    return '720p'
  }
  return '480p'
}

function projectAssetResolutionProfile(
  asset: VideoProjectAsset,
): '480p' | '720p' | '1080p' | undefined {
  const provenance = asset.metadata.provenance as Record<string, unknown> | undefined
  const request = provenance?.request as Record<string, unknown> | undefined
  const declared = String(asset.metadata.resolution || request?.resolution || '')
  if (['480p', '720p', '1080p'].includes(declared)) {
    return declared as '480p' | '720p' | '1080p'
  }
  const probe = asset.metadata.probe as Record<string, unknown> | undefined
  return resolutionProfileFromPixels(Number(probe?.width), Number(probe?.height))
}

function resolveExportVideoProfile(): '480p' | '720p' | '1080p' {
  const ranks = { '480p': 0, '720p': 1, '1080p': 2 } as const
  const canvasProfile
    = resolutionProfileFromPixels(
      Number(timeline.value.canvas.width),
      Number(timeline.value.canvas.height),
    ) || '720p'
  const profiles: Array<'480p' | '720p' | '1080p'> = [canvasProfile]
  for (const item of videoTrack.value?.items || []) {
    if (!item.enabled || !item.project_asset_id) {
      continue
    }
    const asset = project.value?.assets.find(candidate => candidate.id === item.project_asset_id)
    const profile = asset ? projectAssetResolutionProfile(asset) : undefined
    if (profile) {
      profiles.push(profile)
    }
  }
  return profiles.reduce(
    (lowest, profile) => (ranks[profile] < ranks[lowest] ? profile : lowest),
    canvasProfile,
  )
}

function projectAssetKind(asset: VideoProjectAsset) {
  const labels: Record<string, string> = {
    voice: '配音',
    music: '背景音效',
    sfx: '音效',
    subtitle: '字幕',
    video: '主画面',
    overlay: '叠加画面',
    logo: 'Logo',
    intro: '片头',
    outro: '片尾',
  }
  return labels[asset.role] || '工程素材'
}

function projectAssetMediaKind(asset: VideoProjectAsset) {
  return assetMediaKind(asset)
}

function projectAssetSourceLabel(asset: VideoProjectAsset) {
  if (asset.metadata.source === 'ai-generated') {
    return 'AI 生成'
  }
  if (asset.metadata.source === 'workflow-confirmed') {
    return '本任务已确认素材'
  }
  if (asset.source_type === 'library_item' || asset.library_item_id) {
    return '企业知识库'
  }
  return '本地上传'
}

function linkedEnterpriseItem(asset: VideoProjectAsset) {
  const sourceId = String(asset.library_item_id || asset.source_id || '')
  return enterpriseLibraryAssets.value.find(item => item.id === sourceId)
}

function projectAssetReferenceCount(asset: VideoProjectAsset) {
  return timeline.value.tracks.reduce(
    (count, track) =>
      count
      + track.items.filter(
        item =>
          item.project_asset_id === asset.id || item.metadata.source_project_asset_id === asset.id,
      ).length,
    0,
  )
}

function projectAssetVersion(asset: VideoProjectAsset) {
  return (
    linkedEnterpriseItem(asset)?.version
    || Number(asset.metadata.source_version || asset.metadata.version || 1)
  )
}

function libraryMediaKind(item: LibraryItem): 'image' | 'video' | 'audio' {
  if (
    item.resource_kind === 'logo'
    || item.resource_kind === 'image'
    || item.mime_type?.startsWith('image/')
  ) {
    return 'image'
  }
  if (item.resource_kind === 'audio' || item.mime_type?.startsWith('audio/')) {
    return 'audio'
  }
  return 'video'
}

function libraryItemRole(item: LibraryItem): VideoProjectAssetRole {
  if (item.resource_kind === 'logo') {
    return 'logo'
  }
  if (libraryMediaKind(item) === 'audio') {
    return 'voice'
  }
  return visualRole.value
}

function proxiedContentUrl(value: unknown) {
  return contentMediaUrl(value, contentApiBaseUrl)
}

function resolveItemUrl(item: VideoTimelineItemState) {
  const asset = projectAssetFor(item)
  return (
    mediaUrls[item.id]
    || (asset ? projectAssetUrl(asset) : '')
    || proxiedContentUrl(
      asset?.metadata.preview_url || asset?.metadata.download_url || asset?.metadata.source_url,
    )
    || proxiedContentUrl(
      item.metadata.preview_url
      || item.metadata.download_url
      || item.metadata.original_url
      || item.metadata.source_url,
    )
  )
}

async function ensureItemPreview(item: VideoTimelineItemState) {
  if (mediaUrls[item.id] || previewLoading[item.id]) {
    return
  }
  const asset = projectAssetFor(item)
  const assetUrl = asset ? selectProjectAssetPreviewUrl(asset) : ''
  if (asset?.stream_url && assetUrl === asset.stream_url) {
    return
  }
  const downloadableAssetUrl = assetUrl || asset?.original_url
  if (downloadableAssetUrl) {
    previewLoading[item.id] = true
    try {
      const { blob } = await contentApi.downloadVideoProjectAssetUrl(downloadableAssetUrl)
      const url = URL.createObjectURL(blob)
      ownedObjectUrls.add(url)
      mediaUrls[item.id] = url
    }
    catch (error) {
      liveMessage.value = `素材“${item.label}”预览加载失败：${messageOf(error)}`
    }
    finally {
      previewLoading[item.id] = false
    }
    return
  }
  const libraryItemId = String(
    item.library_item_id || asset?.library_item_id || item.metadata.library_item_id || '',
  )
  if (!libraryItemId) {
    return
  }
  previewLoading[item.id] = true
  try {
    const { blob } = await contentApi.downloadLibraryItem(libraryItemId)
    const url = URL.createObjectURL(blob)
    ownedObjectUrls.add(url)
    mediaUrls[item.id] = url
  }
  catch (error) {
    liveMessage.value = `素材“${item.label}”预览加载失败：${messageOf(error)}`
  }
  finally {
    previewLoading[item.id] = false
  }
}

async function hydrateVisibleMedia() {
  const first = currentVisualItem.value
  if (first) {
    await ensureItemPreview(first)
  }
}

function setPlayhead(value: number) {
  playbackRevision += 1
  playbackTimestamp = 0
  playhead.value = Math.max(0, Math.min(Number(value) || 0, Math.max(duration.value, 0)))
  void syncVideoPreview()
  void nextTick(syncAudioPreviews)
}

async function syncVideoPreview() {
  const layers = currentVisualLayers.value
  await Promise.all(layers.map(layer => ensureItemPreview(layer.item)))
  await nextTick()
  for (const [itemId, element] of previewVideoElements) {
    const layer = currentVisualLayers.value.find(candidate => candidate.item.id === itemId)
    if (!layer || !Number.isFinite(element.duration)) {
      previewPlayback.stop(element)
      continue
    }
    const item = layer.item
    const timing = videoPreviewTiming(item, playhead.value, element.duration, timeline.value.canvas.fps)
    const keepAudio = !['mute', 'discard'].includes(String(item.metadata.source_audio || 'keep'))
    previewGain.apply(element, keepAudio && !timing.hold ? item.volume : 0)
    previewPlayback.sync(element, {
      sourceTime: timing.sourceTime,
      speed: Math.max(0.1, item.speed || 1),
      playing: playing.value && !timing.hold,
      revision: playbackRevision,
    })
  }
}

function setPreviewVideoElement(itemId: string, value: unknown) {
  const previous = previewVideoElements.get(itemId)
  if (previous && previous !== value) {
    previous.pause()
    previewGain.release(previous)
  }
  if (value instanceof HTMLVideoElement) {
    previewVideoElements.set(itemId, value)
  }
  else {
    previewVideoElements.delete(itemId)
  }
}

function setPreviewAudioElement(itemId: string, value: unknown) {
  const previous = previewAudioElements.get(itemId)
  if (previous && previous !== value) {
    previous.pause()
    previewGain.release(previous)
  }
  if (value instanceof HTMLAudioElement) {
    previewAudioElements.set(itemId, value)
  }
  else {
    previewAudioElements.delete(itemId)
  }
}

function syncAudioPreviews() {
  const activeIds = new Set(currentAudio.value.map(item => item.id))
  for (const [itemId, element] of previewAudioElements) {
    if (!activeIds.has(itemId) || !playing.value) {
      previewPlayback.stop(element)
    }
  }
  for (const item of currentAudio.value) {
    const element = previewAudioElements.get(item.id)
    if (!element || !Number.isFinite(element.duration)) {
      continue
    }
    const previewState = audioPreviewState({
      playhead: playhead.value,
      clipStart: item.start_seconds,
      clipDuration: item.duration_seconds,
      trimIn: item.trim_in_seconds,
      trimOut: item.trim_out_seconds,
      speed: item.speed,
      volume: item.volume,
      sourceDuration: element.duration,
      loop: Boolean(item.metadata.loop),
      fadeIn: Number(item.metadata.fade_in_seconds) || 0,
      fadeOut: Number(item.metadata.fade_out_seconds) || 0,
    })
    previewGain.apply(element, previewState.volume)
    const sourceWindow = previewState.sourceEnd - previewState.sourceStart
    previewPlayback.sync(element, {
      sourceTime: previewState.sourceTime,
      speed: previewState.playbackRate,
      playing: playing.value && previewState.shouldPlay,
      revision: playbackRevision,
      cycle: item.metadata.loop && sourceWindow > 0
        ? Math.floor(Math.max(0, playhead.value - item.start_seconds) * previewState.playbackRate / sourceWindow)
        : 0,
    })
  }
}

function playbackTick(timestamp: number) {
  if (!playing.value) {
    return
  }
  if (!playbackTimestamp) {
    playbackTimestamp = timestamp
  }
  const elapsed = Math.max(0, (timestamp - playbackTimestamp) / 1000)
  playbackTimestamp = timestamp
  const next = playhead.value + elapsed
  if (next >= duration.value) {
    playhead.value = duration.value
    pausePlayback()
    return
  }
  playhead.value = next
  void syncVideoPreview()
  syncAudioPreviews()
  playbackFrame = requestAnimationFrame(playbackTick)
}

async function togglePlayback() {
  if (playing.value) {
    pausePlayback()
    return
  }
  if (!duration.value) {
    ElMessage.warning('请先导入画面或音频素材')
    return
  }
  if (playhead.value >= duration.value) {
    playhead.value = 0
  }
  playing.value = true
  // AudioContext.resume() is asynchronous. Wait for it before syncing media
  // elements; otherwise replacing an audio clip or changing timing can race
  // the first play() call and leave the preview silent until the next click.
  const resumed = await previewGain.resume()
  if (!playing.value) {
    return
  }
  if (!resumed) {
    ElMessage.warning('浏览器尚未允许音频播放，请再次点击播放按钮')
    playing.value = false
    return
  }
  playbackTimestamp = 0
  playbackFrame = requestAnimationFrame(playbackTick)
  void syncVideoPreview()
  syncAudioPreviews()
}

function pausePlayback() {
  playing.value = false
  playbackTimestamp = 0
  if (playbackFrame) {
    cancelAnimationFrame(playbackFrame)
    playbackFrame = 0
  }
  previewVideoElements.forEach(element => previewPlayback.stop(element))
  previewAudioElements.forEach(element => previewPlayback.stop(element))
}

function openFileDialog() {
  fileInput.value?.click()
}

function isSubtitleFile(file: File) {
  return /\.(?:srt|vtt|txt)$/i.test(file.name)
}

function inferMediaKind(file: File): 'image' | 'video' | 'audio' | undefined {
  if (file.type.startsWith('image/')) {
    return 'image'
  }
  if (file.type.startsWith('video/')) {
    return 'video'
  }
  if (file.type.startsWith('audio/')) {
    return 'audio'
  }
  return undefined
}

function trackKindForAssetRole(role: VideoProjectAssetRole): VideoTrackKind {
  if (role === 'logo') {
    return 'overlay'
  }
  if (role === 'intro' || role === 'outro') {
    return 'video'
  }
  return role
}

async function mediaDuration(file: File) {
  if (file.type.startsWith('image/')) {
    return 4
  }
  return new Promise<number>((resolve) => {
    const element = document.createElement(file.type.startsWith('audio/') ? 'audio' : 'video')
    const url = URL.createObjectURL(file)
    element.preload = 'metadata'
    element.onloadedmetadata = () => {
      const value = Number.isFinite(element.duration) ? element.duration : 4
      URL.revokeObjectURL(url)
      resolve(Math.max(0.1, value))
    }
    element.onerror = () => {
      URL.revokeObjectURL(url)
      resolve(4)
    }
    element.src = url
  })
}

async function uploadImportedFile(
  file: File,
  role: VideoProjectAssetRole,
  view: ImportedAssetView,
) {
  if (!project.value) {
    throw new Error('视频工程尚未创建，无法持久化素材')
  }
  const asset = await uploadProjectAsset({
    projectId: project.value.id,
    file,
    role,
    signal: view.controller?.signal,
    onSession: (session, checkpointKey) => {
      view.uploadId = session.id
      view.checkpointKey = checkpointKey
    },
    onProgress: (progress) => {
      view.progress = Math.round(progress.fraction * 100)
      view.message = progress.message
    },
  })
  project.value.assets = [...project.value.assets.filter(item => item.id !== asset.id), asset]
  view.projectAssetId = asset.id
  view.progress = 100
  return asset
}

type GeneratedSubtitleCue = SubtitleCue & {
  timing_source?: string
}

function subtitleSegmentsFromAsset(asset: VideoProjectAsset): GeneratedSubtitleCue[] {
  const segments = asset.metadata.segments
  if (!Array.isArray(segments)) {
    return []
  }
  return segments
    .map((segment): GeneratedSubtitleCue | undefined => {
      if (!segment || typeof segment !== 'object' || Array.isArray(segment)) {
        return undefined
      }
      const value = segment as Record<string, unknown>
      const start = Number(value.start_seconds)
      const end = Number(value.end_seconds)
      const text = String(value.text || '').trim()
      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || !text) {
        return undefined
      }
      return {
        start_seconds: start,
        end_seconds: end,
        text,
        timing_source: String(value.timing_source || ''),
      }
    })
    .filter((cue): cue is GeneratedSubtitleCue => Boolean(cue))
    .sort((left, right) => left.start_seconds - right.start_seconds)
}

async function loadProjectSubtitleCues(asset: VideoProjectAsset): Promise<GeneratedSubtitleCue[]> {
  const segments = subtitleSegmentsFromAsset(asset)
  if (segments.length) {
    return segments
  }
  const url = asset.original_url || selectProjectAssetPreviewUrl(asset)
  if (!url) {
    throw new Error('字幕素材没有可读取的原文件，请重新导入字幕')
  }
  const { blob, fileName } = await contentApi.downloadVideoProjectAssetUrl(url)
  const filename = [fileName, asset.metadata.filename, asset.metadata.source_name, asset.label]
    .map(value => String(value || ''))
    .find(value => /\.(?:srt|vtt|txt)$/iu.test(value))
  const extension = filename?.split('.').pop()?.toLowerCase() || 'srt'
  const cues = parseSubtitleText(await blob.text(), extension)
  if (!cues.length) {
    throw new Error('字幕文件中没有有效的时间码或文本行，请检查文件后重新导入')
  }
  return cues
}

function addSubtitleCuesToTimeline(
  cues: GeneratedSubtitleCue[],
  sourceAsset: VideoProjectAsset,
  announce = true,
  baseOffsetSeconds = 0,
) {
  if (!cues.length) {
    ElMessage.warning('字幕候选中没有可加入时间线的有效分段')
    return false
  }
  const currentTrack = findTrack(timeline.value, 'subtitle')
  if (!currentTrack || currentTrack.locked) {
    ElMessage.warning('字幕轨已锁定，请解锁后再加入字幕')
    return false
  }
  if (projectAssetReferenceCount(sourceAsset)) {
    ElMessage.warning('该字幕已经加入字幕轨')
    return false
  }
  const before = timeline.value
  const next = cloneTimeline(before)
  const track = findTrack(next, 'subtitle')
  if (!track) {
    ElMessage.error('字幕轨不存在，无法加入字幕')
    return false
  }
  const baseOffset = Math.max(0, baseOffsetSeconds)
  const addedIds: string[] = []
  for (const cue of cues) {
    const item = normalizeItem(
      {
        id: createLocalId('subtitle'),
        project_asset_id: null,
        label: cue.text.split('\n')[0]?.slice(0, 24) || '字幕',
        start_seconds: baseOffset + cue.start_seconds,
        duration_seconds: cue.end_seconds - cue.start_seconds,
        trim_in_seconds: 0,
        trim_out_seconds: 0,
        volume: 1,
        speed: 1,
        enabled: true,
        content: cue.text,
        transform: {},
        metadata: {
          source_name: sourceAsset.label,
          source_kind: 'subtitle',
          source_project_asset_id: sourceAsset.id,
          source_voice_asset_id: sourceAsset.metadata.source_project_asset_id || '',
          timing_source: cue.timing_source || 'imported',
          subtitle_mode: subtitleOutputMode.value,
        },
      },
      'subtitle',
      track.items.length,
    )
    track.items.push(item)
    addedIds.push(item.id)
  }
  if (timelineDuration(next) > MAX_PROJECT_DURATION_SECONDS) {
    ElMessage.error('字幕超过工程 10 分钟时长上限，未加入时间线')
    return false
  }
  past.value = [...past.value.slice(-49), cloneTimeline(before)]
  future.value = []
  timeline.value = next
  queueOperations(timelineTransitionOperations(before, next))
  selectedItemId.value = addedIds.at(-1) || ''
  liveMessage.value = `已将 ${cues.length} 条字幕加入字幕轨`
  if (announce) {
    ElMessage.success(liveMessage.value)
  }
  return true
}

async function importSubtitle(file: File, view: ImportedAssetView) {
  const asset = await uploadImportedFile(file, 'subtitle', view)
  const extension = file.name.split('.').pop()?.toLowerCase() || 'srt'
  const cues = parseSubtitleText(await file.text(), extension)
  if (!cues.length) {
    throw new Error('未识别到有效字幕时间码或文本行')
  }
  if (!addSubtitleCuesToTimeline(cues, asset)) {
    throw new Error('字幕未能加入时间线')
  }
  view.clipId = selectedItemId.value
  view.status = 'ready'
  view.message = `已导入 ${cues.length} 条字幕`
}

async function importMedia(
  file: File,
  kind: 'image' | 'video' | 'audio',
  view: ImportedAssetView,
  trackKind: VideoTrackKind,
  assetRole: VideoProjectAssetRole,
  targetIndex: number,
) {
  if (assetRole === 'overlay' || assetRole === 'logo') {
    throw new Error('叠加轨道暂不开放新增；请改选主画面、片头或片尾后重新导入素材')
  }
  const asset = await uploadImportedFile(file, assetRole, view)
  const clipId = createLocalId('clip')
  const objectUrl = URL.createObjectURL(file)
  ownedObjectUrls.add(objectUrl)
  mediaUrls[clipId] = objectUrl
  const webAvProbe
    = kind === 'video' ? await webAvAdapter.probe(file, timeline.value.canvas) : undefined
  const clipDuration = webAvProbe?.duration_seconds || (await mediaDuration(file))
  const start = trackKind === 'video' ? duration.value : playhead.value
  const clip: VideoTimelineItemState = {
    id: clipId,
    project_asset_id: asset.id,
    label: file.name,
    start_seconds: start,
    duration_seconds: clipDuration,
    trim_in_seconds: 0,
    trim_out_seconds: 0,
    volume: 1,
    speed: 1,
    enabled: true,
    content: null,
    transform: {},
    metadata: {
      resource_kind: kind,
      media_kind: kind,
      mime_type: file.type,
      download_url: selectProjectAssetPreviewUrl(asset),
      stream_url: asset.stream_url || '',
      original_url: asset.original_url || '',
      source_name: file.name,
      audio_role: kind === 'audio' ? trackKind : undefined,
      asset_role: assetRole,
      preview_adapter: webAvProbe?.adapter || 'html5',
      source_width: webAvProbe?.width,
      source_height: webAvProbe?.height,
    },
  }
  if (!addTimelineItem(trackKind, clip, targetIndex)) {
    throw new Error('素材超过工程 10 分钟时长上限，未加入时间线')
  }
  view.clipId = clipId
  view.status = 'ready'
  view.message = `已加入${timelineTrackName(trackKind, '轨道')}`
}

async function importFiles(files: File[]) {
  const batchAudioRole = 'voice' as const
  const batchVisualRole = visualRole.value
  const baseCounts = new Map(timeline.value.tracks.map(track => [track.kind, track.items.length]))
  const roleCounts = new Map<VideoTrackKind, number>()
  const entries = files.map(file => ({
    file,
    view: {
      id: createLocalId('import'),
      name: file.name,
      kind: isSubtitleFile(file) ? '字幕' : inferMediaKind(file) || '未知',
      status: 'uploading',
      message: isSubtitleFile(file) ? '正在解析字幕' : '正在上传并登记素材',
      progress: 0,
      controller: new AbortController(),
    } as ImportedAssetView,
    assetRole: isSubtitleFile(file)
      ? ('subtitle' as const)
      : inferMediaKind(file) === 'audio'
        ? batchAudioRole
        : batchVisualRole,
    trackKind: 'video' as VideoTrackKind,
    targetIndex: 0,
  }))
  entries.forEach((entry) => {
    entry.trackKind = trackKindForAssetRole(entry.assetRole)
    const offset = roleCounts.get(entry.trackKind) || 0
    const baseIndex = entry.assetRole === 'intro' ? 0 : baseCounts.get(entry.trackKind) || 0
    entry.targetIndex = baseIndex + offset
    roleCounts.set(entry.trackKind, offset + 1)
  })
  importedAssets.value = [...entries.map(entry => entry.view), ...importedAssets.value]
  try {
    await ensureTaskAndProject()
  }
  catch (error) {
    const reason = `视频工程创建失败：${messageOf(error)}`
    entries.forEach(({ view }) => {
      view.status = 'error'
      view.message = reason
    })
    saveState.value = 'error'
    saveMessage.value = reason
    return
  }
  await Promise.all(
    entries.map(async (entry) => {
      const { file, view } = entry
      try {
        if (isSubtitleFile(file)) {
          await importSubtitle(file, view)
        }
        else {
          const kind = inferMediaKind(file)
          if (!kind) {
            throw new Error('仅支持图片、视频、音频、SRT、VTT 或 TXT')
          }
          await importMedia(file, kind, view, entry.trackKind, entry.assetRole, entry.targetIndex)
        }
      }
      catch (error) {
        if (view.controller?.signal.aborted) {
          view.status = 'cancelled'
          view.message = '上传已取消；重新选择同一文件可重新开始'
        }
        else {
          view.status = 'error'
          view.message = messageOf(error)
        }
      }
    }),
  )
}

function validateAiBrief() {
  const missingBrief: string[] = []
  if (!taskForm.name.trim()) {
    missingBrief.push('任务名称')
  }
  if (!taskForm.content_type.trim()) {
    missingBrief.push('内容类型')
  }
  if (!taskForm.style.trim()) {
    missingBrief.push('表达风格')
  }
  if (!taskForm.audience.trim()) {
    missingBrief.push('目标人群')
  }
  if (!taskForm.topic.trim() && !taskForm.product.trim()) {
    missingBrief.push('主题或产品')
  }
  if (missingBrief.length) {
    ElMessage.warning(`首次使用 AI 前请补齐：${missingBrief.join('、')}`)
    return false
  }
  return true
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (files.length) {
    void importFiles(files)
  }
}

function onDropFiles(event: DragEvent) {
  event.preventDefault()
  const files = Array.from(event.dataTransfer?.files || [])
  if (files.length) {
    void importFiles(files)
  }
}

async function cancelImportedAsset(asset: ImportedAssetView) {
  asset.controller?.abort()
  asset.status = 'cancelled'
  asset.message = '正在取消上传会话'
  if (!asset.uploadId) {
    asset.message = '上传已取消'
    return
  }
  try {
    await cancelProjectAssetUpload(asset.uploadId, asset.checkpointKey)
    asset.message = '上传会话已取消，服务端分片已清理'
  }
  catch (error) {
    asset.status = 'error'
    asset.message = `取消失败：${messageOf(error)}`
  }
}

function importedAssetSelected(asset: ImportedAssetView) {
  if (!asset.clipId) {
    return
  }
  const item = findItem(timeline.value, asset.clipId)?.item
  if (item) {
    selectItem(item)
  }
}

async function generateMusic() {
  if (musicGenerating.value) {
    return
  }
  const prompt = musicPrompt.value.trim()
  if (!prompt) {
    ElMessage.warning('请先填写背景音乐提示词')
    return
  }
  if (!validateAiBrief()) {
    return
  }
  musicGenerating.value = true
  try {
    if (!(await manualSave()) || !project.value) {
      ElMessage.error('当前改动尚未成功保存，不能生成背景音乐')
      return
    }
    const requestedDuration = Number(musicDuration.value)
    if (!Number.isInteger(requestedDuration) || requestedDuration < musicDurationRange.value.min || requestedDuration > musicDurationRange.value.max) {
      ElMessage.warning(`背景音乐时长请填写 ${musicDurationRange.value.min} 到 ${musicDurationRange.value.max} 秒之间的整数。`)
      return
    }
    const asset = await contentApi.generateVideoProjectMusic(project.value.id, {
      expected_revision: project.value.current_revision,
      prompt,
      duration_seconds: requestedDuration,
      audio_format: musicFormat.value,
      label: `AI 背景音乐 · ${prompt.slice(0, 32)}`,
    })
    registerGeneratedAssets([asset])
    ElMessage.success('AI 背景音乐候选已生成，可在右侧加入时间线')
  }
  catch (error) {
    ElMessage.error(`AI 背景音乐生成失败：${messageOf(error)}`)
  }
  finally {
    musicGenerating.value = false
  }
}

function revisionSourceLabel(source: string) {
  const labels: Record<string, string> = {
    create: '工程创建',
    edit: '人工编辑',
    ai: 'AI 修改',
    restore: '版本恢复',
  }
  return labels[source] || source || '未知来源'
}

async function restoreRevision(revision: VideoProjectRevision) {
  if (!project.value || revision.id === project.value.current_revision_id) {
    return
  }
  if (!(await manualSave()) || !project.value) {
    ElMessage.error('当前改动保存失败，不能执行版本恢复')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将以版本 ${revision.revision ?? '—'} 的时间线创建一个新的恢复版本；当前版本仍会保留。`,
      '确认恢复历史版本',
      {
        confirmButtonText: '创建恢复版本',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  }
  catch {
    return
  }
  restoringRevisionId.value = revision.id
  try {
    const updated = await contentApi.editVideoProject(project.value.id, {
      expected_revision: project.value.current_revision,
      operations: [
        {
          op_id: operationId(),
          type: 'restore',
          revision_id: revision.id,
        },
      ],
    })
    applyProject(updated, true, true)
    saveState.value = 'saved'
    saveMessage.value = `已从版本 ${revision.revision ?? '—'} 创建恢复版本 ${updated.current_revision}`
    liveMessage.value = saveMessage.value
    try {
      revisions.value = await contentApi.listVideoProjectRevisions(updated.id)
    }
    catch (error) {
      ElMessage.warning(`恢复已完成，但版本列表刷新失败：${messageOf(error)}`)
    }
    ElMessage.success(saveMessage.value)
  }
  catch (error) {
    ElMessage.error(`版本恢复失败：${messageOf(error)}`)
  }
  finally {
    restoringRevisionId.value = ''
  }
}

function isTerminalJob(job: MediaJobResponse) {
  return isTerminalMediaJob(job)
}

function mediaJobStatusLabel(status: MediaJobResponse['status']) {
  return (
    {
      queued: '排队中',
      running: '处理中',
      succeeded: '已完成',
      failed: '失败',
      cancelled: '已取消',
    } as Record<MediaJobResponse['status'], string>
  )[status]
}

function mediaJobKindLabel(kind: string) {
  return (
    (
      {
        video_project_render: '精确预览',
        video_project_export: '成片视频',
        video_project_ai_generation: 'AI 视频',
      } as Record<string, string>
    )[kind] || kind
  )
}

function replaceExactPreviewUrl(url: string) {
  if (exactPreviewUrl.value) {
    URL.revokeObjectURL(exactPreviewUrl.value)
    ownedObjectUrls.delete(exactPreviewUrl.value)
  }
  exactPreviewUrl.value = url
  if (url) {
    ownedObjectUrls.add(url)
  }
}

async function prepareExactPreview(job: MediaJobResponse) {
  if (job.status !== 'succeeded' || exactPreviewUrl.value) {
    return
  }
  exactPreviewBusy.value = true
  try {
    const { blob } = await contentApi.downloadJob(job.id)
    if (editorDisposed || exactPreviewJob.value?.id !== job.id) {
      return
    }
    replaceExactPreviewUrl(URL.createObjectURL(blob))
    liveMessage.value = '精确预览已由服务端渲染完成'
  }
  catch (error) {
    if (!editorDisposed && exactPreviewJob.value?.id === job.id) {
      ElMessage.error(`精确预览文件加载失败：${messageOf(error)}`)
    }
  }
  finally {
    if (exactPreviewJob.value?.id === job.id) {
      exactPreviewBusy.value = false
    }
  }
}

async function refreshProjectAssets() {
  if (!project.value) {
    return
  }
  const latest = await contentApi.getVideoProject(project.value.id)
  project.value = {
    ...project.value,
    assets: latest.assets,
    updated_at: latest.updated_at,
  }
}

async function loadEnterpriseLibrary() {
  enterpriseLibraryLoading.value = true
  enterpriseLibraryError.value = ''
  try {
    enterpriseLibraryAssets.value = await contentApi.listLibrary()
  }
  catch (error) {
    enterpriseLibraryError.value = `企业知识库暂不可用：${messageOf(error)}`
    throw error
  }
  finally {
    enterpriseLibraryLoading.value = false
  }
}

async function refreshAssetLibrary() {
  assetLibraryRefreshing.value = true
  try {
    await Promise.all([refreshProjectAssets(), loadEnterpriseLibrary()])
    liveMessage.value = `企业知识库已刷新，共 ${enterpriseMediaAssets.value.length} 项可用媒体素材`
    ElMessage.success(liveMessage.value)
  }
  catch (error) {
    ElMessage.error(`企业知识库刷新失败：${messageOf(error)}`)
  }
  finally {
    assetLibraryRefreshing.value = false
  }
}

async function acceptMediaJobUpdate(job: MediaJobResponse) {
  if (exactPreviewJob.value?.id === job.id) {
    exactPreviewJob.value = job
    if (isTerminalJob(job) && job.status !== 'succeeded') {
      exactPreviewBusy.value = false
    }
    if (job.status === 'succeeded') {
      await prepareExactPreview(job)
    }
  }
  if (exportJob.value?.id === job.id) {
    exportJob.value = job
    if (isTerminalJob(job)) {
      exporting.value = false
    }
    if (job.status === 'succeeded') {
      const downloaded = await downloadMediaJob(job)
      if (downloaded) {
        liveMessage.value = '成片视频已导出并开始下载'
        ElMessage.success(liveMessage.value)
      }
    }
  }
  if (aiVideoJob.value?.id === job.id) {
    aiVideoJob.value = job
    if (isTerminalJob(job)) {
      videoGenerating.value = false
    }
    if (job.status === 'failed' && notifiedAiVideoFailureId.value !== job.id) {
      notifiedAiVideoFailureId.value = job.id
      ElMessage.error(formatContentGenerationError(job, 'AI 视频生成失败，服务未返回具体原因，请稍后重试'))
    }
    else if (job.status !== 'failed') {
      notifiedAiVideoFailureId.value = ''
    }
    if (job.status === 'succeeded') {
      const submissionSequence = aiVideoSubmissionSequence
      try {
        await refreshProjectAssets()
        if (editorDisposed || aiVideoJob.value?.id !== job.id || submissionSequence !== aiVideoSubmissionSequence) {
          return
        }
        liveMessage.value = 'AI 视频已生成，结果已显示在生成进度下方，可预览、下载或加入时间线'
        ElMessage.success(liveMessage.value)
      }
      catch (error) {
        if (!editorDisposed && aiVideoJob.value?.id === job.id && submissionSequence === aiVideoSubmissionSequence) {
          ElMessage.warning(`AI 视频已完成，但候选结果刷新失败：${messageOf(error)}`)
        }
      }
    }
  }
}

function mediaJobBelongsToProject(job: MediaJobResponse, projectId: string) {
  return String(job.result?.project_id || '').trim() === projectId
}

function isFinalWorkflowRender(job: MediaJobResponse, value: VideoProjectResponse) {
  return (
    job.kind === 'video_project_render'
    && String(job.result?.project_id || '') === value.id
    && String(job.result?.revision_id || '') === value.current_revision_id
    && String(job.result?.profile || '').startsWith('final-')
    && (!isTerminalJob(job) || Number(job.result?.timeline_composition_version || 0) >= 2)
  )
}

async function continuePersistedWorkflowRender(
  initial: MediaJobResponse,
  baseline: WorkflowStageBaseline,
) {
  workflowCompletionJob.value = initial
  workflowFinalizing.value = !isTerminalJob(initial)
  try {
    const completed = isTerminalJob(initial) ? initial : await waitForWorkflowRender(initial)
    if (!completed || editorDisposed) {
      return
    }
    if (!project.value || !isFinalWorkflowRender(completed, project.value)
      || pendingOperations.value.length || taskDirty.value) {
      throw new Error('时间线已有新修改，请再次点击“完成并进入确认导出”，生成最新的完整视频')
    }
    workflowCompletionJob.value = completed
    if (completed.status !== 'succeeded') {
      throw new ContentGenerationError(completed, '最终视频渲染失败，服务未返回具体原因')
    }
    const artifactId = String(completed.result.artifact_id || '').trim()
    if (!artifactId) {
      throw new Error('最终视频已生成，但没有返回可确认的内容版本')
    }
    liveMessage.value = '最终视频已生成，正在确认工作流阶段'
    emit('workflowCompleted', { artifactId, baseline })
  }
  catch (error) {
    if (!editorDisposed) {
      liveMessage.value = `最终视频生成失败：${messageOf(error)}`
      ElMessage.error(liveMessage.value)
    }
  }
  finally {
    if (!editorDisposed) {
      workflowFinalizing.value = false
    }
  }
}

async function restorePersistedVideoJobs(value: VideoProjectResponse) {
  const submissionSequence = aiVideoSubmissionSequence
  const previousAiJobId = aiVideoJob.value?.id
  const jobs = (await contentApi.listVideoProjectJobs(value.id)).filter(job =>
    mediaJobBelongsToProject(job, value.id),
  )

  const latestAiVideo = jobs.find(job => job.kind === 'video_project_ai_generation')
  if (latestAiVideo && !editorDisposed && !aiVideoSubmitting.value
    && project.value?.id === value.id && submissionSequence === aiVideoSubmissionSequence
    && aiVideoJob.value?.id === previousAiJobId) {
    aiVideoJob.value = latestAiVideo
    videoGenerating.value = !isTerminalJob(latestAiVideo)
    if (latestAiVideo.status === 'succeeded') {
      // The freshly loaded project can race the worker's final commit by a few
      // milliseconds.  Refresh once so the persisted candidate is visible.
      await refreshProjectAssets()
      if (!editorDisposed && aiVideoJob.value?.id === latestAiVideo.id && submissionSequence === aiVideoSubmissionSequence) {
        liveMessage.value = '已恢复上次生成的视频结果'
      }
    }
    else if (!isTerminalJob(latestAiVideo)) {
      liveMessage.value = '已恢复后台视频生成任务，离开页面不会中断生成'
      scheduleJobPoll(latestAiVideo.id)
    }
  }

  if (!props.workflowMode) {
    return
  }
  const videoStage = taskWorkflow.value?.stages.find(stage => stage.key === 'video_preview')
  if (!videoStage || ['stale', 'failed'].includes(videoStage.state)) {
    return
  }
  const finalRender = jobs.find(job => isFinalWorkflowRender(job, value))
  if (!finalRender || ['failed', 'cancelled'].includes(finalRender.status)) {
    return
  }
  const confirmedIds = new Set(Object.values(videoStage.confirmed_artifact_ids ?? {}).flat())
  const artifactId = String(finalRender.result.artifact_id || '').trim()
  if (videoStage.state === 'confirmed' && artifactId && confirmedIds.has(artifactId)) {
    return
  }
  void continuePersistedWorkflowRender(finalRender, workflowStageBaseline(videoStage))
}

function stopJobPolling(jobId: string) {
  const timer = jobPollTimers.get(jobId)
  if (timer) {
    clearTimeout(timer)
    jobPollTimers.delete(jobId)
  }
}

function scheduleJobPoll(jobId: string) {
  if (editorDisposed) {
    return
  }
  stopJobPolling(jobId)
  jobPollTimers.set(
    jobId,
    setTimeout(async () => {
      if (editorDisposed) {
        return
      }
      jobPollTimers.delete(jobId)
      try {
        const latest = await contentApi.getJob(jobId)
        await acceptMediaJobUpdate(latest)
        if (!isTerminalJob(latest)) {
          scheduleJobPoll(jobId)
        }
      }
      catch (error) {
        liveMessage.value = `任务状态刷新失败：${messageOf(error)}`
        if (!editorDisposed) {
          scheduleJobPoll(jobId)
        }
      }
    }, 1500),
  )
}

async function trackMediaJob(job: MediaJobResponse) {
  await acceptMediaJobUpdate(job)
  if (!isTerminalJob(job)) {
    scheduleJobPoll(job.id)
  }
}

function finalRenderProfile(): 'final-480p' | 'final-720p' | 'final-1080p' {
  if (exportVideoProfile.value === '1080p') {
    return 'final-1080p'
  }
  return exportVideoProfile.value === '480p' ? 'final-480p' : 'final-720p'
}

async function waitForWorkflowRender(initial: MediaJobResponse) {
  let latest = initial
  workflowCompletionJob.value = latest
  for (let attempt = 0; attempt < 400 && !isTerminalJob(latest); attempt += 1) {
    await new Promise<void>(resolve => setTimeout(resolve, 1500))
    if (editorDisposed) {
      // Polling belongs to this mounted view only. The durable server job keeps
      // running and initialize() will reclaim it when the user comes back.
      return undefined
    }
    latest = await contentApi.getJob(latest.id)
    workflowCompletionJob.value = latest
  }
  if (!isTerminalJob(latest)) {
    throw new Error('视频渲染等待超时，请稍后从本阶段重试')
  }
  return latest
}

async function completeWorkflowVideo() {
  if (!props.workflowMode || workflowFinalizing.value) {
    return
  }
  if (!videoTrack.value?.items.some(item => item.enabled)) {
    ElMessage.warning('请先将至少一个图片或视频素材加入主画面轨道')
    return
  }
  workflowFinalizing.value = true
  pausePlayback()
  try {
    if (!(await manualSave()) || !project.value || pendingOperations.value.length || taskDirty.value) {
      throw new Error('工程未成功保存，未进入导出阶段，请保存后重试')
    }
    const latestWorkflow = await contentApi.getWorkflow(resolvedTaskId.value)
    taskWorkflow.value = latestWorkflow
    const videoStage = latestWorkflow.stages.find(stage => stage.key === 'video_preview')
    if (!videoStage) {
      throw new Error('视频生成阶段不存在，请刷新任务后重试')
    }
    const baseline = workflowStageBaseline(videoStage)
    const revision = project.value.current_revision
    const profile = finalRenderProfile()
    const fps = previewFps(Number(timeline.value.canvas.fps))
    const subtitleMode = subtitleOutputMode.value
    const includeAudio = true
    let job = await contentApi.createVideoProjectRender(project.value.id, {
      expected_revision: revision,
      profile,
      fps,
      subtitle_mode: subtitleMode,
      include_audio: includeAudio,
      idempotency_key: [
        'workflow-final-v2',
        resolvedTaskId.value,
        project.value.id,
        revision,
        profile,
        fps,
        subtitleMode,
        includeAudio ? 'audio' : 'silent',
      ].join(':'),
    })
    if (job.status === 'failed' || job.status === 'cancelled') {
      job = await contentApi.retryJob(job.id)
    }
    ElMessage.success('正在生成最终视频，完成后将自动进入导出阶段')
    const completed = await waitForWorkflowRender(job)
    if (!completed || editorDisposed) {
      return
    }
    if (completed.status !== 'succeeded') {
      throw new ContentGenerationError(completed, '最终视频渲染失败，服务未返回具体原因')
    }
    if (!project.value || !isFinalWorkflowRender(completed, project.value)
      || pendingOperations.value.length || taskDirty.value) {
      throw new Error('时间线已有新修改，请再次点击“完成并进入确认导出”，生成最新的完整视频')
    }
    const artifactId = String(completed.result.artifact_id || '').trim()
    if (!artifactId) {
      throw new Error('最终视频已生成，但没有返回可确认的内容版本')
    }
    liveMessage.value = '最终视频已生成，正在确认工作流阶段'
    emit('workflowCompleted', { artifactId, baseline })
  }
  catch (error) {
    if (!editorDisposed) {
      liveMessage.value = `最终视频生成失败：${messageOf(error)}`
      ElMessage.error(liveMessage.value)
    }
  }
  finally {
    workflowFinalizing.value = false
  }
}

function openExportDrawer() {
  exportDrawerOpen.value = true
}

async function submitProjectExport() {
  if (exporting.value || !validateAiBrief()) {
    return
  }
  exporting.value = true
  try {
    if (!(await manualSave()) || !project.value || pendingOperations.value.length || taskDirty.value) {
      throw new Error('工程未成功保存，已停止导出，请保存后重试')
    }
    const job = await contentApi.createVideoProjectExport(project.value.id, {
      expected_revision: project.value.current_revision,
      deliverables: ['mixed_video'],
      video_profile: exportVideoProfile.value,
      fps: previewFps(Number(timeline.value.canvas.fps)),
      subtitle_mode: subtitleOutputMode.value,
      audio_format: 'mp3',
      subtitle_format: 'srt',
      clip_item_ids: [],
      project_track_kinds: [],
      project_asset_ids: null,
      include_original_assets: false,
      idempotency_key: createLocalId('export'),
    })
    exportJob.value = job
    ElMessage.success('成片视频正在导出，完成后会自动下载')
    await trackMediaJob(job)
  }
  catch (error) {
    exporting.value = false
    ElMessage.error(`导出失败：${messageOf(error)}`)
  }
}

async function downloadMediaJob(job: MediaJobResponse) {
  if (job.status !== 'succeeded') {
    ElMessage.warning('任务完成后才能下载')
    return false
  }
  try {
    const { blob, fileName } = await contentApi.downloadJob(job.id)
    saveBlob(blob, fileName || `${mediaJobKindLabel(job.kind)}-${job.id}`)
    return true
  }
  catch (error) {
    ElMessage.error(`下载失败：${messageOf(error)}`)
    return false
  }
}

async function cancelMediaJob(job: MediaJobResponse) {
  try {
    const updated = await contentApi.cancelJob(job.id)
    stopJobPolling(job.id)
    await acceptMediaJobUpdate(updated)
  }
  catch (error) {
    ElMessage.error(`取消任务失败：${messageOf(error)}`)
  }
}

async function retryMediaJob(job: MediaJobResponse) {
  try {
    const retried = await contentApi.retryJob(job.id)
    if (exactPreviewJob.value?.id === job.id) {
      replaceExactPreviewUrl('')
      exactPreviewJob.value = retried
      exactPreviewBusy.value = true
    }
    if (exportJob.value?.id === job.id) {
      exportJob.value = retried
      exporting.value = true
    }
    if (aiVideoJob.value?.id === job.id) {
      aiVideoJob.value = retried
      videoGenerating.value = true
    }
    await trackMediaJob(retried)
  }
  catch (error) {
    ElMessage.error(`重试任务失败：${messageOf(error)}`)
  }
}

function registerGeneratedAssets(assets: VideoProjectAsset[]) {
  if (!project.value || !assets.length) {
    return
  }
  const assetIds = new Set(assets.map(asset => asset.id))
  project.value.assets = [
    ...assets,
    ...project.value.assets.filter(asset => !assetIds.has(asset.id)),
  ]
}

function timelineTrackForProjectAsset(asset: VideoProjectAsset): VideoTrackKind {
  if (
    asset.role === 'voice'
    || asset.role === 'music'
    || asset.role === 'sfx'
    || asset.role === 'subtitle'
  ) {
    return asset.role
  }
  if (asset.role === 'overlay' || asset.role === 'logo') {
    return 'overlay'
  }
  return 'video'
}

function latestLinkedSubtitleAsset(voiceAssetId: string) {
  return (project.value?.assets || [])
    .filter(
      asset =>
        asset.active
        && asset.role === 'subtitle'
        && String(asset.metadata.source_project_asset_id || '') === voiceAssetId
        && subtitleSegmentsFromAsset(asset).length > 0,
    )
    .slice()
    .sort((left, right) => compareContentDateTimeDescending(left.created_at, right.created_at))[0]
}

function subtitleTimelineOffset(sourceVoiceAssetId: string) {
  if (!sourceVoiceAssetId) {
    return playhead.value
  }
  const sourceVoiceItem = findTrack(timeline.value, 'voice')?.items.find(
    item => item.enabled && item.project_asset_id === sourceVoiceAssetId,
  )
  return sourceVoiceItem?.start_seconds ?? playhead.value
}

function projectAssetAddDisabled(asset: VideoProjectAsset) {
  return (
    timelineTrackForProjectAsset(asset) === 'overlay'
    || (asset.role === 'subtitle' && projectAssetReferenceCount(asset) > 0)
  )
}

function projectAssetAddDisabledReason(asset: VideoProjectAsset) {
  if (timelineTrackForProjectAsset(asset) === 'overlay') {
    return '叠加轨道暂不开放新增；工程中已有叠加内容仍会保留并正常导出'
  }
  if (asset.role === 'subtitle' && projectAssetReferenceCount(asset) > 0) {
    return '该字幕已经加入字幕轨'
  }
  return ''
}

async function addProjectAssetToTimeline(
  asset: VideoProjectAsset,
  options: { silent?: boolean } = {},
): Promise<boolean> {
  if (projectAssetAddDisabled(asset)) {
    if (!options.silent) {
      ElMessage.warning(projectAssetAddDisabledReason(asset))
    }
    return false
  }
  const trackKind = timelineTrackForProjectAsset(asset)
  const track = findTrack(timeline.value, trackKind)
  const mediaKind = projectAssetMediaKind(asset)
  if (!track || track.locked) {
    const trackName = timelineTrackName(trackKind)
    if (!options.silent) {
      ElMessage.warning(`${trackName}轨已锁定，请解锁后再加入素材`)
    }
    return false
  }
  if (trackKind === 'subtitle') {
    const projectId = project.value?.id
    const baseOffset = subtitleTimelineOffset(String(asset.metadata.source_project_asset_id || ''))
    try {
      const segments = await loadProjectSubtitleCues(asset)
      if (
        editorDisposed
        || workflowFinalizing.value
        || project.value?.id !== projectId
        || !project.value?.assets.some(current => current.id === asset.id && current.active)
      ) {
        return false
      }
      const added = addSubtitleCuesToTimeline(
        segments,
        asset,
        !options.silent,
        baseOffset,
      )
      if (added && !options.silent) {
        void manualSave()
      }
      return added
    }
    catch (error) {
      ElMessage.error(`字幕加入失败：${messageOf(error)}`)
      return false
    }
  }
  const linkedSubtitle = trackKind === 'voice' ? latestLinkedSubtitleAsset(asset.id) : undefined
  if (
    linkedSubtitle
    && !projectAssetReferenceCount(linkedSubtitle)
    && findTrack(timeline.value, 'subtitle')?.locked
  ) {
    if (!options.silent) {
      ElMessage.warning('该配音带有配套字幕，请先解锁字幕轨再一起加入')
    }
    return false
  }
  const metadataDuration = projectAssetDurationSeconds(asset)
  const item: VideoTimelineItemState = {
    id: createLocalId('asset'),
    project_asset_id: asset.id,
    label: asset.label,
    start_seconds: trackKind === 'video' ? duration.value : playhead.value,
    duration_seconds: metadataDuration ?? (mediaKind === 'image' ? 4 : 5),
    trim_in_seconds: 0,
    trim_out_seconds: 0,
    volume: trackKind === 'music' ? 0.25 : 1,
    speed: 1,
    enabled: true,
    content: null,
    transform: trackKind === 'overlay' ? { x: 50, y: 15, scale: 1, opacity: 1 } : {},
    metadata: {
      ...asset.metadata,
      media_kind: mediaKind,
      resource_kind: mediaKind,
      asset_role: asset.role,
      stream_url: asset.stream_url || '',
      original_url: asset.original_url || '',
    },
  }
  const targetIndex = asset.role === 'intro' ? 0 : track?.items.length
  if (!addTimelineItem(trackKind, item, targetIndex)) {
    return false
  }
  if (trackKind === 'voice') {
    if (
      linkedSubtitle
      && !projectAssetReferenceCount(linkedSubtitle)
      && addSubtitleCuesToTimeline(
        subtitleSegmentsFromAsset(linkedSubtitle),
        linkedSubtitle,
        false,
        item.start_seconds,
      )
    ) {
      liveMessage.value = `“${asset.label}”与配套字幕已同步加入轨道`
      if (!options.silent) {
        ElMessage.success(liveMessage.value)
      }
      if (!options.silent) {
        void manualSave()
      }
      return true
    }
  }
  liveMessage.value = `“${asset.label}”已加入${timelineTrackName(trackKind)}轨道`
  if (!options.silent) {
    ElMessage.success(liveMessage.value)
  }
  return true
}

async function importAllWorkflowProjectAssets() {
  const candidates = workflowProjectAssetsToImport.value
  if (!candidates.length || workflowBulkImporting.value) {
    return
  }
  workflowBulkImporting.value = true
  try {
    let added = 0
    let skipped = 0
    for (const asset of candidates) {
      if (projectAssetAddDisabled(asset)) {
        skipped += 1
        continue
      }
      if (await addProjectAssetToTimeline(asset, { silent: true })) {
        added += 1
      }
      else {
        skipped += 1
      }
    }
    if (!added) {
      ElMessage.warning(
        skipped
          ? '没有可导入的前序素材，请检查主画面轨道是否已锁定'
          : '前序确认图片已经全部加入时间线',
      )
      return
    }
    if (!(await manualSave())) {
      throw new Error('素材已加入时间线，但工程保存失败，请点击“重试保存”')
    }
    liveMessage.value = skipped
      ? `已全部导入 ${added} 项前序素材，另有 ${skipped} 项未加入`
      : `已全部导入 ${added} 项前序素材`
    ElMessage.success(liveMessage.value)
  }
  catch (error) {
    ElMessage.error(`批量导入失败：${messageOf(error)}`)
  }
  finally {
    workflowBulkImporting.value = false
  }
}

async function downloadProjectAsset(asset: VideoProjectAsset) {
  const url = asset.original_url || selectProjectAssetPreviewUrl(asset)
  if (!url) {
    ElMessage.warning('该素材暂无可下载文件')
    return
  }
  try {
    const { blob, fileName } = await contentApi.downloadVideoProjectAssetUrl(url)
    saveBlob(blob, fileName || asset.label)
  }
  catch (error) {
    ElMessage.error(`素材下载失败：${messageOf(error)}`)
  }
}

function addEnterpriseLibraryItem(item: LibraryItem) {
  const mediaKind = libraryMediaKind(item)
  const role = libraryItemRole(item)
  const trackKind = trackKindForAssetRole(role)
  if (trackKind === 'overlay') {
    ElMessage.warning('叠加轨道暂不开放新增；请导入主画面、片头或片尾素材')
    return false
  }
  const metadataDuration = mediaMetadataDurationSeconds(item.metadata)
  const clip: VideoTimelineItemState = {
    id: createLocalId('enterprise'),
    library_item_id: item.id,
    label: item.name,
    start_seconds: trackKind === 'video' ? duration.value : playhead.value,
    duration_seconds: metadataDuration ?? (mediaKind === 'image' ? 4 : 5),
    trim_in_seconds: 0,
    trim_out_seconds: 0,
    volume: trackKind === 'music' ? 0.25 : 1,
    speed: 1,
    enabled: true,
    content: null,
    transform: {},
    metadata: {
      ...(item.metadata || {}),
      source: 'enterprise-knowledge-base',
      source_version: item.version,
      media_kind: mediaKind,
      media_type: item.mime_type || '',
      library_item_id: item.id,
      resource_entry_id: item.resource_entry_id || '',
      download_url: item.download_url || '',
      source_url: item.source_url || '',
      asset_role: role,
    },
  }
  const targetIndex = role === 'intro' ? 0 : findTrack(timeline.value, trackKind)?.items.length
  return addTimelineItem(trackKind, clip, targetIndex)
}

function importEnterpriseSelection(ids: string[]) {
  const selected = ids
    .map(id => enterpriseMediaAssets.value.find(item => item.id === id))
    .filter((item): item is LibraryItem => Boolean(item))
  const added = selected.filter(addEnterpriseLibraryItem)
  enterpriseSelectionIds.value = []
  if (!added.length) {
    return
  }
  liveMessage.value = `已从企业知识库加入 ${added.length} 项素材，正在保存到当前工程`
  ElMessage.success(liveMessage.value)
}

async function renameProjectAsset(asset: VideoProjectAsset) {
  if (!project.value) {
    return
  }
  try {
    const { value } = await ElMessageBox.prompt('请输入新的素材名称', '重命名工程素材', {
      inputValue: asset.label,
      inputPattern: /\S/u,
      inputErrorMessage: '素材名称不能为空',
      confirmButtonText: '保存名称',
      cancelButtonText: '取消',
    })
    projectAssetActionBusyId.value = asset.id
    const updated = await contentApi.updateVideoProjectAsset(project.value.id, asset.id, {
      label: value.trim(),
    })
    registerGeneratedAssets([updated])
    ElMessage.success('素材名称已更新')
  }
  catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(`素材重命名失败：${messageOf(error)}`)
    }
  }
  finally {
    projectAssetActionBusyId.value = ''
  }
}

async function archiveProjectAsset(asset: VideoProjectAsset) {
  if (!project.value) {
    return
  }
  const referenceCount = projectAssetReferenceCount(asset)
  if (referenceCount) {
    ElMessage.warning(`该素材仍被时间线使用 ${referenceCount} 次，请先移出时间线`)
    return
  }
  try {
    await ElMessageBox.confirm(
      `归档“${asset.label}”后将从本工程的企业素材引用中隐藏，企业知识库原件不会被删除。`,
      '归档素材引用',
      { confirmButtonText: '归档', cancelButtonText: '取消', type: 'warning' },
    )
    projectAssetActionBusyId.value = asset.id
    const archived = await contentApi.archiveVideoProjectAsset(project.value.id, asset.id)
    registerGeneratedAssets([archived])
    purgeProjectAssetFromHistory(asset.id)
    ElMessage.success('已归档本工程中的素材引用，企业知识库原件保持不变')
  }
  catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(`素材归档失败：${messageOf(error)}`)
    }
  }
  finally {
    projectAssetActionBusyId.value = ''
  }
}

function projectAssetTimelineRemoval(before: VideoTimelineState, assetId: string) {
  const unlocked = cloneTimeline(before)
  const affectedTrackIds = new Set<string>()
  const referencedItemIds: string[] = []
  for (const track of unlocked.tracks) {
    const matches = track.items.filter(item => timelineItemReferencesProjectAsset(item, assetId))
    if (!matches.length) {
      continue
    }
    matches.forEach(item => referencedItemIds.push(item.id))
    if (track.locked) {
      affectedTrackIds.add(track.id)
      track.locked = false
    }
  }
  let stripped = unlocked
  for (const itemId of referencedItemIds) {
    stripped = removeItem(stripped, itemId)
  }
  const next = cloneTimeline(stripped)
  for (const track of next.tracks) {
    if (affectedTrackIds.has(track.id)) {
      track.locked = true
    }
  }
  return {
    timeline: next,
    operations: [
      ...timelineTransitionOperations(before, unlocked),
      ...timelineTransitionOperations(unlocked, stripped),
      ...timelineTransitionOperations(stripped, next),
    ],
  }
}

async function removeProjectAsset(
  asset: VideoProjectAsset,
  options: {
    title: string
    confirmation: (referenceCount: number) => string
    success: string
    failurePrefix: string
  },
) {
  if (!project.value) {
    return
  }
  const referencedItems = timeline.value.tracks.flatMap(track =>
    track.items.filter(item => timelineItemReferencesProjectAsset(item, asset.id)),
  )
  try {
    await ElMessageBox.confirm(options.confirmation(referencedItems.length), options.title, {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
    projectAssetActionBusyId.value = asset.id
    if (referencedItems.length) {
      const before = timeline.value
      const removal = projectAssetTimelineRemoval(before, asset.id)
      past.value = [...past.value.slice(-49), cloneTimeline(before)]
      future.value = []
      timeline.value = removal.timeline
      queueOperations(removal.operations)
      if (referencedItems.some(item => item.id === selectedItemId.value)) {
        selectedItemId.value = ''
      }
      if (!(await manualSave())) {
        throw new Error('未能先从时间线移除该素材')
      }
    }
    const deleted = await contentApi.archiveVideoProjectAsset(project.value.id, asset.id)
    registerGeneratedAssets([deleted])
    purgeProjectAssetFromHistory(asset.id)
    importedAssets.value = importedAssets.value.filter(
      imported => imported.projectAssetId !== asset.id,
    )
    if (aiVideoCandidateAsset.value?.id === asset.id) {
      aiVideoJob.value = undefined
    }
    ElMessage.success(options.success)
  }
  catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(`${options.failurePrefix}：${messageOf(error)}`)
    }
  }
  finally {
    projectAssetActionBusyId.value = ''
  }
}

async function deleteLocalProjectAsset(asset: VideoProjectAsset) {
  await removeProjectAsset(asset, {
    title: '删除本地导入素材',
    confirmation: referenceCount =>
      referenceCount
        ? `“${asset.label}”正在时间线中使用 ${referenceCount} 次。删除后会同时移出这些片段，且不再允许撤销恢复该素材，是否继续？`
        : `确定从当前工程删除本地素材“${asset.label}”吗？`,
    success: '本地素材已从当前工程删除',
    failurePrefix: '本地素材删除失败',
  })
}

async function deleteAiGeneratedAsset(asset: VideoProjectAsset) {
  await removeProjectAsset(asset, {
    title: '删除 AI 生成结果',
    confirmation: referenceCount =>
      referenceCount
        ? `“${asset.label}”已在时间线中使用 ${referenceCount} 次。删除后会同时移出这些片段，且不再允许撤销恢复该素材，是否继续？`
        : `确定删除 AI 生成结果“${asset.label}”吗？`,
    success: 'AI 生成结果已删除',
    failurePrefix: 'AI 生成结果删除失败',
  })
}

function subtitleCuesFromResponse(segments: VideoProjectSubtitleSegment[]): GeneratedSubtitleCue[] {
  return segments.map(segment => ({
    start_seconds: segment.start_seconds,
    end_seconds: segment.end_seconds,
    text: segment.text,
    timing_source: segment.timing_source,
  }))
}

async function createSubtitleCandidate(
  data: {
    sourceProjectAssetId?: string
    text?: string
    label: string
    durationSeconds?: number
    maxCharsPerLine?: number
    maxLines?: number
    addToTimeline?: boolean
    timelineOffsetSeconds?: number
  },
  announce = true,
): Promise<VideoProjectSubtitleGenerationResponse> {
  if (!project.value) {
    throw new Error('视频工程尚未创建')
  }
  const generated = await contentApi.generateVideoProjectSubtitles(project.value.id, {
    expected_revision: project.value.current_revision,
    source_project_asset_id: data.sourceProjectAssetId || undefined,
    text: data.text || undefined,
    label: data.label,
    duration_seconds: data.durationSeconds,
    max_chars_per_line: data.maxCharsPerLine,
    max_lines: data.maxLines,
  })
  registerGeneratedAssets([generated.asset])
  if (data.addToTimeline !== false) {
    const added = addSubtitleCuesToTimeline(
      subtitleCuesFromResponse(generated.segments),
      generated.asset,
      announce,
      data.timelineOffsetSeconds,
    )
    if (!added) {
      throw new Error('字幕已生成，但没有可加入字幕轨的有效分段')
    }
  }
  return generated
}

async function generateSubtitleCandidate() {
  if (!await ensureEditorAiService('subtitle')) {
    return
  }
  if (subtitleTrackLocked.value) {
    ElMessage.warning('字幕轨已锁定，请解锁后再生成字幕')
    return
  }
  const sourceProjectAssetId
    = subtitleForm.sourceMode === 'voice'
      ? subtitleForm.sourceProjectAssetId || voiceProjectAssets.value[0]?.id || ''
      : ''
  const text
    = subtitleForm.sourceMode === 'text' ? subtitleForm.text.trim() : subtitleForm.sourceText.trim()
  if (subtitleForm.sourceMode === 'voice' && !sourceProjectAssetId) {
    ElMessage.warning('请先生成或选择一个配音候选')
    return
  }
  if (subtitleForm.sourceMode === 'text' && !text) {
    ElMessage.warning('请输入字幕文稿')
    return
  }
  if (!(await manualSave()) || !project.value) {
    return
  }
  subtitleGenerating.value = true
  try {
    const generated = await createSubtitleCandidate(
      {
        sourceProjectAssetId,
        text,
        label: subtitleForm.label.trim() || 'AI 字幕',
        durationSeconds:
          subtitleForm.sourceMode === 'text' && duration.value > 0
            ? Math.min(duration.value, MAX_PROJECT_DURATION_SECONDS)
            : undefined,
        maxCharsPerLine: subtitleForm.maxCharsPerLine,
        maxLines: subtitleForm.maxLines,
        timelineOffsetSeconds: sourceProjectAssetId
          ? subtitleTimelineOffset(sourceProjectAssetId)
          : playhead.value,
      },
      false,
    )
    if (!(await manualSave())) {
      throw new Error('字幕已生成，但字幕轨保存失败')
    }
    subtitleForm.sourceProjectAssetId = sourceProjectAssetId
    liveMessage.value = `已生成 ${generated.segments.length} 条字幕并加入字幕轨`
    ElMessage.success(liveMessage.value)
  }
  catch (error) {
    ElMessage.error(`字幕生成失败：${messageOf(error)}`)
  }
  finally {
    subtitleGenerating.value = false
  }
}

async function generateVoiceCandidate() {
  if (!voiceForm.text.trim()) {
    ElMessage.warning('请填写配音文本')
    return
  }
  if (!await ensureEditorAiService('voice')) {
    return
  }
  if (!validateAiBrief() || !(await manualSave()) || !project.value) {
    return
  }
  voiceGenerating.value = true
  try {
    const asset = await contentApi.generateVideoProjectVoice(project.value.id, {
      expected_revision: project.value.current_revision,
      text: voiceForm.text.trim(),
      label: voiceForm.label.trim() || 'AI 配音候选',
      voice: voiceForm.voice.trim() || undefined,
      speed: voiceForm.speed,
      volume: voiceForm.volume,
      audio_format: voiceForm.audioFormat,
      sample_rate: 24000,
      with_timestamps: true,
      instruction: voiceForm.instruction.trim() || undefined,
    })
    registerGeneratedAssets([asset])
    subtitleForm.sourceProjectAssetId = asset.id
    if (voiceForm.generateSubtitles && subtitleGenerationAvailable.value) {
      subtitleGenerating.value = true
      try {
        const generated = await createSubtitleCandidate(
          {
            sourceProjectAssetId: asset.id,
            label: `${asset.label}字幕`,
            maxCharsPerLine: subtitleForm.maxCharsPerLine,
            maxLines: subtitleForm.maxLines,
            addToTimeline: false,
          },
          false,
        )
        liveMessage.value = `配音候选与 ${generated.segments.length} 条字幕候选已生成，加入配音时会同步加入字幕轨`
        ElMessage.success(liveMessage.value)
      }
      catch (error) {
        ElMessage.warning(`配音已生成，但字幕生成失败：${messageOf(error)}`)
      }
      finally {
        subtitleGenerating.value = false
      }
    }
    else {
      ElMessage.success('AI 配音候选已生成，可在右侧确认后加入时间线')
    }
  }
  catch (error) {
    ElMessage.error(`AI 配音生成失败：${messageOf(error)}`)
  }
  finally {
    voiceGenerating.value = false
  }
}

async function refreshImageCandidateCapability() {
  if (imageCapabilityLoading.value) {
    return
  }
  imageCapabilityLoading.value = true
  imageCapabilityError.value = ''
  try {
    contentCapabilities.value = await contentApi.capabilities()
    if (!imageCandidateCapability.value) {
      imageCapabilityError.value = '当前服务未返回图片模型参数，请联系管理员检查配置后重试。'
    }
  }
  catch (error) {
    imageCapabilityError.value = `图片模型参数读取失败：${messageOf(error)}`
  }
  finally {
    imageCapabilityLoading.value = false
  }
}

async function generateImageCandidates() {
  if (imageGenerating.value) {
    return
  }
  if (imageGenerationUncertain.value) {
    await refreshImageGenerationResult()
    return
  }
  if (!imageForm.prompt.trim()) {
    ElMessage.warning('请填写图片提示词')
    return
  }
  if (!validateAiBrief()) {
    return
  }
  imageGenerating.value = true
  imageGenerationUncertain.value = false
  try {
    if (!imageCandidateCapability.value || imageCapabilityError.value) {
      await refreshImageCandidateCapability()
    }
    const parameterError = imageCapabilityError.value || imageCandidateSettings.value.error
    if (parameterError) {
      ElMessage.warning(parameterError)
      return
    }
    const imageParameters = { size: imageForm.size.trim(), ratio: imageForm.ratio.trim() }
    if (!(await manualSave()) || !project.value) {
      return
    }
    const refreshedError = imageCandidateParameters(imageCandidateCapability.value, imageParameters).error
    if (refreshedError) {
      ElMessage.warning(refreshedError)
      return
    }
    const projectId = project.value.id
    const assets = await contentApi.generateVideoProjectImages(projectId, {
      expected_revision: project.value.current_revision,
      prompt: imageForm.prompt.trim(),
      role: imageForm.role,
      count: imageForm.count,
      label: imageForm.label.trim() || 'AI 画面候选',
      size: imageParameters.size || undefined,
      ratio: imageParameters.ratio || undefined,
      watermark: imageForm.watermark,
      reference_asset_ids: [],
    })
    if (editorDisposed || project.value?.id !== projectId) {
      return
    }
    registerGeneratedAssets(assets)
    ElMessage.success(`${assets.length} 个 AI 图片候选已生成，可在右侧确认后加入时间线`)
  }
  catch (error) {
    imageGenerationUncertain.value = error instanceof ContentGenerationError && error.outcomeUncertain
    if (imageGenerationUncertain.value) {
      ElMessage.warning('图片请求的连接已中断，结果尚未确认。请先刷新图片结果，避免重复生成。')
    }
    else {
      ElMessage.error(`AI 图片生成失败：${messageOf(error)}`)
    }
  }
  finally {
    imageGenerating.value = false
  }
}

async function refreshImageGenerationResult() {
  if (imageResultRefreshing.value || !project.value) {
    return
  }
  imageResultRefreshing.value = true
  try {
    await refreshProjectAssets()
    ElMessage.info('工程素材已刷新，请先查看是否已有本批图片；如尚未出现，可稍后再刷新。')
  }
  catch (error) {
    ElMessage.error(`图片结果读取失败：${messageOf(error)}`)
  }
  finally {
    imageResultRefreshing.value = false
  }
}

async function startNewImageBatch() {
  if (imageGenerating.value || imageResultRefreshing.value) {
    return
  }
  imageGenerationUncertain.value = false
  await generateImageCandidates()
}

async function generateVideoCandidate() {
  if (aiVideoSubmitting.value || videoGenerating.value) {
    ElMessage.info('已有视频正在生成，请等待完成后再试')
    return
  }
  aiVideoSubmitting.value = true
  const submissionSequence = ++aiVideoSubmissionSequence
  try {
    if (!aiVideoForm.prompt.trim()) {
      ElMessage.warning('请填写视频提示词')
      return
    }
    if (!await ensureEditorAiService('video')) {
      return
    }
    if (aiVideoDurationError.value) {
      ElMessage.warning(aiVideoDurationError.value)
      return
    }
    if (
      !aiVideoRatios.value.includes(aiVideoForm.ratio)
      || !aiVideoResolutions.value.includes(aiVideoForm.resolution)
    ) {
      syncAiVideoCapabilityDefaults()
      ElMessage.warning('模型能力已变化，已为你刷新可选参数，请确认后重新生成')
      return
    }
    if (!validateAiVideoReferences()) {
      ElMessage.warning(aiVideoReferenceError.value || '参考素材不符合当前模型要求，请调整后再生成')
      return
    }
    if (!validateAiBrief()) {
      return
    }
    if (!(await manualSave()) || !project.value) {
      ElMessage.warning(saveMessage.value || '工程尚未保存成功，请稍后再生成')
      return
    }
    if (aiVideoDurationError.value) {
      ElMessage.warning(aiVideoDurationError.value)
      return
    }
    videoGenerating.value = true
    // This clears only the current result panel, never the historical asset or timeline.
    aiVideoJob.value = undefined
    const projectId = project.value.id
    const job = await contentApi.generateVideoProjectVideo(project.value.id, {
      expected_revision: project.value.current_revision,
      prompt: aiVideoForm.prompt.trim(),
      label: `AI 生成视频 · ${aiVideoForm.durationSeconds}秒 · ${aiVideoForm.ratio}`,
      // The server resolves the task's authoritative current copy. Keeping
      // this out of the browser draft prevents an older session value from
      // overriding a copy that was generated, selected or edited later.
      source_copy_artifact_id: null,
      reference_image_asset_ids: [...aiVideoForm.referenceImageAssetIds],
      reference_video_asset_ids: [...aiVideoForm.referenceVideoAssetIds],
      duration_seconds: aiVideoForm.durationSeconds,
      ratio: aiVideoForm.ratio,
      resolution: aiVideoForm.resolution,
      generate_audio: aiVideoForm.generateAudio,
      return_last_frame: aiVideoForm.returnLastFrame,
      idempotency_key: createLocalId('ai-video'),
    })
    if (editorDisposed || submissionSequence !== aiVideoSubmissionSequence || project.value?.id !== projectId) {
      return
    }
    aiVideoJob.value = job
    await trackMediaJob(job)
  }
  catch (error) {
    if (!editorDisposed && submissionSequence === aiVideoSubmissionSequence) {
      videoGenerating.value = false
      ElMessage.error(`AI 视频任务创建失败：${messageOf(error)}`)
    }
  }
  finally {
    aiVideoSubmitting.value = false
  }
}

async function confirmBrandAsset(asset: VideoProjectAsset) {
  if (!project.value) {
    return
  }
  try {
    await ElMessageBox.confirm(
      `请确认“${asset.label}”符合你的品牌规范。确认后它才会被标记为正式 Logo 素材。`,
      '人工确认品牌 Logo',
      { confirmButtonText: '确认合规', cancelButtonText: '暂不确认', type: 'warning' },
    )
  }
  catch {
    return
  }
  brandConfirmingAssetId.value = asset.id
  try {
    const confirmed = await contentApi.confirmVideoProjectBrandAsset(project.value.id, asset.id)
    registerGeneratedAssets([confirmed])
    ElMessage.success('Logo 已通过人工确认并标记为正式品牌素材')
  }
  catch (error) {
    ElMessage.error(`Logo 确认失败：${messageOf(error)}`)
  }
  finally {
    brandConfirmingAssetId.value = ''
  }
}

function handleGlobalKeydown(event: KeyboardEvent) {
  if (workflowFinalizing.value) {
    return
  }
  const target = event.target as HTMLElement | null
  if (imagePreviewUrl.value || target?.closest('.el-image-viewer__wrapper')) {
    return
  }
  const editingText = target?.matches('input, textarea, [contenteditable="true"]')
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
    event.preventDefault()
    event.shiftKey ? redo() : undo()
    return
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') {
    event.preventDefault()
    redo()
    return
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    void manualSave()
    return
  }
  if (!editingText && event.code === 'Space') {
    event.preventDefault()
    togglePlayback()
  }
  if (!editingText && event.key === 'Delete') {
    deleteSelected()
  }
}

watch(() => props.initialTask, (value) => {
  if (value?.id === resolvedTaskId.value && hydrated.value
    && (taskDirty.value || saveInFlight.value || value.version < taskVersion.value)) {
    return
  }
  applyTask(value)
}, { immediate: true })
watch(
  () => ({
    taskId: resolvedTaskId.value,
    draft: videoGeneratorDraftSnapshot(),
  }),
  scheduleVideoGeneratorDraftSave,
  { deep: true },
)
watch(
  [voiceProjectAssets, () => subtitleForm.sourceMode],
  ([assets]) => {
    if (!assets.length) {
      subtitleForm.sourceMode = 'text'
    }
    if (!assets.some(asset => asset.id === subtitleForm.sourceProjectAssetId)) {
      subtitleForm.sourceProjectAssetId = assets[0]?.id || ''
    }
  },
  { immediate: true },
)
watch(currentVisualItem, (item) => {
  if (item) {
    void ensureItemPreview(item).then(syncVideoPreview)
  }
})
watch(timeline, () => {
  playbackRevision += 1
  void syncVideoPreview()
  void nextTick(syncAudioPreviews)
}, { deep: true })
watch(
  () => [...currentOverlays.value, ...currentAudio.value].map(item => item.id).join('|'),
  async () => {
    await Promise.all([...currentOverlays.value, ...currentAudio.value].map(ensureItemPreview))
    await nextTick()
    syncAudioPreviews()
  },
  { immediate: true },
)
watch(duration, (value) => {
  if (playhead.value > value) {
    playhead.value = value
  }
})
watch(
  () =>
    [
      aiGeneratorPanels.value.includes('video'),
      aiVideoProjectAssetTransportAvailable.value,
    ] as const,
  scheduleAiVideoTransportRecheck,
)

onMounted(() => {
  editorDisposed = false
  restoreVideoGeneratorDraft()
  window.addEventListener('keydown', handleGlobalKeydown)
  window.addEventListener('resize', updatePreviewViewport)
  void nextTick(observePreviewViewport)
  void loadEnterpriseLibrary().catch(() => undefined)
  void loadAiVideoCapabilities().finally(scheduleAiVideoTransportRecheck)
  void initialize()
})

async function flushPendingAutosave() {
  const shouldFlush = Boolean(saveTimer || pendingOperations.value.length || taskDirty.value)
  if (!shouldFlush) {
    return true
  }
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = undefined
  }
  return manualSave()
}

onBeforeRouteLeave(async () => {
  // Wait for the last debounced edit before this route is destroyed. An empty
  // standalone editor is intentionally ignored so simply visiting it never
  // creates a recent task.
  if (!resolvedTaskId.value && !taskDirty.value && !pendingOperations.value.length) {
    return true
  }
  await flushPendingAutosave()
  return true
})

function requestEditorHome() {
  leavingToHome = true
  emit('editorHome')
}

onBeforeUnmount(() => {
  // A conditional parent can unmount this component without a router guard.
  // Start the same flush as a best-effort fallback before suppressing timers.
  const flushPromise = flushPendingAutosave()
  if (videoGeneratorDraftSaveTimer) {
    clearTimeout(videoGeneratorDraftSaveTimer)
    videoGeneratorDraftSaveTimer = undefined
  }
  persistVideoGeneratorDraft()
  editorDisposed = true
  window.removeEventListener('keydown', handleGlobalKeydown)
  window.removeEventListener('resize', updatePreviewViewport)
  previewResizeObserver?.disconnect()
  clearAiVideoTransportRecheck()
  void flushPromise
  for (const timer of jobPollTimers.values()) {
    clearTimeout(timer)
  }
  jobPollTimers.clear()
  pausePlayback()
  previewGain.dispose()
  for (const url of ownedObjectUrls) {
    URL.revokeObjectURL(url)
  }
})
</script>

<template>
  <ElSkeleton
    v-if="!hydrated"
    :rows="8"
    animated
    role="status"
    aria-label="正在加载视频工作台"
  />
  <section
    v-else
    class="video-editor"
    :aria-busy="saveState === 'loading' || saveState === 'saving' || workflowFinalizing"
    :inert="workflowFinalizing || undefined"
  >
    <a class="skip-link" href="#video-editor-timeline">跳到时间线</a>
    <header class="editor-toolbar">
      <div class="toolbar-title">
        <div>
          <strong>{{ taskForm.name || DEFAULT_VIDEO_TASK_NAME }}</strong>
          <small>
            {{
              workflowMode
                ? initialTask?.mode === 'full'
                  ? '完整流程 · 第 6 / 7 步 · 视频生成'
                  : '快捷视频 · 第 2 / 3 步 · 视频生成'
                : '固定轨道轻量剪辑工作台'
            }}
          </small>
        </div>
      </div>
      <div class="toolbar-history" aria-label="编辑历史">
        <ElButton :disabled="!past.length" aria-label="撤销，快捷键 Ctrl+Z" @click="undo">
          撤销
        </ElButton>
        <ElButton :disabled="!future.length" aria-label="重做，快捷键 Ctrl+Y" @click="redo">
          重做
        </ElButton>
      </div>
      <div
        v-if="showToolbarSaveFeedback"
        class="toolbar-status"
        aria-live="polite"
        aria-atomic="true"
      >
        <ElTag :type="saveStateType" effect="light">{{ saveStateLabel }}</ElTag>
        <span :title="saveMessage">{{ saveMessage }}</span>
        <ElButton
          v-if="saveState === 'error'"
          link
          type="danger"
          @click="manualSave"
        >
          重试保存
        </ElButton>
      </div>
      <div class="toolbar-actions">
        <ElButton
          @click="requestEditorHome"
        >
          返回工作台
        </ElButton>
        <ElButton
          v-if="workflowMode && initialTask?.mode === 'full'"
          :disabled="workflowFinalizing"
          @click="emit('workflowEditCopy')"
        >
          修改文案
        </ElButton>
        <ElButton
          v-if="workflowMode && initialTask?.mode === 'full'"
          :disabled="workflowFinalizing"
          @click="emit('workflowBack')"
        >
          返回图片阶段
        </ElButton>
        <ElButton
          v-if="workflowMode"
          type="primary"
          :loading="workflowFinalizing"
          @click="completeWorkflowVideo"
        >
          完成并进入确认导出
        </ElButton>
        <ElButton
          v-else
          type="primary"
          :loading="exporting"
          @click="openExportDrawer"
        >
          导出
        </ElButton>
      </div>
    </header>

    <ElAlert
      v-if="loadingError"
      class="editor-error"
      type="error"
      title="视频工程服务暂不可用"
      :description="loadingError"
      :closable="false"
      show-icon
    />
    <ElAlert
      v-if="workflowMode && workflowCompletionJob && workflowFinalizing"
      type="info"
      :title="`最终视频${mediaJobStatusLabel(workflowCompletionJob.status)} · ${workflowCompletionJob.progress}%`"
      description="渲染成功并形成可确认的视频版本后，才会进入确认导出阶段。"
      :closable="false"
      show-icon
    />

    <div class="editor-panels">
      <aside class="editor-sidebar editor-left" aria-label="任务与素材">
        <div class="panel-heading">
          <div><strong>任务信息</strong><small>自动保存到内容任务</small></div>
        </div>
        <ElForm label-position="top" size="small" class="compact-form">
          <ElFormItem label="任务名称" required>
            <ElInput
              v-model="taskForm.name"
              :placeholder="DEFAULT_VIDEO_TASK_NAME"
              maxlength="80"
              @input="markTaskDirty"
            />
          </ElFormItem>
          <ElFormItem label="主题" required>
            <ElInput
              v-model="taskForm.topic"
              placeholder="例如：新品品牌短片（或选择产品）"
              @input="markTaskDirty"
            />
          </ElFormItem>
          <ElFormItem label="产品">
            <ContentProductPicker
              :model-value="taskForm.product"
              :product-resource-id="taskProductResourceId || ''"
              :resources="enterpriseLibraryAssets"
              @change="selectTaskProduct"
            />
          </ElFormItem>
          <ElFormItem label="目标人群" required>
            <ElInput v-model="taskForm.audience" @input="markTaskDirty" />
          </ElFormItem>
          <ElFormItem label="补充要求">
            <ElInput
              v-model="taskForm.requirements"
              type="textarea"
              :rows="2"
              @input="markTaskDirty"
            />
          </ElFormItem>
        </ElForm>

        <section
          v-if="showFullWorkflowContext"
          class="workflow-current-content"
          aria-labelledby="workflow-current-content-title"
        >
          <div class="panel-heading material-heading">
            <div>
              <strong id="workflow-current-content-title">当前标题与文案</strong>
              <small>优先展示前序阶段当前使用版本；历史回显不会自动用于新生成</small>
            </div>
          </div>
          <ElEmpty
            v-if="!displayedTopicArtifact && !displayedCopyArtifact"
            description="前序阶段暂无可显示的标题或文案"
          />
          <ElAlert
            v-if="!showingHistoricalText && !displayedCopyArtifact"
            title="暂无当前文案"
            description="可点击页面上方“修改文案”返回文案阶段；不修改也可仅按视频提示词继续生成。"
            type="info"
            :closable="false"
            show-icon
          />
          <article v-if="displayedTopicArtifact || displayedCopyArtifact" class="current-text-card">
            <header>
              <ElTag :type="showingHistoricalText ? 'warning' : 'info'" size="small" effect="plain">
                {{ showingHistoricalText ? `${historicalTextLabel} · 历史版本` : '当前使用内容' }}
              </ElTag>
              <small>
                标题 v{{ displayedTopicArtifact?.version || '—' }} · 文案 v{{
                  displayedCopyArtifact?.version || '—'
                }}
              </small>
            </header>
            <h4>
              {{
                currentArtifactTitle(displayedTopicArtifact)
                  || currentArtifactTitle(displayedCopyArtifact)
                  || '未填写标题'
              }}
            </h4>
            <p>{{ currentArtifactBody(displayedCopyArtifact) || '未填写文案正文' }}</p>
          </article>
        </section>

        <section
          v-if="showFullWorkflowContext"
          class="workflow-project-assets"
          aria-labelledby="workflow-project-assets-title"
        >
          <div class="panel-heading material-heading">
            <div>
              <strong id="workflow-project-assets-title">本任务已确认素材</strong>
              <small>前序确认图片已进入工程素材库，可加入时间线或作为 AI 视频参考图片</small>
            </div>
            <ElButton
              type="primary"
              plain
              size="small"
              :loading="workflowBulkImporting"
              :disabled="!workflowProjectAssetsToImport.length"
              @click="importAllWorkflowProjectAssets"
            >
              {{ workflowProjectAssetsToImport.length ? '全部导入时间线' : '已全部导入' }}
            </ElButton>
          </div>
          <ElEmpty v-if="!workflowProjectAssets.length" description="前序阶段暂无已确认图片素材" />
          <div v-else class="generated-assets workflow-generated-assets">
            <article
              v-for="asset in workflowProjectAssets"
              :key="asset.id"
              class="generated-asset-card"
              data-testid="workflow-project-asset-card"
            >
              <div class="generated-asset-preview">
                <img
                  v-if="projectAssetMediaKind(asset) === 'image' && projectAssetUrl(asset)"
                  :src="projectAssetUrl(asset)"
                  :alt="asset.label"
                  class="image-preview-trigger"
                  role="button"
                  tabindex="0"
                  title="双击查看完整图片"
                  :aria-label="`${asset.label}，双击或按回车查看完整图片`"
                  loading="lazy"
                  @dblclick.stop="openImagePreview(projectAssetUrl(asset))"
                  @keydown.enter.stop.prevent="openImagePreview(projectAssetUrl(asset))"
                  @keydown.space.stop.prevent="openImagePreview(projectAssetUrl(asset))"
                >
                <span v-else>图</span>
              </div>
              <div class="generated-asset-copy">
                <strong :title="asset.label">{{ asset.label }}</strong>
                <small>{{ projectAssetSourceLabel(asset) }} · {{ projectAssetKind(asset) }}</small>
              </div>
              <div class="generated-asset-actions">
                <ElButton
                  v-if="timelineTrackForProjectAsset(asset) !== 'overlay'"
                  size="small"
                  type="primary"
                  plain
                  :disabled="projectAssetAddDisabled(asset)"
                  :title="projectAssetAddDisabledReason(asset)"
                  @click="addProjectAssetToTimeline(asset)"
                >
                  加入
                </ElButton>
                <ElButton size="small" @click="downloadProjectAsset(asset)">下载</ElButton>
              </div>
            </article>
          </div>
        </section>

        <div class="panel-heading material-heading">
          <div><strong>本地素材</strong><small>批量导入后自动加入对应轨道</small></div>
        </div>
        <div class="two-fields import-role-fields">
          <div>
            <label class="audio-role-label" for="video-visual-role">画面导入位置</label>
            <ElSelect
              id="video-visual-role"
              v-model="visualRole"
              size="small"
              aria-label="选择图片视频导入轨道"
            >
              <ElOption label="主画面" value="video" />
              <ElOption label="片头" value="intro" />
              <ElOption label="片尾" value="outro" />
            </ElSelect>
          </div>
        </div>
        <button
          type="button"
          class="upload-dropzone"
          aria-describedby="video-upload-help"
          @click="openFileDialog"
          @keydown.enter.prevent="openFileDialog"
          @keydown.space.prevent="openFileDialog"
          @dragover.prevent
          @drop="onDropFiles"
        >
          <strong>拖入或批量选择素材</strong>
          <span id="video-upload-help">图片 / 视频 / 音频 / SRT / VTT / TXT</span>
        </button>
        <input
          ref="fileInput"
          class="visually-hidden"
          type="file"
          multiple
          accept="image/*,video/*,audio/*,.srt,.vtt,.txt"
          @change="onFileChange"
        >
        <div class="import-list" aria-label="导入素材列表">
          <div v-for="asset in visibleImportJobs" :key="asset.id" class="import-item">
            <button
              type="button"
              class="import-select"
              :disabled="!asset.clipId"
              @click="importedAssetSelected(asset)"
            >
              <span class="asset-kind">{{ asset.kind }}</span>
              <span><strong>{{ asset.name }}</strong><small>{{ asset.message }}</small></span>
              <ElTag
                size="small"
                :type="
                  asset.status === 'error'
                    ? 'danger'
                    : asset.status === 'ready'
                      ? 'success'
                      : 'warning'
                "
              >
                {{
                  asset.status === 'error'
                    ? '失败'
                    : asset.status === 'ready'
                      ? '就绪'
                      : asset.status === 'cancelled'
                        ? '已取消'
                        : '处理中'
                }}
              </ElTag>
            </button>
            <ElProgress
              v-if="asset.status === 'uploading'"
              :percentage="asset.progress || 0"
              :show-text="false"
              :stroke-width="3"
              aria-label="素材上传进度"
            />
            <button
              v-if="asset.status === 'uploading'"
              type="button"
              class="cancel-import"
              :aria-label="`取消上传 ${asset.name}`"
              @click="cancelImportedAsset(asset)"
            >
              取消
            </button>
          </div>
          <p v-if="!visibleImportJobs.length && !localProjectAssets.length" class="empty-copy">
            尚未导入本地素材
          </p>
        </div>
        <div class="generated-assets local-project-assets" aria-label="已导入的本地素材">
          <article
            v-for="asset in localProjectAssets"
            :key="asset.id"
            class="generated-asset-card"
            data-testid="local-project-asset-card"
            :data-asset-id="asset.id"
          >
            <div class="generated-asset-preview">
              <img
                v-if="projectAssetMediaKind(asset) === 'image' && projectAssetUrl(asset)"
                :src="projectAssetUrl(asset)"
                :alt="asset.label"
                class="image-preview-trigger"
                role="button"
                tabindex="0"
                title="双击查看完整图片"
                :aria-label="`${asset.label}，双击或按回车查看完整图片`"
                loading="lazy"
                @dblclick.stop="openImagePreview(projectAssetUrl(asset))"
                @keydown.enter.stop.prevent="openImagePreview(projectAssetUrl(asset))"
                @keydown.space.stop.prevent="openImagePreview(projectAssetUrl(asset))"
              >
              <video
                v-else-if="projectAssetMediaKind(asset) === 'video' && projectAssetUrl(asset)"
                :src="projectAssetUrl(asset)"
                :aria-label="`预览 ${asset.label}`"
                controls
                muted
                playsinline
                preload="metadata"
              />
              <audio
                v-else-if="projectAssetMediaKind(asset) === 'audio' && projectAssetUrl(asset)"
                :src="projectAssetUrl(asset)"
                :aria-label="`试听 ${asset.label}`"
                controls
                preload="metadata"
              />
              <span v-else>{{
                asset.role === 'subtitle' ? '字' : asset.role === 'voice' ? '声' : '本'
              }}</span>
            </div>
            <div class="generated-asset-copy">
              <strong :title="asset.label">{{ asset.label }}</strong>
              <small>{{ projectAssetSourceLabel(asset) }} · {{ projectAssetKind(asset) }}</small>
              <p>
                {{
                  projectAssetReferenceCount(asset)
                    ? `时间线已使用 ${projectAssetReferenceCount(asset)} 次`
                    : '已保存到工程，可重新加入'
                }}
              </p>
            </div>
            <div class="generated-asset-state">
              <ElTag
                size="small"
                :type="projectAssetReferenceCount(asset) ? 'success' : 'info'"
                effect="plain"
              >
                {{ projectAssetReferenceCount(asset) ? '使用中' : '可加入' }}
              </ElTag>
            </div>
            <div class="generated-asset-actions">
              <ElButton
                v-if="timelineTrackForProjectAsset(asset) !== 'overlay'"
                size="small"
                type="primary"
                plain
                :disabled="projectAssetAddDisabled(asset)"
                :title="projectAssetAddDisabledReason(asset)"
                :aria-label="`将本地素材 ${asset.label} 加入时间线`"
                @click="addProjectAssetToTimeline(asset)"
              >
                加入
              </ElButton>
              <ElButton size="small" @click="downloadProjectAsset(asset)"> 下载 </ElButton>
              <ElButton
                link
                size="small"
                :loading="projectAssetActionBusyId === asset.id"
                @click="renameProjectAsset(asset)"
              >
                重命名
              </ElButton>
              <ElButton
                link
                size="small"
                type="danger"
                :loading="projectAssetActionBusyId === asset.id"
                @click="deleteLocalProjectAsset(asset)"
              >
                删除
              </ElButton>
            </div>
          </article>
        </div>

        <div class="panel-heading material-heading generated-heading">
          <div>
            <strong>企业知识库素材</strong><small>从企业知识库搜索、预览并加入时间线</small>
          </div>
          <span class="material-library-tools">
            <ElTag size="small" effect="plain">{{ enterpriseMediaAssets.length }} 项</ElTag>
            <ElButton
              link
              type="primary"
              size="small"
              :loading="assetLibraryRefreshing || enterpriseLibraryLoading"
              @click="refreshAssetLibrary"
            >刷新</ElButton>
          </span>
        </div>
        <div class="enterprise-library-entry">
          <ContentResourcePicker
            :model-value="enterpriseSelectionIds"
            :resources="enterpriseMediaAssets"
            :kinds="enterpriseMediaKinds"
            title="选择企业知识库素材"
            description="可搜索并预览企业图片、视频和音频；图片/视频按画面导入位置加入，音频默认加入配音轨。"
            button-text="浏览企业知识库"
            empty-text="企业知识库中暂无可用媒体素材"
            :preserve-unknown="false"
            @update:model-value="importEnterpriseSelection"
          />
          <ElAlert
            v-if="enterpriseLibraryError"
            :title="enterpriseLibraryError"
            type="warning"
            :closable="false"
          />
          <small v-else>图片/视频使用“画面导入位置”；音频默认加入配音，选中后可上下移动到配音或背景音效轨。</small>
        </div>
        <div class="generated-assets" aria-label="企业知识库素材">
          <article
            v-for="asset in enterpriseProjectAssets"
            :key="asset.id"
            class="generated-asset-card"
            data-testid="enterprise-project-asset-card"
            :data-asset-id="asset.id"
          >
            <div class="generated-asset-preview">
              <img
                v-if="projectAssetMediaKind(asset) === 'image' && projectAssetUrl(asset)"
                :src="projectAssetUrl(asset)"
                :alt="asset.label"
                class="image-preview-trigger"
                role="button"
                tabindex="0"
                title="双击查看完整图片"
                :aria-label="`${asset.label}，双击或按回车查看完整图片`"
                loading="lazy"
                @dblclick.stop="openImagePreview(projectAssetUrl(asset))"
                @keydown.enter.stop.prevent="openImagePreview(projectAssetUrl(asset))"
                @keydown.space.stop.prevent="openImagePreview(projectAssetUrl(asset))"
              >
              <video
                v-else-if="projectAssetMediaKind(asset) === 'video' && projectAssetUrl(asset)"
                :src="projectAssetUrl(asset)"
                :aria-label="`预览 ${asset.label}`"
                controls
                muted
                playsinline
                preload="metadata"
              />
              <audio
                v-else-if="projectAssetMediaKind(asset) === 'audio' && projectAssetUrl(asset)"
                :src="projectAssetUrl(asset)"
                :aria-label="`试听 ${asset.label}`"
                controls
                preload="metadata"
              />
              <span v-else>{{
                asset.role === 'voice' ? '声' : asset.role === 'video' ? '影' : '企'
              }}</span>
            </div>
            <div class="generated-asset-copy">
              <strong :title="asset.label">{{ asset.label }}</strong>
              <small>{{ projectAssetSourceLabel(asset) }} · {{ projectAssetKind(asset) }} · v{{
                projectAssetVersion(asset)
              }}</small>
              <p>
                {{
                  projectAssetReferenceCount(asset)
                    ? `时间线已使用 ${projectAssetReferenceCount(asset)} 次`
                    : '已引用到工程，尚未使用'
                }}
              </p>
            </div>
            <div class="generated-asset-state">
              <ElTag
                size="small"
                :type="projectAssetReferenceCount(asset) ? 'success' : 'info'"
                effect="plain"
              >
                {{ projectAssetReferenceCount(asset) ? '使用中' : '可加入' }}
              </ElTag>
            </div>
            <div class="generated-asset-actions">
              <ElButton
                v-if="timelineTrackForProjectAsset(asset) !== 'overlay'"
                size="small"
                type="primary"
                plain
                :disabled="projectAssetAddDisabled(asset)"
                :title="projectAssetAddDisabledReason(asset)"
                :aria-label="`将企业素材 ${asset.label} 加入时间线`"
                @click="addProjectAssetToTimeline(asset)"
              >
                加入
              </ElButton>
              <ElButton size="small" @click="downloadProjectAsset(asset)"> 下载 </ElButton>
              <ElButton
                link
                size="small"
                :loading="projectAssetActionBusyId === asset.id"
                @click="renameProjectAsset(asset)"
              >
                重命名
              </ElButton>
              <ElButton
                link
                size="small"
                type="danger"
                :disabled="projectAssetReferenceCount(asset) > 0"
                :title="
                  projectAssetReferenceCount(asset)
                    ? '请先从时间线移除后再归档'
                    : '只归档本工程引用，不删除企业知识库原件'
                "
                :loading="projectAssetActionBusyId === asset.id"
                @click="archiveProjectAsset(asset)"
              >
                归档
              </ElButton>
            </div>
          </article>
        </div>
      </aside>

      <main class="preview-panel" aria-label="视频预览">
        <div class="preview-heading">
          <div>
            <strong>实时预览</strong><small>浏览器轻量预览，最终效果以服务端渲染为准</small>
          </div>
          <span class="preview-badges">
            <ElTooltip :content="webAvStatus.reason" placement="bottom">
              <ElTag
                effect="plain"
                :type="
                  webAvStatus.state === 'ready'
                    ? 'success'
                    : webAvStatus.state === 'failed'
                      ? 'danger'
                      : 'info'
                "
              >
                {{
                  webAvStatus.state === 'ready'
                    ? 'WebAV 预检'
                    : webAvStatus.state === 'checking'
                      ? 'WebAV 检查中'
                      : 'HTML5 回退'
                }}
              </ElTag>
            </ElTooltip>
            <ElTag effect="plain">{{ timeline.canvas.width }} × {{ timeline.canvas.height }} ·
              {{ timeline.canvas.fps }}fps</ElTag>
          </span>
        </div>
        <div ref="previewStageWrap" class="preview-stage-wrap" data-testid="preview-stage-wrap">
          <div
            class="preview-stage"
            :style="previewStageStyle"
            :data-canvas-ratio="timeline.canvas.aspect_ratio"
            data-testid="preview-stage"
          >
            <div
              v-for="layer in currentVisualLayers"
              :key="layer.item.id"
              class="preview-visual-layer"
              :style="{ opacity: layer.opacity }"
            >
              <video
                v-if="resolveItemUrl(layer.item) && isVideoPreviewItem(layer.item)"
                :ref="element => setPreviewVideoElement(layer.item.id, element)"
                crossorigin="anonymous"
                :src="resolveItemUrl(layer.item)"
                :style="{ objectFit: visualFitMode(layer.item) }"
                playsinline
                preload="metadata"
                @loadedmetadata="syncVideoPreview"
                @click="togglePlayback"
              />
              <img
                v-else-if="resolveItemUrl(layer.item)"
                :src="resolveItemUrl(layer.item)"
                :alt="layer.item.label"
                :style="{ objectFit: visualFitMode(layer.item) }"
                class="image-preview-trigger"
                role="button"
                tabindex="0"
                title="双击查看完整图片"
                :aria-label="`${layer.item.label}，双击或按回车查看完整图片`"
                @dblclick.stop="openImagePreview(resolveItemUrl(layer.item))"
                @keydown.enter.stop.prevent="openImagePreview(resolveItemUrl(layer.item))"
                @keydown.space.stop.prevent="openImagePreview(resolveItemUrl(layer.item))"
              >
            </div>
            <div v-if="!currentVisualLayers.length" class="preview-empty">
              <span>预览画布</span>
              <small>从左侧导入图片或视频开始制作</small>
            </div>
            <div class="preview-overlays" aria-hidden="true">
              <div
                v-for="item in currentOverlays"
                :key="item.id"
                class="preview-overlay-item"
                :style="overlayPreviewStyle(item, timeline.canvas, previewStageSize.width)"
              >
                <video
                  v-if="resolveItemUrl(item) && item.metadata.media_kind === 'video'"
                  :src="resolveItemUrl(item)"
                  playsinline
                  muted
                  autoplay
                  loop
                  :aria-label="item.label"
                />
                <img
                  v-else-if="resolveItemUrl(item)"
                  :src="resolveItemUrl(item)"
                  :alt="item.label"
                >
                <span v-else>{{ item.content || item.label }}</span>
              </div>
            </div>
            <div
              v-for="item in currentSubtitles"
              :key="item.id"
              class="preview-subtitles"
              :style="subtitlePreviewStyle(item, timeline.canvas, previewStageSize.width)"
            >
              {{ item.content || item.label }}
            </div>
          </div>
        </div>
        <div class="preview-controls">
          <ElButton
            type="primary"
            circle
            :aria-label="playing ? '暂停预览' : '播放预览'"
            @click="togglePlayback"
          >
            {{ playing ? 'Ⅱ' : '▶' }}
          </ElButton>
          <span>{{ formatTime(playhead) }} / {{ formatTime(duration) }}</span>
          <ElSlider
            :model-value="playhead"
            :min="0"
            :max="Math.max(duration, 0.1)"
            :step="0.01"
            :show-tooltip="false"
            aria-label="视频播放头"
            @update:model-value="setPlayhead(Number($event))"
          />
        </div>
        <AudioWaveform
          v-if="selectedAudioItem && resolveItemUrl(selectedAudioItem)"
          :key="selectedAudioItem.id"
          :item-id="selectedAudioItem.id"
          :label="selectedAudioItem.label"
          :url="resolveItemUrl(selectedAudioItem)"
          :clip-start="selectedAudioItem.start_seconds"
          :clip-duration="selectedAudioItem.duration_seconds"
          :trim-in="selectedAudioItem.trim_in_seconds"
          :trim-out="selectedAudioItem.trim_out_seconds"
          :speed="selectedAudioItem.speed"
          :volume="selectedAudioItem.volume"
          :loop="Boolean(selectedAudioItem.metadata.loop)"
          :playhead="playhead"
          @seek="setPlayhead"
          @media-element="setPreviewAudioElement"
          @ready="syncAudioPreviews"
          @error="liveMessage = $event"
        />
        <div v-if="currentAudio.length" class="active-audio" aria-label="当前活动音频">
          <template v-for="item in currentAudio" :key="item.id">
            <span>{{ item.label }} · {{ Math.round(item.volume * 100) }}%</span>
            <audio
              v-if="resolveItemUrl(item) && item.id !== selectedAudioItem?.id"
              :ref="(element) => setPreviewAudioElement(item.id, element)"
              class="preview-audio-element"
              crossorigin="anonymous"
              :src="resolveItemUrl(item)"
              preload="metadata"
              aria-hidden="true"
              @loadedmetadata="syncAudioPreviews"
            />
          </template>
        </div>
      </main>

      <aside class="editor-sidebar editor-right" aria-label="属性与 AI 调整">
        <div class="panel-heading">
          <div>
            <strong>画布与属性</strong><small>{{
              selectedItem ? `已选：${selectedItem.label}` : '选择时间线片段后编辑'
            }}</small>
          </div>
        </div>
        <ElForm label-position="top" size="small" class="compact-form">
          <div class="two-fields">
            <ElFormItem label="画面比例">
              <ElSelect
                :model-value="timeline.canvas.aspect_ratio"
                aria-label="画布比例"
                data-testid="canvas-aspect-select"
                @update:model-value="applyCanvasPreset(String($event))"
              >
                <ElOption label="竖版 9:16" value="9:16" />
                <ElOption label="横版 16:9" value="16:9" />
                <ElOption label="方形 1:1" value="1:1" />
                <ElOption label="标准 4:3" value="4:3" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem label="帧率">
              <ElInputNumber
                :model-value="timeline.canvas.fps"
                :precision="0"
                :min="1"
                :max="60"
                @update:model-value="updateCanvas({ fps: Number($event) || 25 })"
              />
            </ElFormItem>
          </div>
          <template v-if="selectedItem">
            <ElFormItem label="片段名称">
              <ElInput
                :model-value="selectedItem.label"
                @update:model-value="patchSelected({ label: String($event) })"
              />
            </ElFormItem>
            <ElFormItem
              v-if="selectedTrack?.kind === 'subtitle' && !selectedItem.project_asset_id"
              label="显示内容"
            >
              <ElInput
                :model-value="selectedItem.content || ''"
                type="textarea"
                :rows="2"
                @update:model-value="patchSelected({ content: String($event) })"
              />
            </ElFormItem>
            <div class="two-fields">
              <ElFormItem label="开始（秒）">
                <ElInputNumber
                  :model-value="selectedItem.start_seconds"
                  :min="0"
                  :step="0.1"
                  :precision="2"
                  :disabled="selectedTrack?.kind === 'video' || (selectedTrack?.kind === 'subtitle' && Boolean(selectedItem.project_asset_id))"
                  @update:model-value="patchSelectedNumber('start_seconds', $event)"
                />
              </ElFormItem>
              <ElFormItem label="时长（秒）">
                <ElInputNumber
                  :model-value="selectedItem.duration_seconds"
                  :min="0.1"
                  :step="0.1"
                  :precision="2"
                  @update:model-value="patchSelectedNumber('duration_seconds', $event, 'trim')"
                />
              </ElFormItem>
              <ElFormItem v-if="selectedCanTrim" label="裁掉开头（秒）">
                <ElInputNumber
                  :model-value="selectedItem.trim_in_seconds"
                  :min="0"
                  :step="0.1"
                  :precision="2"
                  @update:model-value="patchSelectedNumber('trim_in_seconds', $event, 'trim')"
                />
              </ElFormItem>
              <ElFormItem v-if="selectedCanTrim" label="裁掉结尾（秒）">
                <ElInputNumber
                  :model-value="selectedItem.trim_out_seconds"
                  :min="0"
                  :step="0.1"
                  :precision="2"
                  @update:model-value="patchSelectedNumber('trim_out_seconds', $event, 'trim')"
                />
              </ElFormItem>
            </div>
            <template v-if="selectedTrack?.kind === 'video' || selectedTrack?.kind === 'overlay'">
              <div class="two-fields">
                <ElFormItem v-if="selectedTrack?.kind === 'video'" label="画面适配">
                  <ElSelect
                    :model-value="String(selectedItem.metadata.fit_mode || 'contain')"
                    @update:model-value="updateMetadataField('fit_mode', String($event))"
                  >
                    <ElOption label="适应画布" value="contain" />
                    <ElOption label="填充裁切" value="cover" />
                  </ElSelect>
                </ElFormItem>
                <ElFormItem v-if="selectedTrack.kind === 'video'" label="转场">
                  <ElSelect
                    :model-value="String(selectedItem.metadata.transition || 'none')"
                    @update:model-value="updateTransition"
                  >
                    <ElOption label="无转场" value="none" />
                    <ElOption label="淡入淡出" value="fade" />
                    <ElOption label="淡入黑场" value="fadeblack" />
                    <ElOption label="淡入白场" value="fadewhite" />
                  </ElSelect>
                </ElFormItem>
              </div>
              <ElFormItem
                v-if="
                  selectedTrack.kind === 'video'
                    && String(selectedItem.metadata.transition || 'none') !== 'none'
                "
                label="转场持续时间（秒）"
              >
                <ElInputNumber
                  :model-value="Number(selectedItem.metadata.transition_duration_seconds ?? 0.4)"
                  :min="0"
                  :max="Math.min(5, selectedItem.duration_seconds)"
                  :step="0.1"
                  :precision="2"
                  @update:model-value="
                    updateMetadataField('transition_duration_seconds', Number($event) || 0)
                  "
                />
              </ElFormItem>
            </template>
            <ElFormItem
              v-if="selectedHasAudio"
              label="音量"
            >
              <ElSlider
                :model-value="selectedItem.volume"
                :min="0"
                :max="2"
                :step="0.05"
                show-input
                @update:model-value="patchSelectedNumber('volume', $event)"
              />
            </ElFormItem>
            <ElFormItem v-if="selectedTrack?.kind === 'voice'" label="配音语速">
              <ElInputNumber
                :model-value="selectedItem.speed"
                :min="0.1"
                :max="8"
                :step="0.1"
                :precision="2"
                @update:model-value="updateSelectedVoiceSpeed"
              />
            </ElFormItem>
            <div
              v-if="['voice', 'music', 'sfx'].includes(selectedTrack?.kind || '')"
              class="two-fields"
            >
              <ElFormItem label="淡入（秒）">
                <ElInputNumber
                  :model-value="Number(selectedItem.metadata.fade_in_seconds || 0)"
                  :min="0"
                  :max="selectedItem.duration_seconds"
                  :step="0.1"
                  :precision="2"
                  @update:model-value="updateMetadataField('fade_in_seconds', Number($event) || 0)"
                />
              </ElFormItem>
              <ElFormItem label="淡出（秒）">
                <ElInputNumber
                  :model-value="Number(selectedItem.metadata.fade_out_seconds || 0)"
                  :min="0"
                  :max="selectedItem.duration_seconds"
                  :step="0.1"
                  :precision="2"
                  @update:model-value="updateMetadataField('fade_out_seconds', Number($event) || 0)"
                />
              </ElFormItem>
            </div>
            <ElFormItem v-if="selectedTrack?.kind === 'music'" label="循环补足片段时长">
              <ElSwitch
                :model-value="Boolean(selectedItem.metadata.loop)"
                active-text="循环"
                inactive-text="不循环"
                @update:model-value="updateMetadataField('loop', Boolean($event))"
              />
            </ElFormItem>
            <template v-if="selectedTrack?.kind === 'subtitle'">
              <div class="two-fields">
                <ElFormItem label="字幕字号">
                  <ElInputNumber
                    :model-value="Number(subtitleStyleValue('font_size', 28))"
                    :disabled="subtitleOutputMode === 'soft'"
                    :min="8"
                    :max="120"
                    @update:model-value="updateSubtitleStyle('font_size', Number($event) || 28)"
                  />
                </ElFormItem>
                <ElFormItem label="全片字幕方式">
                  <ElSelect
                    :model-value="subtitleOutputMode"
                    @update:model-value="updateSubtitleOutputMode"
                  >
                    <ElOption label="烧录字幕" value="burn" />
                    <ElOption label="软字幕" value="soft" />
                  </ElSelect>
                </ElFormItem>
              </div>
              <p class="property-hint">
                {{ subtitleOutputMode === 'soft'
                  ? '软字幕可在支持的播放器中开关，字体样式由播放器决定；需要保留设定的颜色和描边，请选烧录字幕。'
                  : '烧录字幕会直接显示在完整成片中，不能在播放器里单独关闭。' }}
              </p>
              <div class="two-fields subtitle-color-fields">
                <ElFormItem label="文字颜色">
                  <ElColorPicker
                    :model-value="String(subtitleStyleValue('primary_color', '#FFFFFF'))"
                    :disabled="subtitleOutputMode === 'soft'"
                    @update:model-value="
                      updateSubtitleStyle('primary_color', String($event || '#FFFFFF'))
                    "
                  />
                </ElFormItem>
                <ElFormItem label="描边颜色">
                  <ElColorPicker
                    :model-value="String(subtitleStyleValue('outline_color', '#000000'))"
                    :disabled="subtitleOutputMode === 'soft'"
                    @update:model-value="
                      updateSubtitleStyle('outline_color', String($event || '#000000'))
                    "
                  />
                </ElFormItem>
                <ElFormItem label="描边宽度">
                  <ElInputNumber
                    :model-value="Number(subtitleStyleValue('outline_width', 2))"
                    :disabled="subtitleOutputMode === 'soft'"
                    :min="0"
                    :max="10"
                    :step="0.5"
                    @update:model-value="updateSubtitleStyle('outline_width', Number($event) || 0)"
                  />
                </ElFormItem>
              </div>
            </template>
            <div v-if="selectedTrack?.kind === 'overlay'" class="two-fields">
              <ElFormItem label="X 位置%">
                <ElInputNumber
                  :model-value="Number(selectedItem.transform.x ?? 50)"
                  :min="0"
                  :max="100"
                  @update:model-value="updateTransform('x', $event)"
                />
              </ElFormItem>
              <ElFormItem label="Y 位置%">
                <ElInputNumber
                  :model-value="Number(selectedItem.transform.y ?? 15)"
                  :min="0"
                  :max="100"
                  @update:model-value="updateTransform('y', $event)"
                />
              </ElFormItem>
              <ElFormItem label="缩放">
                <ElInputNumber
                  :model-value="Number(selectedItem.transform.scale ?? 1)"
                  :min="0.1"
                  :max="10"
                  :step="0.1"
                  @update:model-value="updateTransform('scale', $event)"
                />
              </ElFormItem>
              <ElFormItem label="透明度">
                <ElInputNumber
                  :model-value="Number(selectedItem.transform.opacity ?? 1)"
                  :min="0"
                  :max="1"
                  :step="0.05"
                  @update:model-value="updateTransform('opacity', $event)"
                />
              </ElFormItem>
            </div>
          </template>
        </ElForm>

        <div class="panel-heading ai-heading">
          <div>
            <strong>AI 生成结果</strong><small>结果先留在这里；点击“加入”后才进入时间线</small>
          </div>
        </div>
        <div
          v-if="aiCandidateAssets.length"
          class="ai-candidate-results"
          aria-label="已生成 AI 素材候选"
        >
          <article v-for="asset in aiCandidateAssets" :key="asset.id">
            <span class="asset-kind">{{
              asset.role === 'subtitle'
                ? '字'
                : projectAssetMediaKind(asset) === 'audio'
                  ? '声'
                  : projectAssetMediaKind(asset) === 'video'
                    ? '影'
                    : '图'
            }}</span>
            <div>
              <strong :title="asset.label">{{ asset.label }}</strong>
              <small :title="projectAssetPrompt(asset)">{{ projectAssetKind(asset)
              }}{{ projectAssetPrompt(asset) ? ` · ${projectAssetPrompt(asset)}` : '' }}</small>
            </div>
            <span class="ai-candidate-actions">
              <ElButton
                v-if="asset.role === 'logo' && asset.metadata.pending_brand_confirmation === true"
                link
                size="small"
                :loading="brandConfirmingAssetId === asset.id"
                @click="confirmBrandAsset(asset)"
              >确认 Logo</ElButton>
              <ElButton
                v-if="timelineTrackForProjectAsset(asset) !== 'overlay'"
                link
                type="primary"
                size="small"
                :disabled="projectAssetAddDisabled(asset) || projectAssetReferenceCount(asset) > 0"
                @click="addProjectAssetToTimeline(asset)"
              >{{ projectAssetReferenceCount(asset) ? '已加入' : '加入' }}</ElButton>
              <ElButton link size="small" @click="downloadProjectAsset(asset)">下载</ElButton>
              <ElButton
                link
                type="danger"
                size="small"
                :loading="projectAssetActionBusyId === asset.id"
                @click="deleteAiGeneratedAsset(asset)"
              >删除</ElButton>
            </span>
          </article>
        </div>
        <ElCollapse v-model="aiGeneratorPanels" class="ai-generator-collapse">
          <ElCollapseItem name="voice">
            <template #title><strong>配音候选</strong><span>文字转配音</span></template>
            <ElInput
              v-model="voiceForm.text"
              type="textarea"
              :rows="3"
              maxlength="100000"
              placeholder="输入要朗读的完整配音文本"
              aria-label="AI 配音文本"
            />
            <ElInput
              v-model="voiceForm.instruction"
              class="generator-field"
              placeholder="朗读提示，例如：沉稳、可信、语速舒缓"
              maxlength="2000"
              aria-label="AI 配音提示"
            />
            <div class="two-fields generator-field">
              <ElInput v-model="voiceForm.label" placeholder="候选名称" aria-label="配音候选名称" />
              <ElSelect
                v-model="voiceForm.voice"
                clearable
                filterable
                :loading="aiVideoCapabilityLoading"
                :disabled="speechVoiceSelectDisabled"
                :placeholder="speechVoicePlaceholder"
                :title="speechVoiceSelectDisabled ? speechVoicePlaceholder : undefined"
                aria-label="配音音色"
              >
                <ElOption
                  v-for="option in speechVoiceOptions"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </ElSelect>
              <ElInputNumber
                v-model="voiceForm.speed"
                :min="speechSpeedRange?.min ?? 0.1"
                :max="speechSpeedRange?.max ?? 4"
                :step="0.1"
                :disabled="!speechGenerationAvailable"
                aria-label="配音语速"
              />
              <ElSelect v-model="voiceForm.audioFormat" aria-label="配音格式">
                <ElOption label="MP3" value="mp3" />
                <ElOption label="WAV" value="wav" />
                <ElOption label="OGG Opus" value="ogg_opus" />
              </ElSelect>
            </div>
            <label class="generator-switch">
              <span>同时生成字幕</span>
              <ElSwitch
                v-model="voiceForm.generateSubtitles"
                :disabled="!subtitleGenerationAvailable"
                :title="
                  subtitleGenerationAvailable
                    ? '配音生成后同步生成字幕并加入字幕轨'
                    : '当前字幕生成真实能力未启用'
                "
                aria-label="配音时同时生成字幕"
              />
            </label>
            <ElButton
              class="ai-button"
              type="primary"
              plain
              :loading="voiceGenerating || subtitleGenerating"
              :disabled="aiServiceAttemptChecking"
              @click="generateVoiceCandidate"
            >
              生成配音候选
            </ElButton>
          </ElCollapseItem>

          <ElCollapseItem name="subtitle">
            <template #title><strong>字幕</strong></template>
            <ElForm label-position="top" size="small" class="compact-form subtitle-generator-form">
              <div class="two-fields">
                <ElFormItem label="生成来源">
                  <ElSelect v-model="subtitleForm.sourceMode" aria-label="字幕生成来源">
                    <ElOption v-if="voiceProjectAssets.length" label="已有配音" value="voice" />
                    <ElOption label="输入文稿" value="text" />
                  </ElSelect>
                </ElFormItem>
                <ElFormItem label="字幕名称">
                  <ElInput
                    v-model="subtitleForm.label"
                    maxlength="255"
                    placeholder="AI 字幕"
                    aria-label="字幕候选名称"
                  />
                </ElFormItem>
              </div>
              <ElFormItem v-if="subtitleForm.sourceMode === 'voice'" label="配音候选">
                <ElSelect
                  v-model="subtitleForm.sourceProjectAssetId"
                  filterable
                  :disabled="!voiceProjectAssets.length"
                  :placeholder="voiceProjectAssets.length ? '选择配音候选' : '请先生成配音'"
                  no-data-text="暂无配音候选"
                  aria-label="选择用于生成字幕的配音候选"
                >
                  <ElOption
                    v-for="asset in voiceProjectAssets"
                    :key="asset.id"
                    :label="`${asset.label}${String(asset.metadata.text || '').trim() ? '' : '（需补文稿）'}`"
                    :value="asset.id"
                  />
                </ElSelect>
              </ElFormItem>
              <ElFormItem v-if="subtitleForm.sourceMode === 'voice'" label="文稿（可选）">
                <ElInput
                  v-model="subtitleForm.sourceText"
                  type="textarea"
                  :rows="3"
                  maxlength="100000"
                  placeholder="示例：音频没有原文时，可在这里粘贴文稿"
                  aria-label="配音对应字幕文稿"
                />
              </ElFormItem>
              <ElFormItem v-else label="字幕文稿">
                <ElInput
                  v-model="subtitleForm.text"
                  type="textarea"
                  :rows="4"
                  maxlength="100000"
                  placeholder="输入需要生成字幕的完整文稿"
                  aria-label="字幕文稿"
                />
              </ElFormItem>
              <div class="two-fields">
                <ElFormItem label="每行最多字数">
                  <ElInputNumber
                    v-model="subtitleForm.maxCharsPerLine"
                    :min="1"
                    :max="200"
                    :step="1"
                    aria-label="字幕每行最多字数"
                  />
                </ElFormItem>
                <ElFormItem label="每条最多行数">
                  <ElInputNumber
                    v-model="subtitleForm.maxLines"
                    :min="1"
                    :max="10"
                    :step="1"
                    aria-label="每条字幕最多行数"
                  />
                </ElFormItem>
              </div>
            </ElForm>
            <ElButton
              class="ai-button"
              type="primary"
              plain
              :loading="subtitleGenerating"
              :disabled="subtitleGenerationDisabled"
              @click="generateSubtitleCandidate"
            >
              生成字幕并加入轨道
            </ElButton>
          </ElCollapseItem>

          <ElCollapseItem name="image">
            <template #title>
              <strong>图片候选</strong>
            </template>
            <ElInput
              v-model="imageForm.prompt"
              type="textarea"
              :rows="3"
              maxlength="100000"
              placeholder="描述主体、构图、光线、品牌色和画面禁忌"
              aria-label="AI 图片提示词"
            />
            <div class="two-fields generator-field">
              <ElSelect v-model="imageForm.role" aria-label="图片素材角色">
                <ElOption label="主画面" value="video" />
                <ElOption label="片头" value="intro" />
                <ElOption label="片尾" value="outro" />
              </ElSelect>
              <ElInputNumber
                v-model="imageForm.count"
                :min="1"
                :max="8"
                aria-label="图片候选数量"
              />
            </div>
            <ElForm class="two-fields generator-field" label-position="top">
              <ElFormItem label="画面比例">
                <ElSelect
                  :model-value="imageForm.ratio"
                  clearable
                  filterable
                  :allow-create="imageCandidateSettings.allowCustomRatio"
                  :default-first-option="imageCandidateSettings.allowCustomRatio"
                  :loading="imageCapabilityLoading"
                  :disabled="!imageCandidateCapability || imageCapabilityLoading"
                  :placeholder="imageCandidateSettings.ratioDefaultLabel"
                  aria-label="图片候选画面比例"
                  @update:model-value="imageForm.ratio = String($event || '').trim()"
                >
                  <ElOption :label="imageCandidateSettings.ratioDefaultLabel" value="" />
                  <ElOption
                    v-for="option in imageCandidateSettings.ratioOptions"
                    :key="option.value"
                    :label="option.label"
                    :value="option.value"
                    :disabled="option.disabled"
                  />
                </ElSelect>
              </ElFormItem>
              <ElFormItem label="输出清晰度">
                <ElSelect
                  :model-value="imageForm.size"
                  clearable
                  filterable
                  :allow-create="imageCandidateSettings.allowCustomSize"
                  :default-first-option="imageCandidateSettings.allowCustomSize"
                  :loading="imageCapabilityLoading"
                  :disabled="!imageCandidateCapability || imageCapabilityLoading"
                  :placeholder="imageCandidateSettings.sizeDefaultLabel"
                  aria-label="图片候选输出清晰度"
                  @update:model-value="imageForm.size = String($event || '').trim().replace(/[X×]/g, 'x')"
                >
                  <ElOption :label="imageCandidateSettings.sizeDefaultLabel" value="" />
                  <ElOption
                    v-for="option in imageCandidateSettings.sizeOptions"
                    :key="option.value"
                    :label="option.label"
                    :value="option.value"
                    :disabled="option.disabled"
                  />
                </ElSelect>
              </ElFormItem>
            </ElForm>
            <p v-if="imageCapabilityError || imageCandidateSettings.error" class="ai-video-reference-feedback is-error" role="alert">
              {{ imageCapabilityError || imageCandidateSettings.error }}
              <ElButton link :loading="imageCapabilityLoading" @click="refreshImageCandidateCapability">重新读取图片参数</ElButton>
            </p>
            <label class="generator-switch">
              <span>允许生成水印</span>
              <ElSwitch v-model="imageForm.watermark" />
            </label>
            <ElButton
              class="ai-button"
              type="primary"
              plain
              :loading="imageGenerating || imageResultRefreshing"
              @click="generateImageCandidates"
            >
              {{ imageGenerating ? `正在生成 ${imageForm.count} 张图片…` : imageGenerationUncertain ? '刷新图片结果' : '生成图片候选' }}
            </ElButton>
            <p v-if="imageGenerating" class="ai-video-reference-feedback" role="status">多张图片需要逐张生成，请等待本批完成。</p>
            <div v-if="imageGenerationUncertain" class="ai-video-reference-feedback" role="status">
              上次请求的结果尚未确认，可能仍在生成。请先查看工程素材，再决定是否生成新一批图片。
              <ElButton link :disabled="imageResultRefreshing" @click="startNewImageBatch">已核对，另生成一批</ElButton>
            </div>
          </ElCollapseItem>

          <ElCollapseItem name="video">
            <template #title>
              <strong>AI 视频生成</strong><span>人工选择参考图片与视频，完成后由你决定是否加入</span>
            </template>
            <ElInput
              v-model="aiVideoForm.prompt"
              type="textarea"
              :rows="3"
              maxlength="10000"
              show-word-limit
              placeholder="描述镜头运动、主体动作、光线、节奏和需要保留的品牌元素"
              aria-label="AI 视频提示词"
            />
            <div class="ai-video-reference-counts" aria-live="polite" aria-atomic="true">
              <strong>参考素材</strong>
              <span>
                参考图片 {{ aiVideoReferenceImageCount }}/{{
                  aiVideoReferenceImageLimit ?? '未声明'
                }}（产品自动 {{ aiVideoProductVisualReferences.length }}，手动
                {{ aiVideoForm.referenceImageAssetIds.length }}，剩余
                {{ aiVideoReferenceImageRemaining ?? '未声明' }}）、参考视频
                {{ aiVideoForm.referenceVideoAssetIds.length }}/{{
                  aiVideoReferenceVideoLimit ?? '未声明'
                }}
              </span>
            </div>
            <div class="ai-video-reference-duration" aria-live="polite">
              参考视频总时长 {{ Number(aiVideoReferenceVideoDuration.toFixed(2)) }}/{{
                aiVideoReferenceVideoDurationLimit ?? '未声明'
              }}
              秒
            </div>
            <div
              v-if="hasAiVideoProductResource && aiVideoProductVisualReferences.length"
              class="ai-video-product-references"
              aria-label="AI 视频产品资料关联参考图片"
            >
              <div
                v-if="aiVideoProductVisualReferences.length"
                class="ai-video-product-reference-list"
              >
                <article
                  v-for="item in aiVideoProductVisualReferences"
                  :key="item.resource_entry_id || item.id"
                  :title="item.name"
                >
                  <ContentLibraryMedia :item="item" :alt="item.name" compact />
                  <span>{{ item.name }}</span>
                </article>
              </div>
            </div>
            <div class="ai-video-transport-status" aria-live="polite">
              <ElTag
                size="small"
                :type="aiVideoProjectAssetTransportAvailable ? 'success' : 'warning'"
                effect="plain"
              >
                {{
                  aiVideoProjectAssetTransportAvailable
                    ? '工程参考视频通道可用'
                    : aiVideoProviderNativeReferenceAssets.length
                      ? '仅同模型 AI 生成视频可直接引用'
                      : '工程参考视频通道待配置'
                }}
              </ElTag>
              <ElButton
                link
                size="small"
                :loading="aiVideoCapabilityLoading || aiVideoCapabilityRefreshing"
                @click="recheckAiVideoCapabilities"
              >
                重新检测
              </ElButton>
            </div>
            <ElForm class="ai-video-reference-fields" label-position="top">
              <ElFormItem label="参考图片">
                <ElSelect
                  :model-value="aiVideoForm.referenceImageAssetIds"
                  multiple
                  filterable
                  clearable
                  collapse-tags
                  collapse-tags-tooltip
                  placeholder="从工程素材中选择"
                  no-data-text="暂无可选图片，请先从左侧导入"
                  aria-label="选择 AI 视频参考图片"
                  :disabled="aiVideoReferenceImageLimit == null || aiVideoReferenceImageLimit === 0"
                  @update:model-value="updateAiVideoImageReferences($event as string[])"
                >
                  <ElOption
                    v-for="asset in aiVideoReferenceImageAssets"
                    :key="asset.id"
                    :label="`${asset.label} · ${projectAssetSourceLabel(asset)}`"
                    :value="asset.id"
                  />
                </ElSelect>
              </ElFormItem>
              <ElFormItem label="参考视频">
                <ElSelect
                  :model-value="aiVideoForm.referenceVideoAssetIds"
                  multiple
                  filterable
                  clearable
                  collapse-tags
                  collapse-tags-tooltip
                  placeholder="从工程素材中选择"
                  no-data-text="暂无可选视频，请先从左侧导入"
                  aria-label="选择 AI 视频参考视频"
                  :disabled="aiVideoReferenceVideoDisabled"
                  @update:model-value="updateAiVideoVideoReferences($event as string[])"
                >
                  <ElOption
                    v-for="asset in aiVideoReferenceVideoAssets"
                    :key="asset.id"
                    :label="aiVideoReferenceVideoOptionLabel(asset)"
                    :value="asset.id"
                    :disabled="!aiVideoReferenceVideoAssetSelectable(asset)"
                  />
                </ElSelect>
              </ElFormItem>
            </ElForm>
            <p
              v-if="aiVideoReferenceFeedback"
              class="ai-video-reference-feedback"
              :class="{ 'is-error': aiVideoReferenceError }"
              :role="aiVideoReferenceError ? 'alert' : 'status'"
              aria-live="assertive"
            >
              {{ aiVideoReferenceFeedback }}
            </p>
            <ElForm class="generator-field ai-video-options" label-position="top">
              <ElFormItem label="时长（秒）">
                <ElSelect v-model="aiVideoForm.durationSeconds" aria-label="AI 视频单段时长">
                  <ElOption
                    v-if="aiVideoDurationError"
                    :label="`${aiVideoForm.durationSeconds} 秒（请重新选择）`"
                    :value="aiVideoForm.durationSeconds"
                    disabled
                  />
                  <ElOption
                    v-for="value in aiVideoDurations"
                    :key="value"
                    :label="`${value} 秒`"
                    :value="value"
                  />
                </ElSelect>
                <span v-if="aiVideoDurationError" class="ai-video-reference-feedback is-error" role="alert">{{ aiVideoDurationError }}</span>
              </ElFormItem>
              <ElFormItem label="生成比例">
                <ElSelect v-model="aiVideoForm.ratio" aria-label="AI 视频生成比例">
                  <ElOption
                    v-for="value in aiVideoRatios"
                    :key="value"
                    :label="aiVideoRatioLabel(value)"
                    :value="value"
                  />
                </ElSelect>
              </ElFormItem>
              <ElFormItem label="生成清晰度">
                <ElSelect v-model="aiVideoForm.resolution" aria-label="AI 视频生成清晰度">
                  <ElOption
                    v-for="value in aiVideoResolutions"
                    :key="value"
                    :label="value"
                    :value="value"
                  />
                </ElSelect>
              </ElFormItem>
            </ElForm>
            <div class="generator-switches">
              <label v-if="aiVideoGenerateAudioSupported"><span>同步生成声音</span><ElSwitch v-model="aiVideoForm.generateAudio" /></label>
              <label v-if="aiVideoReturnLastFrameSupported"><span>保留尾帧（用于续拍）</span><ElSwitch v-model="aiVideoForm.returnLastFrame" /></label>
            </div>
            <div v-if="aiVideoJob && !aiVideoSubmitting" class="inline-job" aria-live="polite">
              <span>{{ mediaJobStatusLabel(aiVideoJob.status) }} · {{ aiVideoJob.progress }}%</span>
              <ElProgress
                :percentage="aiVideoJob.progress"
                :status="
                  aiVideoJob.status === 'failed'
                    ? 'exception'
                    : aiVideoJob.status === 'succeeded'
                      ? 'success'
                      : undefined
                "
              />
              <ElAlert
                v-if="aiVideoJobErrorMessage"
                :title="
                  aiVideoJobNeedsReferenceChange ? '参考图片需要调整' : 'AI 视频生成未完成'
                "
                :description="aiVideoJobErrorMessage"
                type="error"
                :closable="false"
                show-icon
              />
              <ElButton
                v-if="!isTerminalJob(aiVideoJob)"
                link
                type="danger"
                @click="cancelMediaJob(aiVideoJob)"
              >
                取消
              </ElButton>
              <ElButton
                v-else-if="
                  (aiVideoJob.status === 'failed' || aiVideoJob.status === 'cancelled')
                    && !aiVideoJobNeedsReferenceChange
                "
                link
                type="primary"
                :loading="videoGenerating || aiVideoSubmitting"
                :disabled="aiServiceAttemptChecking"
                @click="generateVideoCandidate"
              >
                按当前设置重新生成
              </ElButton>
            </div>
            <div
              v-if="!aiVideoSubmitting && aiVideoJob?.status === 'succeeded'"
              class="ai-video-completed-result"
              aria-live="polite"
            >
              <template v-if="aiVideoCandidateAsset">
                <video
                  v-if="projectAssetUrl(aiVideoCandidateAsset)"
                  :src="projectAssetUrl(aiVideoCandidateAsset)"
                  :aria-label="`预览 ${aiVideoCandidateAsset.label}`"
                  controls
                  muted
                  playsinline
                  preload="metadata"
                />
                <div class="ai-video-result-copy">
                  <div>
                    <strong>视频已生成</strong>
                    <small>{{ aiVideoCandidateAsset.label }}</small>
                  </div>
                  <span class="ai-video-result-actions">
                    <ElButton
                      type="primary"
                      plain
                      size="small"
                      @click="addProjectAssetToTimeline(aiVideoCandidateAsset)"
                    >
                      加入时间线
                    </ElButton>
                    <ElButton
                      plain
                      size="small"
                      @click="downloadProjectAsset(aiVideoCandidateAsset)"
                    >
                      下载
                    </ElButton>
                    <ElButton
                      type="danger"
                      plain
                      size="small"
                      :loading="projectAssetActionBusyId === aiVideoCandidateAsset.id"
                      @click="deleteAiGeneratedAsset(aiVideoCandidateAsset)"
                    >
                      删除
                    </ElButton>
                  </span>
                </div>
              </template>
              <ElAlert
                v-else
                title="视频已经生成，正在加载结果，请稍候或刷新素材。"
                type="info"
                :closable="false"
              />
            </div>
            <ElButton
              class="ai-button"
              type="primary"
              plain
              :loading="videoGenerating || aiVideoSubmitting"
              :disabled="aiServiceAttemptChecking"
              @click="generateVideoCandidate"
            >
              生成视频
            </ElButton>
          </ElCollapseItem>
          <ElCollapseItem name="music">
            <template #title><strong>AI 生成背景音乐（无人声）</strong></template>
            <p class="ai-video-reference-feedback">用于配乐；环境音、碰杯等音效可导入音频后移到背景音效轨。</p>
            <ElInput
              v-model="musicPrompt"
              type="textarea"
              :rows="2"
              maxlength="2000"
              show-word-limit
              placeholder="示例：轻盈克制的东方氛围背景音乐，适合品牌宣传短片，无人声"
              aria-label="AI 背景音乐提示词"
            />
            <div class="two-fields music-options">
              <label>
                <span>时长（秒）</span>
                <ElInputNumber
                  v-model="musicDuration"
                  :min="musicDurationRange.min"
                  :max="musicDurationRange.max"
                  :precision="0"
                  :step="1"
                  aria-label="AI 背景音乐时长"
                />
              </label>
              <label>
                <span>音频格式</span>
                <ElSelect v-model="musicFormat" aria-label="AI 背景音乐格式">
                  <ElOption label="MP3" value="mp3" />
                  <ElOption label="WAV" value="wav" />
                </ElSelect>
              </label>
            </div>
            <p
              v-if="shortMusicNotice"
              class="ai-video-reference-feedback"
              role="status"
              aria-live="polite"
            >
              {{ shortMusicNotice }}
            </p>
            <ElButton
              class="ai-button"
              type="primary"
              plain
              :loading="musicGenerating"
              @click="generateMusic"
            >
              生成背景音乐候选
            </ElButton>
          </ElCollapseItem>
        </ElCollapse>
      </aside>
    </div>

    <section id="video-editor-timeline" class="timeline-panel" aria-label="固定多轨时间线">
      <header class="timeline-toolbar">
        <div class="clip-actions" aria-label="片段操作">
          <ElButton size="small" :disabled="!selectedItem" @click="deleteSelected">删除</ElButton>
          <ElButton size="small" :disabled="!selectedItem" @click="duplicateSelected">
            复制
          </ElButton>
          <ElButton
            v-if="selectedItem && selectedAudioTargetKind"
            size="small"
            data-testid="move-selected-audio-track"
            :disabled="!canMoveSelectedAudioTrack"
            :title="selectedAudioMoveHint"
            :aria-label="selectedAudioMoveHint"
            @click="moveSelectedAudioTrack"
          >
            {{ selectedAudioMoveLabel }}
          </ElButton>
          <ElButton size="small" :disabled="!selectedItem" @click="splitSelected">
            播放头分割
          </ElButton>
          <ElButton
            size="small"
            :disabled="!selectedItem"
            :aria-label="
              selectedTrack?.kind === 'video' ? '将所选片段向前换序' : '将所选片段向左移动'
            "
            @click="moveSelected(-1)"
          >
            {{ selectedTrack?.kind === 'video' ? '上移' : '左移' }}
          </ElButton>
          <ElButton
            size="small"
            :disabled="!selectedItem"
            :aria-label="
              selectedTrack?.kind === 'video' ? '将所选片段向后换序' : '将所选片段向右移动'
            "
            @click="moveSelected(1)"
          >
            {{ selectedTrack?.kind === 'video' ? '下移' : '右移' }}
          </ElButton>
          <span>主画面轨换序；其余轨道 Alt+←/→ 移动时间 · 两侧把手调整长度 · Delete 删除</span>
        </div>
        <div class="timeline-zoom">
          <ElTooltip content="只改变底部时间轴的刻度和片段宽度，不改变成片画面大小" placement="top">
            <span class="timeline-zoom-label">时间轴缩放</span>
          </ElTooltip>
          <ElButton
            circle
            size="small"
            aria-label="缩小时间轴"
            @click="adjustTimelineZoom(-0.1)"
          >
            −
          </ElButton>
          <ElSlider
            v-model="zoom"
            :min="0.5"
            :max="2.5"
            :step="0.1"
            :format-tooltip="(value) => `${Math.round(Number(value) * 100)}%`"
            aria-label="时间线缩放"
          />
          <ElButton
            circle
            size="small"
            aria-label="放大时间轴"
            @click="adjustTimelineZoom(0.1)"
          >
            ＋
          </ElButton>
          <output data-testid="timeline-zoom-value">{{ zoomPercent }}%</output>
          <ElButton
            link
            size="small"
            :disabled="zoom === 1"
            @click="resetTimelineZoom"
          >
            重置
          </ElButton>
        </div>
      </header>
      <div class="timeline-scroll">
        <div class="track-label-column">
          <div class="ruler-label">轨道</div>
          <div v-for="track in visibleTimelineTracks" :key="track.id" class="track-label">
            <strong>{{ timelineTrackName(track.kind, track.name) }}</strong>
            <span>
              <button
                type="button"
                :aria-label="`${track.muted ? '取消静音' : '静音'}${timelineTrackName(track.kind, track.name)}轨道`"
                @click="toggleTrack(track, 'muted')"
              >
                {{ track.muted ? 'M×' : 'M' }}
              </button>
              <button
                type="button"
                :aria-label="`${track.locked ? '解锁' : '锁定'}${timelineTrackName(track.kind, track.name)}轨道`"
                @click="toggleTrack(track, 'locked')"
              >
                {{ track.locked ? '锁' : '开' }}
              </button>
            </span>
          </div>
        </div>
        <div class="tracks-viewport">
          <div
            class="tracks-canvas"
            data-testid="timeline-canvas"
            :style="{
              'width': `${timelineWidth}px`,
              'min-width': '100%',
              '--timeline-grid-size': `${pixelsPerSecond}px`,
            }"
          >
            <div class="time-ruler">
              <span
                v-for="tick in rulerTicks"
                :key="tick"
                :style="{ left: `${tick * pixelsPerSecond}px` }"
              >{{ formatTime(tick) }}</span>
            </div>
            <div
              v-for="track in visibleTimelineTracks"
              :key="track.id"
              class="track-row"
              :class="[`is-${track.kind}`, { 'is-muted': track.muted, 'is-locked': track.locked }]"
              role="listbox"
              :aria-label="`${timelineTrackName(track.kind, track.name)}轨道，共 ${track.items.length} 个片段`"
            >
              <TimelineClip
                v-for="item in track.items"
                :key="item.id"
                :item="item"
                :items="track.items"
                :track-kind="track.kind"
                :track-name="timelineTrackName(track.kind, track.name)"
                :pixels-per-second="pixelsPerSecond"
                :source-duration="projectAssetDurationSeconds(projectAssetFor(item) || item)"
                :selected="selectedItemId === item.id"
                :locked="track.locked"
                @select="selectItem"
                @seek="setPlayhead"
                @reorder="reorderVideoItem"
                @trim="trimTimelineItem"
                @position="positionTimelineItem"
                @resize="resizeTimelineItem"
                @remove="removeTimelineItem"
                @move="moveTimelineItem"
              />
              <span v-if="!track.items.length" class="empty-track">{{
                track.kind === 'video'
                  ? '导入图片或视频后可拖拽换序'
                  : `暂无${timelineTrackName(track.kind, track.name)}片段`
              }}</span>
            </div>
            <button
              type="button"
              class="timeline-playhead"
              :style="{ left: `${playhead * pixelsPerSecond}px` }"
              aria-label="拖动播放头"
              @pointerdown.prevent
            >
              <span />
            </button>
          </div>
        </div>
      </div>
    </section>

    <div class="visually-hidden" aria-live="polite" aria-atomic="true">{{ liveMessage }}</div>

    <ElImageViewer
      v-if="imagePreviewUrl"
      :url-list="[imagePreviewUrl]"
      teleported
      :close-on-press-escape="true"
      @close="imagePreviewUrl = ''"
    />

    <ElDialog v-model="versionsOpen" title="视频工程版本" width="min(820px, 92vw)">
      <ElAlert
        title="恢复历史版本会创建一个新的 revision，不会覆盖或删除当前版本。"
        type="info"
        :closable="false"
        show-icon
      />
      <ElTable
        v-loading="revisionsLoading"
        class="revision-table"
        :data="revisions"
        max-height="440"
      >
        <ElTableColumn label="版本" width="92">
          <template #default="scope">
            <strong>v{{ scope.row.revision ?? '—' }}</strong>
            <ElTag v-if="scope.row.id === project?.current_revision_id" size="small" type="success">
              当前
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="来源" width="128">
          <template #default="scope">{{ revisionSourceLabel(scope.row.source) }}</template>
        </ElTableColumn>
        <ElTableColumn prop="created_by" label="创建人" min-width="130" />
        <ElTableColumn label="创建时间" min-width="190">
          <template #default="scope">{{ formatDateTime(scope.row.created_at) }}</template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="118" align="right">
          <template #default="scope">
            <ElButton
              size="small"
              :disabled="scope.row.id === project?.current_revision_id"
              :loading="restoringRevisionId === scope.row.id"
              @click="restoreRevision(scope.row)"
            >
              恢复此版本
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
      <ElEmpty v-if="!revisionsLoading && !revisions.length" description="暂无可用工程版本" />
      <template #footer><ElButton @click="versionsOpen = false">关闭</ElButton></template>
    </ElDialog>

    <ElDialog
      v-model="exactPreviewOpen"
      title="服务端精确预览"
      width="min(920px, 94vw)"
      destroy-on-close
    >
      <ElAlert
        title="精确预览锁定当前 revision，由服务端按真实转场、字幕和音频渲染；不会改变时间线。"
        type="info"
        :closable="false"
        show-icon
      />
      <div v-if="exactPreviewJob" class="exact-preview-job" aria-live="polite">
        <div>
          <strong>{{ mediaJobStatusLabel(exactPreviewJob.status) }}</strong>
          <span>尝试 {{ exactPreviewJob.attempt }}/{{
            exactPreviewJob.max_attempts
          }}</span>
        </div>
        <ElProgress
          :percentage="exactPreviewJob.progress"
          :status="
            exactPreviewJob.status === 'failed'
              ? 'exception'
              : exactPreviewJob.status === 'succeeded'
                ? 'success'
                : undefined
          "
        />
        <ElAlert
          v-if="exactPreviewJob.error || exactPreviewJob.status === 'failed'"
          :title="
            formatContentGenerationError(
              exactPreviewJob,
              '精确预览生成失败，请检查时间线中的素材是否仍可用后重新生成',
            )
          "
          type="error"
          :closable="false"
          show-icon
        />
      </div>
      <div class="exact-preview-stage">
        <video
          v-if="exactPreviewUrl"
          :src="exactPreviewUrl"
          controls
          playsinline
          aria-label="服务端精确预览视频"
        >
          <track
            v-if="exactPreviewSubtitleUrl"
            :key="exactPreviewSubtitleUrl"
            kind="subtitles"
            label="字幕"
            srclang="zh"
            :src="exactPreviewSubtitleUrl"
            default
          >
        </video>
        <ElEmpty
          v-else
          :description="
            exactPreviewBusy
              ? '服务端正在渲染精确预览…'
              : exactPreviewJob?.status === 'failed'
                ? '渲染失败，请检查时间线素材后重新生成'
                : '尚无精确预览文件'
          "
        />
      </div>
      <template #footer>
        <ElButton
          v-if="exactPreviewJob && !isTerminalJob(exactPreviewJob)"
          type="danger"
          plain
          @click="cancelMediaJob(exactPreviewJob)"
        >
          取消任务
        </ElButton>
        <ElButton
          v-if="
            exactPreviewJob
              && (exactPreviewJob.status === 'failed' || exactPreviewJob.status === 'cancelled')
          "
          @click="retryMediaJob(exactPreviewJob)"
        >
          重试
        </ElButton>
        <ElButton
          v-if="exactPreviewJob?.status === 'succeeded'"
          @click="downloadMediaJob(exactPreviewJob)"
        >
          下载预览
        </ElButton>
        <ElButton type="primary" @click="exactPreviewOpen = false">关闭</ElButton>
      </template>
    </ElDialog>

    <ElDrawer v-model="exportDrawerOpen" title="导出视频" size="min(520px, 94vw)">
      <section class="export-section" aria-labelledby="export-profile-title">
        <header>
          <strong id="export-profile-title">导出成片</strong>
          <small>输出带画面、声音和字幕的 MP4 视频</small>
        </header>
        <div class="selected-export-option" aria-label="当前可导出的内容">
          <span aria-hidden="true">✓</span>
          <div>
            <strong>成片视频（MP4）</strong>
            <small>{{ exportVideoProfile }} · 清晰度按当前主视频素材自动确定，不进行虚假放大</small>
          </div>
        </div>
        <ElButton type="primary" :loading="exporting" @click="submitProjectExport">
          导出视频
        </ElButton>
        <div v-if="exportJob" class="current-export-job" aria-live="polite">
          <div>
            <span>{{ mediaJobStatusLabel(exportJob.status) }}</span>
            <strong>{{ exportJob.progress }}%</strong>
          </div>
          <ElProgress
            :percentage="exportJob.progress"
            :status="
              exportJob.status === 'failed'
                ? 'exception'
                : exportJob.status === 'succeeded'
                  ? 'success'
                  : undefined
            "
          />
          <p v-if="exportJob.error || exportJob.status === 'failed'" class="job-error">
            {{
              formatContentGenerationError(
                exportJob,
                '视频导出失败，请检查主画面、音频和字幕素材后重新导出',
              )
            }}
          </p>
          <ElButton
            v-if="exportJob.status === 'succeeded'"
            link
            type="primary"
            @click="downloadMediaJob(exportJob)"
          >
            再次下载
          </ElButton>
          <ElButton
            v-else-if="!isTerminalJob(exportJob)"
            link
            type="danger"
            @click="cancelMediaJob(exportJob)"
          >
            取消导出
          </ElButton>
          <ElButton
            v-else-if="exportJob.status === 'failed' || exportJob.status === 'cancelled'"
            link
            type="primary"
            @click="retryMediaJob(exportJob)"
          >
            重新导出
          </ElButton>
        </div>
      </section>
    </ElDrawer>
  </section>
</template>

<style scoped>
.video-editor {
  --editor-brand: var(--xi-semantic-color-action-primary-default);
  --editor-brand-deep: var(--xi-semantic-color-action-primary-active);
  --editor-brand-soft: var(--xi-semantic-color-background-brand-light);
  --editor-line: var(--xi-semantic-color-border-subtle);
  --editor-page: var(--xi-semantic-color-background-page);
  --editor-panel: var(--xi-semantic-color-background-container);
  --editor-text: var(--xi-semantic-color-text-primary);
  --editor-muted: var(--xi-semantic-color-text-secondary);
  display: grid;
  width: 100%;
  max-width: 100%;
  height: calc(100dvh - 80px);
  min-width: 0;
  min-height: 960px;
  overflow-x: hidden;
  overflow-y: auto;
  container: video-editor-layout / inline-size;
  color: var(--editor-text);
  background: var(--editor-page);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: minmax(58px, auto) auto minmax(480px, 1fr) clamp(330px, 36vh, 380px);
}

.skip-link {
  position: fixed;
  z-index: 1000;
  top: 8px;
  left: 8px;
  padding: 8px 12px;
  color: var(--xi-semantic-color-text-inverse);
  background: var(--editor-brand-deep);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
  transform: translateY(-160%);
}

.skip-link:focus {
  transform: translateY(0);
}

.editor-toolbar,
.toolbar-title,
.toolbar-history,
.toolbar-status,
.toolbar-actions,
.preview-heading,
.preview-controls,
.timeline-toolbar,
.clip-actions,
.timeline-zoom,
.track-label,
.track-label span,
.compare-card header,
.compare-card dl,
.compare-card dl div {
  display: flex;
  align-items: center;
}

.editor-toolbar {
  z-index: 3;
  min-width: 0;
  grid-row: 1;
  gap: 16px;
  padding: 8px 12px;
  background: var(--editor-panel);
  border-bottom: 1px solid var(--editor-line);
}

.editor-toolbar > *,
.editor-panels > * {
  min-width: 0;
}

.toolbar-title {
  min-width: 0;
  flex: 0 1 230px;
  gap: 12px;
}

.toolbar-title > div,
.panel-heading > div,
.preview-heading > div {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.toolbar-title strong,
.toolbar-title small,
.import-list strong,
.import-list small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.toolbar-title small,
.panel-heading small,
.preview-heading small,
.toolbar-status span,
.clip-actions > span,
.empty-copy,
.empty-track {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.toolbar-history {
  gap: 4px;
}

.toolbar-status {
  min-width: 0;
  flex: 1;
  gap: 8px;
}

.toolbar-status span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.toolbar-actions {
  min-width: 0;
  flex: 0 0 auto;
  gap: 8px;
  margin-left: auto;
  white-space: nowrap;
}

.editor-error {
  grid-row: 2;
  margin: 8px 10px 0;
}

.editor-panels {
  display: grid;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 480px;
  overflow: hidden;
  grid-row: 3;
  grid-template-columns: 286px minmax(360px, 1fr) 304px;
}

.editor-sidebar,
.preview-panel {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  background: var(--editor-panel);
}

.editor-sidebar {
  padding: 12px;
}

.editor-left {
  border-right: 1px solid var(--editor-line);
}

.editor-right {
  border-left: 1px solid var(--editor-line);
  overflow-wrap: anywhere;
}

.editor-right :deep(.el-alert__content) {
  min-width: 0;
}

.panel-heading,
.preview-heading {
  margin-bottom: 12px;
}

.panel-heading strong,
.preview-heading strong {
  font-size: var(--xi-semantic-typography-body-font-size);
}

.material-heading,
.ai-heading {
  padding-top: 12px;
  margin-top: 4px;
  border-top: 1px solid var(--editor-line);
}

.workflow-current-content,
.current-text-card {
  display: grid;
  min-width: 0;
}

.current-text-card {
  gap: 8px;
  padding: 12px;
  overflow: hidden;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.current-text-card header {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.current-text-card header small {
  overflow-wrap: anywhere;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  text-align: right;
}

.current-text-card h4,
.current-text-card p {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.current-text-card h4 {
  font-size: var(--xi-semantic-typography-body-font-size);
  line-height: 1.5;
}

.current-text-card p {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.65;
}

.compact-form :deep(.product-picker),
.compact-form :deep(.el-cascader) {
  width: 100%;
  min-width: 0;
}

.compact-form :deep(.selected-product) {
  grid-template-columns: 48px minmax(0, 1fr);
}

.compact-form :deep(.selected-product-cover) {
  height: 48px;
}

.compact-form :deep(.selected-product > .el-button) {
  grid-column: 1 / -1;
  justify-self: start;
  margin-left: 0;
}

.compact-form :deep(.el-form-item) {
  margin-bottom: 12px;
}

.compact-form :deep(.el-form-item__label) {
  height: auto;
  padding-bottom: 4px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.4;
}

.two-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.two-fields :deep(.el-form-item),
.two-fields :deep(.el-form-item__content) {
  min-width: 0;
}

.two-fields :deep(.el-select),
.two-fields :deep(.el-input-number) {
  width: 100%;
  min-width: 0;
}

.audio-role-label {
  display: block;
  margin-bottom: 4px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.import-role-fields {
  margin-bottom: 8px;
}

.import-role-fields .el-select {
  width: 100%;
}

.upload-dropzone {
  display: grid;
  width: 100%;
  min-height: 72px;
  padding: 12px;
  place-content: center;
  gap: 4px;
  color: var(--editor-brand-deep);
  text-align: center;
  cursor: pointer;
  background: var(--editor-brand-soft);
  border: 1px dashed var(--xi-semantic-color-border-brand-strong);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.upload-dropzone span {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.import-list {
  display: grid;
  gap: 8px;
  margin-top: 8px;
}

.import-item {
  position: relative;
  display: grid;
  overflow: hidden;
  background: var(--xi-semantic-color-background-container);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.import-select {
  display: grid;
  width: 100%;
  min-width: 0;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  padding: 8px;
  color: inherit;
  text-align: left;
  cursor: pointer;
  background: var(--xi-semantic-color-background-container);
  border: 0;
}

.import-select:disabled {
  cursor: default;
}

.import-select > span:nth-child(2) {
  display: grid;
  min-width: 0;
}

.import-item :deep(.el-progress) {
  margin: 0 42px 4px 6px;
}

.cancel-import {
  position: absolute;
  right: 5px;
  bottom: 2px;
  padding: 1px 4px;
  color: var(--xi-semantic-color-feedback-danger-text);
  font-size: var(--xi-semantic-typography-caption-font-size);
  cursor: pointer;
  background: var(--xi-semantic-color-feedback-danger-background-subtle);
  border: 1px solid var(--xi-semantic-color-feedback-danger-border);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
}

.asset-kind {
  display: grid;
  width: 34px;
  height: 30px;
  place-items: center;
  color: var(--editor-brand-deep);
  font-size: var(--xi-semantic-typography-caption-font-size);
  background: var(--editor-brand-soft);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
}

.empty-copy {
  margin: 6px 0;
  text-align: center;
}

.generated-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.material-library-tools {
  display: flex;
  flex: none;
  align-items: center;
  gap: 4px;
}

.enterprise-library-entry {
  display: grid;
  gap: 8px;
  padding: 8px;
  margin-bottom: 8px;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--xi-semantic-color-border-brand-subtle);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.enterprise-library-entry > small {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.4;
}

.enterprise-library-entry :deep(.resource-picker-action) {
  justify-content: space-between;
}

.enterprise-library-entry :deep(.resource-picker-action > span) {
  text-align: right;
}

.generated-assets {
  display: grid;
  gap: 8px;
}

.generated-asset-card {
  display: grid;
  min-width: 0;
  padding: 8px;
  grid-template-columns: 64px minmax(0, 1fr) auto;
  grid-template-areas:
    'preview copy state'
    'actions actions actions';
  gap: 8px;
  background: var(--xi-semantic-color-background-container);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.generated-asset-preview {
  display: grid;
  width: 64px;
  height: 48px;
  grid-area: preview;
  place-items: center;
  overflow: hidden;
  color: var(--editor-brand-deep);
  font-size: var(--xi-semantic-typography-heading2-font-size);
  font-weight: 800;
  background: var(--editor-brand-soft);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
}

.generated-asset-preview img,
.generated-asset-preview video {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.generated-asset-preview img {
  object-fit: contain;
}

.image-preview-trigger {
  cursor: zoom-in;
}

.image-preview-trigger:focus-visible {
  outline: 2px solid var(--editor-brand-deep);
  outline-offset: -2px;
}

.generated-asset-preview audio {
  width: 62px;
  height: 30px;
}

.generated-asset-copy {
  display: grid;
  min-width: 0;
  align-content: start;
  grid-area: copy;
  gap: 4px;
}

.generated-asset-copy strong,
.generated-asset-copy small,
.generated-asset-copy p {
  overflow: hidden;
  text-overflow: ellipsis;
}

.generated-asset-copy strong,
.generated-asset-copy small {
  white-space: nowrap;
}

.generated-asset-copy small,
.generated-asset-copy p {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.generated-asset-copy p {
  display: -webkit-box;
  margin: 1px 0 0;
  line-height: 1.3;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.generated-asset-state {
  display: grid;
  min-width: 68px;
  justify-items: end;
  align-content: start;
  grid-area: state;
  gap: 4px;
}

.generated-asset-actions :deep(.el-button) {
  min-width: 0;
  margin-left: 0;
  padding-inline: 2px;
  font-size: var(--xi-semantic-typography-caption-font-size);
  flex-shrink: 0;
}

.generated-asset-actions {
  display: flex;
  min-width: 0;
  flex-wrap: nowrap;
  justify-content: flex-end;
  grid-area: actions;
  gap: 2px;
  padding-top: 8px;
  border-top: 1px solid var(--xi-semantic-color-divider-default);
}

.preview-panel {
  display: grid;
  min-width: 0;
  padding: 12px 16px;
  background: var(--xi-semantic-color-background-page);
  grid-template-rows: auto minmax(0, 1fr) auto auto auto;
}

.preview-heading {
  justify-content: space-between;
  gap: 12px;
}

.preview-badges {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.preview-stage-wrap {
  display: grid;
  min-height: 0;
  padding: 12px;
  place-items: center;
  overflow: hidden;
  background: var(--xi-semantic-color-background-page);
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.preview-stage {
  position: relative;
  display: grid;
  flex: none;
  overflow: hidden;
  place-items: center;
  color: var(--xi-semantic-color-text-inverse);
  background: var(--xi-semantic-color-background-inverse);
  border: 1px solid var(--xi-semantic-color-border-strong);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
  transition:
    width 180ms ease,
    height 180ms ease;
}

.preview-stage video,
.preview-stage img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.preview-visual-layer {
  position: absolute;
  inset: 0;
  background: var(--xi-semantic-color-background-inverse);
}

.property-hint {
  margin: 0 0 10px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.5;
}

.preview-empty {
  display: grid;
  place-items: center;
  gap: 8px;
  color: var(--xi-semantic-color-text-inverse);
}

.preview-empty span {
  font-size: 20px;
  font-weight: 700;
}

.preview-empty small {
  color: var(--xi-semantic-color-text-tertiary);
}

.preview-overlays,
.preview-subtitles {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.preview-overlay-item {
  position: absolute;
}

.preview-overlay-item span {
  display: block;
  padding: 8px 8px;
  color: var(--xi-semantic-color-text-inverse);
  font-size: var(--xi-semantic-typography-body-font-size);
  background: var(--xi-semantic-color-background-brand-section);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
}

.preview-overlay-item img,
.preview-overlay-item video {
  display: block;
  width: 100%;
  height: auto;
  object-fit: contain;
}

.preview-subtitles {
  display: block;
  padding: 0;
}

.preview-controls {
  gap: 12px;
  padding-top: 12px;
}

.preview-controls > span {
  flex: none;
  color: var(--editor-muted);
  font-variant-numeric: tabular-nums;
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.preview-controls .el-slider {
  flex: 1;
}

.active-audio {
  display: flex;
  gap: 8px;
  padding-top: 8px;
  overflow: hidden;
}

.active-audio span {
  overflow: hidden;
  padding: 4px 8px;
  color: var(--editor-brand-deep);
  font-size: var(--xi-semantic-typography-caption-font-size);
  text-overflow: ellipsis;
  white-space: nowrap;
  background: var(--editor-brand-soft);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
}

.preview-audio-element {
  display: none;
}

.ai-button {
  width: 100%;
  margin-top: 8px;
}

.ai-candidate-results {
  display: grid;
  gap: 8px;
  margin-bottom: 8px;
}

.ai-candidate-results article {
  display: grid;
  min-width: 0;
  grid-template-columns: 30px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  padding: 8px;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.ai-candidate-results article > div {
  display: grid;
  min-width: 0;
}

.ai-candidate-results strong,
.ai-candidate-results small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ai-candidate-results small,
.more-ai-candidates {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.ai-candidate-actions {
  display: flex;
  min-width: 0;
  max-width: 144px;
  flex-wrap: wrap;
  align-items: center;
}

.ai-candidate-actions .el-button + .el-button {
  margin-left: 3px;
}

.ai-generator-collapse {
  border-color: var(--editor-line);
}

.ai-generator-collapse :deep(.el-collapse-item__header) {
  height: 36px;
  min-width: 0;
  color: var(--editor-text);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.ai-generator-collapse :deep(.el-collapse-item__header strong) {
  flex: none;
}

.ai-generator-collapse :deep(.el-collapse-item__header span) {
  min-width: 0;
  margin-left: 6px;
  overflow: hidden;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  font-weight: 400;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ai-generator-collapse :deep(.el-collapse-item__content) {
  padding-bottom: 12px;
}

.generator-field {
  width: 100%;
  margin-top: 7px;
}

.generator-field .el-input-number,
.generator-field .el-select {
  width: 100%;
}

.subtitle-generator-form .el-input-number,
.subtitle-generator-form .el-select {
  width: 100%;
}

.ai-video-completed-result {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  padding: 12px;
  margin-bottom: 8px;
  color: var(--editor-text);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--xi-semantic-color-border-brand-subtle);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.ai-video-completed-result video {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  max-height: 220px;
  background: var(--xi-semantic-color-background-inverse);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.ai-video-result-copy {
  display: flex;
  flex-wrap: wrap;
  min-width: 0;
  gap: var(--xi-primitive-space-8);
}

.ai-video-result-copy > div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}

.ai-video-result-copy > span,
.ai-video-result-copy > span {
  flex-wrap: wrap;
  max-width: 100%;
}

.ai-video-result-actions {
  display: flex;
  width: 100%;
  min-width: 0;
  flex-wrap: nowrap;
  align-items: center;
  gap: var(--xi-primitive-space-8);
}

.ai-video-result-actions :deep(.el-button),
.ai-video-result-actions :deep(.el-button--small),
.ai-video-result-actions :deep(.el-button + .el-button) {
  box-sizing: border-box;
  flex: 1 1 auto;
  min-width: 0;
  height: var(--el-component-size-small);
  padding: 0 var(--xi-primitive-space-8);
  margin-left: 0;
  overflow: hidden;
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1;
  white-space: nowrap;
}

.ai-video-result-actions :deep(.el-button > span) {
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1;
}

.ai-video-result-copy strong,
.ai-video-result-copy small {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ai-video-result-copy strong {
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.ai-video-result-copy small {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.ai-video-reference-counts {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 8px;
  margin-top: 8px;
  color: var(--editor-text);
  font-size: var(--xi-semantic-typography-caption-font-size);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--xi-semantic-color-border-brand-subtle);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.ai-video-reference-counts span {
  color: var(--editor-muted);
  text-align: right;
}

.ai-video-reference-duration {
  margin-top: 4px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  text-align: right;
}

.ai-video-product-references {
  display: grid;
  gap: 8px;
  padding: 8px 8px;
  margin-top: 7px;
  color: var(--editor-text);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--xi-semantic-color-border-brand-subtle);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.ai-video-product-references > div:first-child {
  display: grid;
  gap: 4px;
}

.ai-video-product-references small,
.ai-video-product-reference-list span {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.ai-video-product-reference-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 8px;
}

.ai-video-product-reference-list article {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.ai-video-product-reference-list :deep(.library-media) {
  height: 52px;
  min-height: 52px;
  background: var(--xi-semantic-color-background-container);
}

.ai-video-product-reference-list span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ai-video-transport-status {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 5px;
}

.ai-video-transport-status :deep(.el-tag) {
  max-width: 100%;
  min-height: 24px;
  height: auto;
  padding-block: 3px;
  line-height: 18px;
  white-space: normal;
}

.ai-video-transport-status :deep(.el-tag__content) {
  min-width: 0;
  white-space: normal;
  overflow-wrap: anywhere;
}

.ai-video-transport-status > .el-button {
  flex: none;
  margin-left: auto;
}

.ai-video-reference-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 7px;
}

.ai-video-reference-fields :deep(.el-form-item) {
  min-width: 0;
  margin-bottom: 0;
}

.ai-video-reference-fields :deep(.el-form-item__label) {
  height: auto;
  padding: 0 0 4px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.3;
}

.ai-video-reference-fields :deep(.el-select) {
  width: 100%;
}

.ai-video-reference-feedback {
  padding: 8px 8px;
  margin: 7px 0 0;
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.45;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--xi-semantic-color-border-brand-subtle);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.ai-video-reference-feedback.is-error {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  border-color: var(--el-color-danger-light-7);
}

.ai-video-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.ai-video-options :deep(.el-form-item) {
  min-width: 0;
  margin-bottom: 0;
}

.ai-video-options :deep(.el-form-item__label) {
  height: auto;
  min-height: 22px;
  white-space: nowrap;
  padding: 0 0 4px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.3;
  align-items: flex-end;
}

.ai-video-options :deep(.el-select__selected-item) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.generator-switch,
.generator-switches,
.generator-switches label {
  display: flex;
  align-items: center;
}

.generator-switch,
.generator-switches {
  justify-content: space-between;
  gap: 8px;
  margin-top: 7px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.generator-switches label {
  gap: 8px;
}

.inline-job {
  display: grid;
  margin-top: 7px;
  padding: 8px;
  gap: 4px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.music-options {
  margin-top: 8px;
}

.music-options label {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.music-options span {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.music-options .el-input-number,
.music-options .el-select {
  width: 100%;
}

.timeline-panel {
  display: grid;
  min-width: 0;
  min-height: 330px;
  grid-row: 4;
  background: var(--xi-semantic-color-background-brand-subtle);
  border-top: 1px solid var(--editor-line);
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: minmax(42px, auto) minmax(0, 1fr);
}

.timeline-toolbar {
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  background: var(--xi-semantic-color-background-container);
  border-bottom: 1px solid var(--editor-line);
}

.clip-actions {
  min-width: 0;
  gap: 4px;
}

.clip-actions > span {
  margin-left: 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.timeline-zoom {
  width: 360px;
  flex: none;
  gap: 8px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.timeline-zoom-label,
.timeline-zoom output {
  flex: none;
  white-space: nowrap;
}

.timeline-zoom output {
  width: 38px;
  color: var(--editor-brand-deep);
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  text-align: right;
}

.timeline-zoom .el-button + .el-button {
  margin-left: 0;
}

.timeline-zoom .el-slider {
  flex: 1;
}

.timeline-scroll {
  display: grid;
  min-height: 0;
  overflow: hidden;
  grid-template-columns: 176px minmax(0, 1fr);
}

.track-label-column {
  z-index: 2;
  background: var(--xi-semantic-color-background-brand-subtle);
  border-right: 1px solid var(--editor-line);
}

.ruler-label,
.time-ruler {
  height: 24px;
  border-bottom: 1px solid var(--editor-line);
}

.ruler-label {
  display: grid;
  place-items: center;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.track-label {
  justify-content: space-between;
  height: 38px;
  padding: 0 8px 0 12px;
  border-bottom: 1px solid var(--editor-line);
}

.track-label strong {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.track-label span {
  flex-shrink: 0;
  gap: 4px;
}

.track-label button {
  display: grid;
  width: var(--el-component-size-small);
  height: var(--el-component-size-small);
  padding: 0;
  place-items: center;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  cursor: pointer;
  background: var(--xi-semantic-color-background-container);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
}

.tracks-viewport {
  min-height: 0;
  overflow: auto;
}

.tracks-canvas {
  position: relative;
  min-height: 252px;
  background-image: linear-gradient(to right, var(--xi-semantic-color-border-brand-subtle) 1px, transparent 1px);
  background-size: var(--timeline-grid-size, 42px) 100%;
}

.time-ruler {
  position: relative;
  background: var(--xi-semantic-color-background-container);
}

.time-ruler span {
  position: absolute;
  top: 4px;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  font-variant-numeric: tabular-nums;
  transform: translateX(3px);
}

.track-row {
  position: relative;
  height: 38px;
  overflow: hidden;
  border-bottom: 1px solid var(--editor-line);
}

.track-row.is-muted {
  opacity: 0.55;
}

.track-row.is-locked {
  background: repeating-linear-gradient(
    135deg,
    transparent 0,
    transparent 8px,
    var(--xi-semantic-color-background-page) 8px,
    var(--xi-semantic-color-background-page) 16px
  );
}

.timeline-item {
  position: absolute;
  top: 3px;
  display: block;
  height: 31px;
  min-width: 0;
  padding: 0;
  overflow: hidden;
  color: var(--xi-semantic-color-text-brand);
  text-align: left;
  cursor: pointer;
  background: var(--xi-semantic-color-background-brand-light);
  border: 1px solid var(--xi-semantic-color-border-brand-strong);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
}

.is-overlay .timeline-item {
  color: var(--xi-semantic-color-feedback-warning-text);
  background: var(--xi-semantic-color-feedback-warning-background-light);
  border-color: var(--xi-semantic-color-feedback-warning-border);
}
.is-subtitle .timeline-item {
  color: var(--xi-semantic-color-extension-purple-text);
  background: var(--xi-semantic-color-extension-purple-background-subtle);
  border-color: var(--xi-semantic-color-extension-purple-border);
}
.is-voice .timeline-item {
  color: var(--xi-semantic-color-extension-sky-text);
  background: var(--xi-semantic-color-extension-sky-background-subtle);
  border-color: var(--xi-semantic-color-extension-sky-border);
}
.is-music .timeline-item {
  color: var(--xi-semantic-color-extension-purple-text);
  background: var(--xi-semantic-color-extension-purple-background-subtle);
  border-color: var(--xi-semantic-color-extension-purple-border);
}
.is-sfx .timeline-item {
  color: var(--xi-semantic-color-extension-orange-text);
  background: var(--xi-semantic-color-extension-orange-background-subtle);
  border-color: var(--xi-semantic-color-extension-orange-border);
}

.timeline-item.selected {
  z-index: 2;
  outline: 2px solid var(--editor-brand-deep);
  outline-offset: 1px;
}

.timeline-item.disabled {
  filter: grayscale(1);
  opacity: 0.5;
}

.timeline-item strong,
.timeline-item small {
  overflow: hidden;
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.1;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.timeline-item small {
  opacity: 0.75;
}

.empty-track {
  position: absolute;
  top: 10px;
  left: 12px;
}

.timeline-playhead {
  position: absolute;
  z-index: 4;
  top: 0;
  bottom: 0;
  width: 1px;
  padding: 0;
  cursor: ew-resize;
  background: var(--xi-semantic-color-feedback-danger-default);
  border: 0;
}

.timeline-playhead span {
  position: absolute;
  top: 0;
  left: -5px;
  width: 11px;
  height: 8px;
  background: var(--xi-semantic-color-feedback-danger-default);
  clip-path: polygon(0 0, 100% 0, 50% 100%);
}

.compare-layout {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin: 14px 0;
}

.revision-table {
  margin-top: 12px;
}

.revision-table strong {
  margin-right: 6px;
  font-variant-numeric: tabular-nums;
}

.exact-preview-job {
  display: grid;
  margin: 12px 0;
  gap: 8px;
}

.exact-preview-job > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.exact-preview-job span {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.exact-preview-stage {
  display: grid;
  min-height: 360px;
  place-items: center;
  overflow: hidden;
  background: var(--xi-semantic-color-background-inverse);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.exact-preview-stage video {
  width: 100%;
  max-height: 62vh;
}

.exact-preview-stage :deep(.el-empty__description p) {
  color: var(--xi-semantic-color-text-inverse);
}

.export-section {
  padding-bottom: 18px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--editor-line);
}

.export-section > header {
  display: flex;
  align-items: center;
}

.export-section > header {
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.export-section > header small {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.selected-export-option,
.current-export-job {
  display: grid;
  padding: 12px;
  margin-bottom: 12px;
  gap: 8px;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.selected-export-option {
  grid-template-columns: 24px 1fr;
  align-items: center;
}

.selected-export-option > span {
  display: grid;
  width: 22px;
  height: 22px;
  place-items: center;
  color: var(--xi-semantic-color-text-inverse);
  background: var(--editor-brand);
  border-radius: 50%;
}

.selected-export-option div,
.current-export-job > div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}

.selected-export-option div {
  flex-direction: column;
}

.selected-export-option small {
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.job-error {
  margin: 0;
  color: var(--xi-semantic-color-feedback-danger-text);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.compare-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
  padding: 8px 12px;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.compare-controls > span {
  flex: none;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  font-variant-numeric: tabular-nums;
}

.compare-controls .el-slider {
  flex: 1;
}

.compare-card {
  padding: 12px;
  background: var(--xi-semantic-color-background-page);
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.compare-card.candidate {
  background: var(--editor-brand-soft);
  border-color: var(--xi-semantic-color-border-brand-default);
}

.compare-card header,
.compare-card dl {
  justify-content: space-between;
  gap: 12px;
}

.compare-canvas {
  display: grid;
  height: 180px;
  margin: 10px 0;
  place-items: center;
  color: var(--xi-semantic-color-text-inverse);
  font-size: var(--xi-semantic-typography-heading1-font-size);
  font-weight: 800;
  letter-spacing: 0.08em;
  background: var(--xi-semantic-color-background-inverse);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.compare-canvas video,
.compare-canvas img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.compare-canvas > span {
  overflow: hidden;
  padding: 12px;
  font-size: var(--xi-semantic-typography-heading2-font-size);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.compare-frame-label {
  margin: -4px 0 8px;
  overflow: hidden;
  color: var(--editor-muted);
  font-size: var(--xi-semantic-typography-caption-font-size);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.compare-card dl {
  margin: 0;
}

.compare-card dl div {
  gap: 8px;
}

.compare-card dt {
  color: var(--editor-muted);
}

.compare-card dd {
  margin: 0;
  font-weight: 650;
}

.proposal-operations {
  padding: 12px;
  border: 1px solid var(--editor-line);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.proposal-operations legend {
  padding: 0 8px;
  font-weight: 650;
}

.proposal-operations .el-checkbox-group {
  display: grid;
  gap: 8px;
}

.visually-hidden {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  white-space: nowrap !important;
  border: 0 !important;
}

.video-editor :is(button, a, input, textarea, [tabindex]):focus-visible {
  outline: 2px solid var(--xi-semantic-color-border-brand-default);
  outline-offset: 2px;
}

@media (max-width: 1280px) {
  .editor-panels {
    grid-template-columns: 250px minmax(320px, 1fr) 280px;
  }

  .toolbar-status span,
  .clip-actions > span {
    display: none;
  }
}

/* Respond to the editor's available space, including the app sidebar and zoom. */
@container video-editor-layout (max-width: 950px) {
  .editor-panels {
    grid-template-columns: 200px minmax(220px, 1fr) 280px;
  }

  .editor-toolbar,
  .toolbar-actions,
  .timeline-toolbar,
  .clip-actions,
  .preview-heading,
  .preview-badges {
    flex-wrap: wrap;
  }

  .toolbar-actions {
    max-width: 100%;
  }

  .preview-heading > div {
    flex: 1 0 100%;
  }

  .preview-badges {
    justify-content: flex-start;
  }

  .timeline-zoom {
    min-width: 0;
    max-width: 100%;
    margin-left: auto;
  }

  .clip-actions > span {
    display: none;
  }
}

@container video-editor-layout (max-width: 700px) {
  .editor-panels {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-rows: repeat(2, minmax(240px, 1fr));
  }

  .preview-panel {
    grid-column: 1 / -1;
    grid-row: 1;
  }

  .editor-left {
    grid-column: 1;
    grid-row: 2;
  }

  .editor-right {
    grid-column: 2;
    grid-row: 2;
  }
}

@container video-editor-layout (max-width: 500px) {
  .editor-panels {
    overflow: auto;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: repeat(3, minmax(240px, 1fr));
  }

  .editor-left,
  .editor-right {
    grid-column: 1;
  }

  .editor-left {
    grid-row: 1;
  }

  .preview-panel {
    grid-row: 2;
  }

  .editor-right {
    grid-row: 3;
  }
}

@media (prefers-reduced-motion: reduce) {
  .video-editor *,
  .video-editor *::before,
  .video-editor *::after {
    scroll-behavior: auto !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
