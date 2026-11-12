<script setup lang="ts">
import type { ImageGenerationValue } from './ImageGenerationControls.vue'
import type { WorkflowStageBaseline } from './video-editor/workflowFinalization'
import type {
  Artifact,
  ArtifactContent,
  ComplianceResult,
  ContentCapabilities,
  ContentLength,
  ContentServiceStatus,
  ContentTask,
  LibraryItem,
  LibraryResourceKind,
  ProviderCapability,
  StageKey,
  StoryboardShot,
  TaskMode,
  TaskWorkflow,
} from '@/api/agent/content'
import type { AgentJob } from '@/api/agent/types'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { contentApi } from '@/api/agent/content'
import { ContentGenerationError, formatContentGenerationError } from '@/api/agent/contentGenerationErrors'
import { saveBlob } from '@/api/agent/http'
import { beginVideoEditorRouteHandoff, contentCreateViewKey } from '@/router/videoEditorRouteSession'
import {
  createLatestSelectionQueue,
  nextArtifactSelection,
} from './artifactSelectionQueue'
import ContentArtifactCard from './ContentArtifactCard.vue'
import ContentArtifactEditor from './ContentArtifactEditor.vue'
import ContentKnowledgePicker from './ContentKnowledgePicker.vue'
import ContentMediaUploader from './ContentMediaUploader.vue'
import ContentResourcePicker from './ContentResourcePicker.vue'
import {
  artifactKindLabels,
  createIdempotencyKey,
  displayLabel,
  friendlyContentError,
  hasMaterialTransformChange,
  isArtifactContentEditable,
  isReferenceImagePrivacyError,
  jobStatusLabels,
  MIN_AI_VIDEO_DURATION_SECONDS,
  modeLabels,
  modeStageKeys,
  normalizeShots,
  parameterOptionLabel,
  providerLabel,
  selectedIds,
  stageArtifacts,
  stageDefinition,
  stateLabels,
  taskStatusLabels,
  workflowStageRoute,
} from './contentWorkflow'
import ImageGenerationControls from './ImageGenerationControls.vue'
import {
  imageGenerationFormState,
  latestGenerationRequestedCount,
  recoverImageGenerationDraft,
} from './imageGenerationDraft'
import { imageReferenceCount as countImageReferences } from './imageReferenceLimits'
import StoryboardEditor from './StoryboardEditor.vue'
import TaskConfigurationForm from './TaskConfigurationForm.vue'
import VideoEditorWorkspace from './video-editor/VideoEditorWorkspace.vue'
import {
  finalConfirmationRetryDecision,
  isOnlyWorkflowArtifact,
  stableWorkflowArtifactIds,
  workflowFinalizationDecision,
} from './video-editor/workflowFinalization'

defineOptions({ name: 'ContentTaskView' })

const props = withDefaults(
  defineProps<{
    mode?: TaskMode
    taskId?: string
    stageKey?: StageKey
  }>(),
  {
    mode: 'full',
    taskId: '',
    stageKey: 'configuration',
  },
)

const router = useRouter()
const loading = ref(false)
const busy = ref('')
const pageError = ref('')
const supportWarning = ref('')
const task = ref<ContentTask>()
const workflow = ref<TaskWorkflow>()
const artifacts = ref<Artifact[]>([])
const resources = ref<LibraryItem[]>([])
const localImageReferences = ref<LibraryItem[]>([])
const imageResources = computed(() => [...localImageReferences.value, ...resources.value])
const allTasks = ref<ContentTask[]>([])
const capabilities = ref<ContentCapabilities>({ capabilities: [] })
const serviceStatus = ref<ContentServiceStatus>()
const imageReferenceError = ref('')
const selectedArtifactIds = ref<string[]>([])
const selectionSaving = ref(false)
const lastPersistedSelectionIds = ref<string[]>([])
const stageRequirements = ref('')
const complianceResults = reactive<Record<string, ComplianceResult>>({})
const mediaUrls = reactive<Record<string, string>>({})
const activeRun = ref<{ id: string, status: string, error?: string | null }>()
const activeJob = ref<AgentJob>()
const editDialogOpen = ref(false)
let artifactEditorSession = 0
const imageRevisionPreviewOpen = ref(false)
const linkDialogOpen = ref(false)
const linkTargetTaskId = ref('')
const linkArtifactIds = ref<string[]>([])
const editingArtifact = ref<Artifact>()
const artifactDraft = ref<ArtifactContent>({})
const transformOperation = ref('rewrite')
const transformInstruction = ref('请保持品牌事实准确，并按要求调整表达')
const imageRevisionInstruction = ref('')
const imageRevisionNotice = ref('')
const storyboardDraft = ref<StoryboardShot[]>([])
const existingScriptText = ref('')
const exportFormat = ref('docx')
const selectedFullExportArtifactId = ref('')
const fullSingleExportFormats = reactive<Record<string, string>>({})

function emptyTaskForm() {
  return {
    name: '',
    topic: '',
    product: '',
    product_resource_id: '',
    knowledge_resource_ids: [] as string[],
    keywords: '',
    audience: '',
    content_type: '短视频口播',
    style: '品牌宣传型',
    length: '中' as ContentLength,
    requirements: '',
  }
}
const taskForm = reactive(emptyTaskForm())
const generationForm = reactive({
  count: undefined as number | undefined,
  knowledge_resource_ids: [] as string[],
  source_artifact_id: '',
  image_prompt: '',
  image_scene: '',
  image_style: '',
  image_ratio: '',
  image_size: '',
  reference_artifact_ids: [] as string[],
  reference_asset_ids: [] as string[],
  voice_text: '',
  voice: '',
  speed: undefined as number | undefined,
  volume: undefined as number | undefined,
  audio_format: '',
  sample_rate: '',
  subtitle_max_chars_per_line: undefined as number | undefined,
  subtitle_max_lines: undefined as number | undefined,
  ai_video_duration: '',
  ai_video_ratio: '',
  ai_video_resolution: '',
  generate_audio: false,
  return_last_frame: false,
  video_width: '',
  video_height: '',
  video_fps: '',
  video_size_preset: '',
  fit_mode: 'contain',
  transition: '',
  source_audio_policy: 'mute',
  source_asset_ids: [] as string[],
  voice_asset_id: '',
  uploaded_voice_text: '',
  bgm_asset_id: '',
  background_music_volume: undefined as number | undefined,
  logo_asset_id: '',
  logo_position: 'top_right',
  logo_width: undefined as number | undefined,
  intro_asset_id: '',
  intro_duration_seconds: undefined as number | undefined,
  outro_asset_id: '',
  outro_duration_seconds: undefined as number | undefined,
  subtitle_font_size: undefined as number | undefined,
  subtitle_primary_color: '',
  subtitle_outline_color: '',
  subtitle_margin_vertical: undefined as number | undefined,
})

interface ArtifactSelectionCommit {
  taskId: string
  stageKey: StageKey
  artifactIds: string[]
}

const artifactSelectionQueue = createLatestSelectionQueue<ArtifactSelectionCommit>(
  async ({ taskId, stageKey, artifactIds }) => {
    const stage = workflow.value?.stages.find(item => item.key === stageKey)
    if (!stage || task.value?.id !== taskId) {
      throw new Error('当前阶段状态已变化，请刷新后重试')
    }
    const updated = await contentApi.selectStageArtifacts(taskId, stageKey, {
      artifact_ids: artifactIds,
      expected_version: stage.version,
    })
    // Keep the CAS version current without replacing the workflow object. A
    // replacement would make the stage watcher overwrite newer optimistic
    // clicks that arrived while this request was in flight.
    Object.assign(stage, updated)
    lastPersistedSelectionIds.value = selectedIds(updated.selected_artifact_ids)
  },
  {
    onActiveChange: (active) => {
      selectionSaving.value = active
      if (!active) {
        selectedArtifactIds.value = [...lastPersistedSelectionIds.value]
      }
    },
    onError: async (error) => {
      await handleWorkflowMutationError(error, { reconcileOnAnyError: true })
      lastPersistedSelectionIds.value = selectedIds(currentStage.value?.selected_artifact_ids)
    },
  },
)

const activeStageKey = computed<StageKey>(() => props.stageKey)
const currentMode = computed<TaskMode>(() => task.value?.mode ?? props.mode)
const showVideoEditor = computed(
  () =>
    (!props.taskId || Boolean(task.value && task.value.can_edit !== false && !taskArchived.value))
    && ((currentMode.value === 'video' && activeStageKey.value !== 'export')
      || (currentMode.value === 'full' && activeStageKey.value === 'video_preview')),
)
const visibleStageKeys = computed(() => {
  if (workflow.value) {
    const modeKeys = new Set(modeStageKeys[currentMode.value])
    return workflow.value.stages
      .filter(item => item.applicable && modeKeys.has(item.key))
      .map(item => item.key)
  }
  return modeStageKeys[currentMode.value]
})
const currentStage = computed(() =>
  workflow.value?.stages.find(item => item.key === activeStageKey.value),
)
const exportStageConfirmed = computed(
  () => activeStageKey.value === 'export' && currentStage.value?.state === 'confirmed',
)
const taskWorkflowStatus = computed(() =>
  task.value?.workflow_status || (task.value?.status === 'archived' ? 'draft' : task.value?.status || 'draft'),
)
const taskArchived = computed(() => Boolean(task.value?.archived || task.value?.status === 'archived'))
const taskEditDraft = computed(() => Boolean(task.value?.is_edit_draft))
const archivedReadOnly = computed(() => taskArchived.value)
const currentStageDefinition = computed(() => stageDefinition(activeStageKey.value))
const currentArtifacts = computed(() => stageArtifacts(artifacts.value, activeStageKey.value))
const confirmedTopicArtifact = computed(() => {
  const stage = workflow.value?.stages.find(item => item.key === 'topic')
  const selected = selectedIds(stage?.selected_artifact_ids)
  const confirmed = selectedIds(stage?.confirmed_artifact_ids)
  const ids = confirmed.length ? confirmed : selected
  return ids
    .map(id => artifacts.value.find(item => item.id === id && item.kind === 'topic'))
    .filter((item): item is Artifact => Boolean(item))
    .sort((left, right) => right.version - left.version)[0]
})
const confirmedTopicTitle = computed(() => artifactSelectedTitle(confirmedTopicArtifact.value))
const showUpstreamChangedAlert = computed(() => {
  if (!currentStage.value || currentStage.value.state !== 'stale' || archivedReadOnly.value) {
    return false
  }
  // Copy cards already display the confirmed title. A leftover stale flag
  // after re-selecting that title is not a user-facing conflict.
  return !(activeStageKey.value === 'copy' && Boolean(confirmedTopicTitle.value))
})
const hasArtifactDraftChanges = computed(() =>
  Boolean(
    editingArtifact.value
    && JSON.stringify(artifactDraft.value) !== JSON.stringify(editingArtifact.value.content),
  ),
)
const confirmedArtifactIds = computed(
  () =>
    new Set(
      workflow.value?.stages.flatMap(stage =>
        Object.values(stage.confirmed_artifact_ids).flat(),
      ) ?? [],
    ),
)
const serverExportArtifactIds = computed(() => workflow.value?.export_artifact_ids)
const exportArtifactIds = computed(() =>
  new Set(
    serverExportArtifactIds.value === undefined
      ? confirmedArtifactIds.value
      : serverExportArtifactIds.value,
  ),
)
const currentStageConfirmedArtifactIds = computed(
  () => new Set(selectedIds(currentStage.value?.confirmed_artifact_ids)),
)
const directExportKind = computed(
  () =>
    (
      ({
        copy: 'copy',
        image: 'image',
        video: 'video',
      }) as Record<string, string>
    )[currentMode.value],
)
const directExportArtifacts = computed(() => {
  const kind = directExportKind.value
  if (!kind) {
    return []
  }
  return artifacts.value.filter(
    item =>
      item.kind === kind
      && item.status === 'confirmed'
      && (!workflow.value || confirmedArtifactIds.value.has(item.id)),
  )
})
const fullExportArtifactKinds = new Set([
  'topic',
  'copy',
  'image',
  'storyboard',
  'voice',
  'subtitle',
  'video',
])
const fullExportArtifacts = computed(() =>
  artifacts.value.filter(
    item =>
      currentMode.value === 'full'
      && exportArtifactIds.value.has(item.id)
      && (serverExportArtifactIds.value !== undefined || item.status === 'confirmed')
      && fullExportArtifactKinds.has(item.kind),
  ),
)
const selectedFullExportArtifact = computed(() =>
  fullExportArtifacts.value.find(item => item.id === selectedFullExportArtifactId.value)
  ?? fullExportArtifacts.value[0],
)
const originalFullExportFormat = '__original__'
const selectedFullExportFormatOptions = computed(() => {
  const artifact = selectedFullExportArtifact.value
  if (!artifact) {
    return []
  }
  const options = fullSingleExportFormatOptions(artifact)
  return options.length
    ? options
    : [{
        label: artifact.kind === 'video' ? 'MP4 视频（.mp4）' : '原文件格式',
        value: originalFullExportFormat,
      }]
})
const selectedFullExportFormat = computed(() => {
  const artifact = selectedFullExportArtifact.value
  return artifact
    ? fullSingleExportFormat(artifact) || originalFullExportFormat
    : ''
})
const exportFormatOptions = computed(() => {
  if (currentMode.value === 'copy') {
    return [
      { label: 'Word 文档（.docx）', value: 'docx' },
      { label: '纯文本（.txt）', value: 'txt' },
      { label: 'Markdown（.md）', value: 'md' },
    ]
  }
  if (currentMode.value === 'image') {
    return [
      { label: 'PNG 图片（.png）', value: 'png' },
      { label: 'JPG 图片（.jpg）', value: 'jpg' },
      { label: 'WebP 图片（.webp）', value: 'webp' },
    ]
  }
  return []
})
const exportPresentation = computed(() => {
  const count = directExportArtifacts.value.length
  if (currentMode.value === 'copy') {
    return {
      title: '导出最终文案',
      description: '导出当前版本对应的标题和文案，可选择 Word、纯文本或 Markdown 格式。',
      button: `导出文案${count > 1 ? `（${count} 个版本）` : ''}`,
    }
  }
  if (currentMode.value === 'image') {
    return {
      title: '导出最终图片',
      description: '只导出当前选中的图片，并保持生成时的像素尺寸。',
      button: `导出图片${count > 1 ? `（${count} 张）` : ''}`,
    }
  }
  if (currentMode.value === 'video') {
    return {
      title: '导出最终视频',
      description: '直接下载已确认的 MP4 成片。',
      button: '导出视频',
    }
  }
  return {
    title: '导出完整创作素材',
    description: '逐项下载当前版本已有的标题、文案、图片、最终视频、音频和字幕，也可一键打包。',
    button: '下载完整素材包',
  }
})
const bundleDescription = computed(() => {
  if (currentMode.value === 'copy') {
    return 'ZIP 只打包当前版本对应的标题和文案。'
  }
  if (currentMode.value === 'image') {
    return 'ZIP 只打包当前选中的图片。'
  }
  return 'ZIP 按当前版本打包已有的图片、最终视频、音频和字幕，未生成的内容不会加入。'
})
const exportResultIcon = computed(() => exportStageConfirmed.value ? 'success' : 'warning')
const exportResultTitle = computed(() => exportStageConfirmed.value
  ? exportPresentation.value.title
  : archivedReadOnly.value ? '已归档版本' : currentStage.value?.state === 'stale' ? '导出内容需要更新' : '待确认导出')
const exportResultDescription = computed(() => exportStageConfirmed.value
  ? exportPresentation.value.description
  : archivedReadOnly.value
    ? '这是已保存的历史版本。需要修改时可返回前面的步骤；最终确认后另存为新任务，原归档内容不变。'
    : currentStage.value?.state === 'stale'
      ? '上游内容发生变化，当前导出版本已失效。请先确认最新内容，再重新确认导出。'
      : '请核对当前内容，点击“确认导出”后即可下载。')

function fullSingleExportFormatOptions(artifact: Artifact) {
  if (['topic', 'copy'].includes(artifact.kind)) {
    return [
      { label: 'Word（.docx）', value: 'docx' },
      { label: '纯文本（.txt）', value: 'txt' },
      { label: 'Markdown（.md）', value: 'md' },
    ]
  }
  if (artifact.kind === 'image') {
    return [
      { label: 'PNG（.png）', value: 'png' },
      { label: 'JPG（.jpg）', value: 'jpg' },
      { label: 'WebP（.webp）', value: 'webp' },
    ]
  }
  return []
}

function fullSingleExportFormat(artifact: Artifact) {
  return fullSingleExportFormats[artifact.id]
    || fullSingleExportFormatOptions(artifact)[0]?.value
}

function updateFullSingleExportFormat(artifactId: string, value: string) {
  if (value === originalFullExportFormat) {
    delete fullSingleExportFormats[artifactId]
    return
  }
  fullSingleExportFormats[artifactId] = value
}

function fullExportArtifactLabel(artifact: Artifact) {
  return `${displayLabel(artifact.kind, artifactKindLabels, '内容')} v${artifact.version}`
}
const copyArtifacts = computed(() =>
  artifacts.value.filter(item => item.kind === 'copy' && item.status !== 'discarded'),
)
const referenceImageArtifacts = computed(() =>
  artifacts.value.filter(
    item => item.kind === 'image' && item.status !== 'discarded' && item.storage_key,
  ),
)
const storyboardEnterpriseAssets = computed(() =>
  resources.value.filter(
    item =>
      ['image', 'logo'].includes(item.resource_kind)
      && Boolean(item.has_asset || item.download_url || item.storage_key),
  ),
)
const storyboardTaskAssetOptions = computed(() =>
  referenceImageArtifacts.value.map(item => ({
    id: item.id,
    name: `${item.linked ? '已回引' : '本任务'}图片 · 版本 ${item.version}`,
  })),
)
const compositionArtifactOptions = computed(() =>
  artifacts.value
    .filter(
      item =>
        ['image', 'ai_video_clip', 'video'].includes(item.kind)
        && item.storage_key
        && !['discarded', 'stale', 'legacy_mock'].includes(item.status),
    )
    .map(item => ({
      id: item.id,
      label: `${item.linked ? '已回引' : '本任务'} · ${item.kind === 'image' ? '图片' : item.kind === 'ai_video_clip' && item.content.shot_index != null ? `镜头 ${Number(item.content.shot_index) + 1} 视频` : '成片'}版本 ${item.version}`,
    })),
)
const imageVideoResourceKinds: LibraryResourceKind[] = ['image', 'video']
const audioResourceKinds: LibraryResourceKind[] = ['audio']
const logoResourceKinds: LibraryResourceKind[] = ['logo', 'image']
const videoResourceKinds: LibraryResourceKind[] = ['video']
const compositionArtifactIds = computed(
  () => new Set(compositionArtifactOptions.value.map(item => item.id)),
)
const selectedCompositionArtifactIds = computed({
  get: () => generationForm.source_asset_ids.filter(id => compositionArtifactIds.value.has(id)),
  set: (ids: string[]) => {
    const libraryIds = generationForm.source_asset_ids.filter(
      id => !compositionArtifactIds.value.has(id),
    )
    generationForm.source_asset_ids = [...libraryIds, ...ids]
  },
})
const selectedStoryboard = computed(() => {
  const selected = selectedArtifactIds.value
    .map(id => artifacts.value.find(item => item.id === id))
    .find(item => item?.kind === 'storyboard')
  return (
    selected
    ?? [...artifacts.value]
      .reverse()
      .find(item => item.kind === 'storyboard' && item.status !== 'discarded')
  )
})
const activeCapability = computed(() => capabilityForStage(activeStageKey.value))
const imageReferenceLimit = computed(() => {
  const value = parameterRange(activeCapability.value, 'reference_images')?.max
  return Number.isInteger(value) && Number(value) >= 0 ? Number(value) : undefined
})
const speechCapability = computed(() => capabilityForOperation('voice_subtitles', 'voice'))
const aiVideoCapability = computed(() => capabilityForOperation('video_preview', 'clips'))
const compositionCapability = computed(() => capabilityForOperation('video_preview', 'compose'))
const storyboardShots = computed(() =>
  selectedStoryboard.value ? normalizeShots(selectedStoryboard.value.content) : [],
)
const availableVoiceArtifacts = computed(() =>
  artifacts.value.filter(
    item =>
      item.kind === 'voice'
      && item.storage_key
      && !['discarded', 'stale', 'legacy_mock'].includes(item.status),
  ),
)
const availableSubtitleArtifacts = computed(() =>
  artifacts.value.filter(
    item =>
      item.kind === 'subtitle'
      && item.storage_key
      && !['discarded', 'stale', 'legacy_mock'].includes(item.status),
  ),
)
const compositionReadiness = computed(() => {
  const shots = storyboardShots.value
  const fallbackSources = generationForm.source_asset_ids.length
  const missingShotAssets = shots.filter(
    (shot, index) => !shot.asset_id && index >= fallbackSources,
  ).length
  return {
    shotCount: shots.length,
    duration: shots.reduce((sum, shot) => sum + Math.max(Number(shot.duration_seconds) || 0, 0), 0),
    missingShotAssets,
    hasVoice: Boolean(generationForm.voice_asset_id || availableVoiceArtifacts.value.length),
    hasSubtitle: Boolean(availableSubtitleArtifacts.value.length),
  }
})
const aiVideoCandidatesByShot = computed(() => {
  const counts = new Map<number, number>()
  for (const artifact of artifacts.value) {
    if (
      artifact.kind !== 'ai_video_clip'
      || ['discarded', 'stale', 'legacy_mock'].includes(artifact.status)
    ) {
      continue
    }
    const index = Number(artifact.content.shot_index)
    if (Number.isInteger(index) && index >= 0) {
      counts.set(index, (counts.get(index) || 0) + 1)
    }
  }
  return counts
})
const candidateCountRange = computed(() => parameterRange(activeCapability.value, 'count'))
const editableStoryboard = computed(
  () => activeStageKey.value === 'storyboard' && selectedStoryboard.value,
)
const linkTargetTasks = computed(() =>
  allTasks.value.filter(
    item => item.can_edit !== false && ['full', 'video'].includes(item.mode) && item.id !== task.value?.id,
  ),
)
const linkableArtifacts = computed(() =>
  artifacts.value.filter(
    item => confirmedArtifactIds.value.has(item.id) && !item.linked,
  ),
)

const contentTypeOptions = [
  '短视频口播',
  '短视频脚本',
  '产品介绍',
  '品牌宣传',
  '活动宣传',
  '节日营销',
  '社交媒体内容',
  '标题及摘要',
]
const styleOptions = [
  '正式',
  '口语化',
  '年轻化',
  '品牌宣传型',
  '知识科普型',
  '故事型',
  '短视频口播型',
]
const firstProductionStage: Record<TaskMode, StageKey> = {
  full: 'topic',
  copy: 'topic',
  image: 'image',
  video: 'storyboard',
}
const creationGuidance: Record<TaskMode, { description: string, action: string }> = {
  full: {
    description: '保存基础信息后直接进入智能标题，继续完成文案、图片、分镜、配音字幕和视频。',
    action: '保存并进入智能标题',
  },
  copy: {
    description: '保存基础信息后先生成并确认标题，再由智能文案自动读取任务配置与所选标题生成正文。',
    action: '保存并进入智能标题',
  },
  image: {
    description: '填写图片主题和产品等基础信息，保存后直接进入图片生成并补充本阶段要求。',
    action: '保存并进入图片生成',
  },
  video: {
    description: '使用已有创意、脚本或素材，保存后直接进入视频分镜，不强制经过选题和文案策划。',
    action: '保存并进入视频分镜',
  },
}
const generationHistoryStages = new Set<StageKey>([
  'topic',
  'image',
  'storyboard',
  'voice_subtitles',
  'video_preview',
])

function updateTaskForm(value: typeof taskForm) {
  Object.assign(taskForm, value)
}

function resetCreationForm() {
  Object.assign(taskForm, emptyTaskForm())
  existingScriptText.value = ''
  pageError.value = ''
  supportWarning.value = ''
  generationForm.knowledge_resource_ids = []
}

function selectedTextCandidate(
  artifact: Artifact | undefined,
): Record<string, unknown> | undefined {
  if (!artifact) {
    return undefined
  }
  const candidates = artifact.content.candidates ?? []
  const selectedIndex = artifact.content.selected_candidate_index
  if (
    typeof selectedIndex === 'number'
    && Number.isInteger(selectedIndex)
    && selectedIndex >= 0
    && selectedIndex < candidates.length
  ) {
    return candidates[selectedIndex]
  }
  if (artifact.content.selected_candidate) {
    return artifact.content.selected_candidate as Record<string, unknown>
  }
  return candidates[0]
}

function artifactSelectedTitle(artifact: Artifact | undefined) {
  const candidate = selectedTextCandidate(artifact)
  return String(candidate?.title || artifact?.content.title || artifact?.content.text || '').trim()
}

function copyContentWithConfirmedTitle(content: ArtifactContent) {
  const title = confirmedTopicTitle.value
  if (!title) {
    return content
  }
  const candidates = content.candidates?.map(candidate => ({ ...candidate, title }))
  const selectedCandidate = content.selected_candidate
    ? { ...content.selected_candidate, title }
    : undefined
  return {
    ...content,
    // The confirmed topic title is the copy's title through preview and save, even
    // when an AI preview returns only the body.
    title,
    ...(candidates ? { candidates } : {}),
    ...(selectedCandidate ? { selected_candidate: selectedCandidate } : {}),
  }
}

function normalizeContentLength(value: string): ContentLength {
  if (value === '短' || value === '长') {
    return value
  }
  return '中'
}

function taskKnowledgeResourceIds(value: ContentTask) {
  if (value.knowledge_resource_ids?.length) {
    return [...value.knowledge_resource_ids]
  }
  return value.product_resource_id ? [value.product_resource_id] : []
}

function messageOf(error: unknown) {
  return error instanceof ContentGenerationError ? error.message : friendlyContentError(error)
}

function generationJobError(job: AgentJob) {
  return ['topic', 'copy', 'image', 'video_preview'].includes(activeStageKey.value)
    ? formatContentGenerationError(job, '后台生成任务失败，服务未返回具体原因，请稍后重试')
    : friendlyContentError(job.error, '后台生成任务失败，请调整参数后重试')
}

function importDraftKey(taskId: string, stage: 'storyboard') {
  return `content-import-draft:${stage}:${taskId}`
}

function stageState(key: StageKey) {
  return workflow.value?.stages.find(item => item.key === key)?.state ?? 'not_started'
}

function presentationStageState(key: StageKey) {
  const state = stageState(key)
  if (taskArchived.value && state === 'stale') {
    return stageHasExportArtifact(key) ? 'archived' : 'archived_unincluded'
  }
  return state
}

function stageHasExportArtifact(key: StageKey) {
  const kinds = new Set(stageDefinition(key).artifactKinds)
  return artifacts.value.some(artifact =>
    exportArtifactIds.value.has(artifact.id)
    && (serverExportArtifactIds.value !== undefined || artifact.status === 'confirmed')
    && (artifact.stage_key === key || kinds.has(artifact.kind)),
  )
}

function presentationStageLabel(key: StageKey) {
  const state = presentationStageState(key)
  if (state === 'archived') {
    return '已归档'
  }
  if (state === 'archived_unincluded') {
    return '未纳入导出'
  }
  return stateLabels[state as keyof typeof stateLabels] ?? '状态待确认'
}

function stateTagType(state: string) {
  if (state === 'confirmed') {
    return 'success'
  }
  if (state === 'failed') {
    return 'danger'
  }
  if (state === 'stale' || state === 'awaiting_confirmation') {
    return 'warning'
  }
  if (state === 'archived_unincluded') {
    return 'warning'
  }
  return 'info'
}

function capabilityForStage(stage: StageKey): ProviderCapability | undefined {
  const capabilityMap: Partial<Record<StageKey, string[]>> = {
    topic: ['text-generation'],
    copy: ['text-generation'],
    image: ['image-generation'],
    storyboard: ['text-generation'],
    voice_subtitles: ['speech-synthesis'],
    video_preview: ['ai-video-generation', 'video-composition'],
  }
  const names = capabilityMap[stage]
  if (!names) {
    return undefined
  }
  return capabilities.value.capabilities.find(item => names.includes(item.capability))
}

function capabilityForOperation(stage: StageKey, operation?: string) {
  const operationCapability: Record<string, string> = {
    subtitle: 'subtitle-alignment',
    clips: 'ai-video-generation',
    compose: 'video-composition',
  }
  const capability = operation && operationCapability[operation]
  return capability
    ? capabilities.value.capabilities.find(item => item.capability === capability)
    : capabilityForStage(stage)
}

function statusForCapability(capability?: ProviderCapability) {
  if (!capability || !serviceStatus.value) {
    return undefined
  }
  return serviceStatus.value.services.find(
    item =>
      item.capability === capability.capability && item.provider_id === capability.provider_id,
  )
}

function isGenerationUnavailable(operation?: string) {
  const stagesWithoutProvider: StageKey[] = ['configuration', 'compliance', 'export']
  if (stagesWithoutProvider.includes(activeStageKey.value)) {
    return false
  }
  const capability = capabilityForOperation(activeStageKey.value, operation)
  const service = statusForCapability(capability)
  return (
    !capability
    || !capability.enabled
    || capability.mode === 'mock'
    || service?.status === 'disabled'
  )
}

function unavailableMessage(operation?: string) {
  const capability = capabilityForOperation(activeStageKey.value, operation)
  const service = statusForCapability(capability)
  if (!capability) {
    return '暂时无法读取这项 AI 功能，请稍后再次点击生成。'
  }
  if (capability.mode === 'mock') {
    return '这项 AI 功能尚未连接正式服务，请联系管理员完成配置后再次尝试。'
  }
  if (!capability.enabled || service?.status === 'disabled') {
    return '这项 AI 功能暂时无法调用，请联系管理员检查服务配置，之后可再次尝试。'
  }
  return '请先完成真实 AI 服务配置后重试。'
}

async function ensureGenerationAvailable(operation?: string) {
  if (!isGenerationUnavailable(operation)) {
    return true
  }
  // Refresh configuration on a user attempt so a repaired service is never
  // stuck behind a cached preflight failure. Historical health is not a gate.
  busy.value = 'checking-service'
  try {
    const [capabilityResult, statusResult] = await Promise.allSettled([
      contentApi.capabilities(),
      contentApi.serviceStatus(),
    ])
    if (capabilityResult.status === 'fulfilled') {
      capabilities.value = capabilityResult.value
    }
    if (statusResult.status === 'fulfilled') {
      serviceStatus.value = statusResult.value
    }
    if (isGenerationUnavailable(operation)) {
      ElMessage.error(unavailableMessage(operation))
      return false
    }
    return true
  }
  finally {
    busy.value = ''
  }
}

function parameterOptions(capability: ProviderCapability | undefined, key: string) {
  if (!capability) {
    return [] as Array<{ label: string, value: string }>
  }
  const raw = capability.parameters?.[key]
  const options = Array.isArray(raw)
    ? raw
    : raw && typeof raw === 'object' && 'options' in raw
      ? ((raw as { options?: unknown[] }).options ?? [])
      : []
  return options
    .filter(value => ['string', 'number'].includes(typeof value))
    .filter(value => key !== 'durations' || Number(value) >= MIN_AI_VIDEO_DURATION_SECONDS)
    .map(value => ({ label: parameterOptionLabel(key, value), value: String(value) }))
}

function parameterRange(capability: ProviderCapability | undefined, key: string) {
  const raw = capability?.parameters?.[key]
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return undefined
  }
  const range = raw as { min?: unknown, max?: unknown }
  const min = typeof range.min === 'number' ? range.min : undefined
  const max = typeof range.max === 'number' ? range.max : undefined
  return min != null || max != null ? { min, max } : undefined
}

function supportsParameter(capability: ProviderCapability | undefined, key: string) {
  return capability?.parameters?.[key] === true
}

function supportsCustomParameter(
  capability: ProviderCapability | undefined,
  key: 'ratio' | 'size',
) {
  const explicit = capability?.parameters?.[`custom_${key}`]
  if (explicit === true) {
    return true
  }
  const raw = capability?.parameters?.[`${key}s`]
  return Boolean(
    raw
    && typeof raw === 'object'
    && !Array.isArray(raw)
    && ((raw as { allow_custom?: unknown }).allow_custom === true
      || (raw as { custom?: unknown }).custom === true),
  )
}

function parameterDefault(capability: ProviderCapability | undefined, key: string) {
  const defaults = capability?.parameters?.defaults
  if (defaults && typeof defaults === 'object' && !Array.isArray(defaults)) {
    const value = (defaults as Record<string, unknown>)[key]
    if (typeof value === 'string' || typeof value === 'number') {
      return String(value)
    }
  }
  const raw = capability?.parameters?.[key]
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    const value = (raw as { default?: unknown }).default
    if (typeof value === 'string' || typeof value === 'number') {
      return String(value)
    }
  }
  return ''
}

function updateImageGenerationForm(value: ImageGenerationValue) {
  imageReferenceError.value = ''
  Object.assign(generationForm, value)
}

function handleImageReferenceResourcesUploaded(items: LibraryItem[]) {
  for (const item of items) {
    const existingIndex = localImageReferences.value.findIndex(resource => resource.id === item.id)
    if (existingIndex >= 0) {
      localImageReferences.value.splice(existingIndex, 1, item)
    }
    else {
      localImageReferences.value.unshift(item)
    }
  }
}

function imageReferenceSelectionCount(
  knowledgeResourceIds = generationForm.knowledge_resource_ids,
  libraryAssetIds = generationForm.reference_asset_ids,
  artifactIds = generationForm.reference_artifact_ids,
) {
  return countImageReferences(imageResources.value, {
    knowledgeResourceIds,
    libraryAssetIds,
    artifactIds,
  })
}

function validateImageKnowledgeReferenceSelection(ids: string[]) {
  const nextIds = [...new Set(ids.map(id => String(id).trim()).filter(Boolean))]
  const currentCount = imageReferenceSelectionCount()
  const nextCount = imageReferenceSelectionCount(nextIds)
  const limit = imageReferenceLimit.value
  if (limit == null && nextCount > currentCount) {
    return '当前图片模型未声明参考图片上限，已阻止添加知识库图片或 Logo。'
  }
  if (limit != null && nextCount > limit && nextCount >= currentCount) {
    return `参考图片合计最多选择 ${limit} 张；知识库图片、Logo 和本地上传图片会合并去重计数。`
  }
  return ''
}

function updateImageKnowledgeReferences(ids: string[]) {
  const message = validateImageKnowledgeReferenceSelection(ids)
  if (message) {
    imageReferenceError.value = message
    return
  }
  generationForm.knowledge_resource_ids = [
    ...new Set(ids.map(id => String(id).trim()).filter(Boolean)),
  ]
  imageReferenceError.value = ''
}

function updateSingleResource(
  key: 'voice_asset_id' | 'bgm_asset_id' | 'logo_asset_id' | 'intro_asset_id' | 'outro_asset_id',
  ids: string[],
) {
  generationForm[key] = ids.at(-1) || ''
}

function applyVideoSizePreset(value: string) {
  generationForm.video_size_preset = value
  const match = /^(\d{2,5})x(\d{2,5})$/i.exec(value.trim())
  if (!match) {
    return
  }
  generationForm.video_width = match[1] || ''
  generationForm.video_height = match[2] || ''
}

function handleTaskMediaUploaded(item: LibraryItem) {
  const existingIndex = resources.value.findIndex(resource => resource.id === item.id)
  if (existingIndex >= 0) {
    resources.value.splice(existingIndex, 1, item)
  }
  else {
    resources.value.unshift(item)
  }
  if (item.resource_kind === 'audio') {
    generationForm.voice_asset_id ||= item.id
    ElMessage.success('音频已上传，并设为当前主配音；可改选为背景音乐')
    return
  }
  if (
    ['image', 'video'].includes(item.resource_kind)
    && !generationForm.source_asset_ids.includes(item.id)
  ) {
    generationForm.source_asset_ids.push(item.id)
  }
  ElMessage.success('素材已上传并加入当前视频素材')
}

function validateImageGenerationParameters() {
  const referenceCount = imageReferenceSelectionCount()
  if (referenceCount && imageReferenceLimit.value == null) {
    ElMessage.warning('当前图片模型未声明参考图片上限，不能提交所选参考图片')
    return false
  }
  if (imageReferenceLimit.value != null && referenceCount > imageReferenceLimit.value) {
    ElMessage.warning(
      `参考图片最多选择 ${imageReferenceLimit.value} 张，当前合并去重后为 ${referenceCount} 张，请先移除多余图片`,
    )
    return false
  }
  const checks: Array<{
    label: string
    key: 'ratio' | 'size'
    value: string
    options: Array<{ value: string }>
  }> = [
    {
      label: '画面比例',
      key: 'ratio',
      value: generationForm.image_ratio,
      options: parameterOptions(activeCapability.value, 'ratios'),
    },
    {
      label: '图片尺寸',
      key: 'size',
      value: generationForm.image_size,
      options: parameterOptions(activeCapability.value, 'sizes'),
    },
  ]
  for (const check of checks) {
    if (!check.value || supportsCustomParameter(activeCapability.value, check.key)) {
      continue
    }
    if (!check.options.some(option => option.value === check.value)) {
      ElMessage.warning(
        `${check.label}“${check.value}”不在当前图片服务支持范围内，请重新选择或留空使用默认值`,
      )
      return false
    }
  }
  return true
}

function fillTaskForm(value: ContentTask) {
  Object.assign(taskForm, {
    name: value.name,
    topic: value.topic,
    product: value.product,
    product_resource_id: value.product_resource_id ?? '',
    knowledge_resource_ids: taskKnowledgeResourceIds(value),
    keywords: value.keywords.join('，'),
    audience: value.audience,
    content_type: value.content_type,
    style: value.style,
    length: normalizeContentLength(value.length),
    requirements: value.requirements,
  })
}

async function loadCore() {
  if (!props.taskId) {
    return
  }
  loading.value = true
  pageError.value = ''
  supportWarning.value = ''
  const results = await Promise.allSettled([
    contentApi.getTask(props.taskId),
    contentApi.getWorkflow(props.taskId),
    contentApi.listArtifacts(props.taskId),
    contentApi.listLibrary(),
    contentApi.capabilities(),
    contentApi.serviceStatus(),
    contentApi.listTasks(),
    generationHistoryStages.has(activeStageKey.value)
      ? contentApi.listGenerationRuns(props.taskId, {
          stageKey: activeStageKey.value,
          status: 'succeeded',
          pageSize: activeStageKey.value === 'image' ? 10 : 1,
        })
      : Promise.resolve([]),
  ])
  if (results[0].status === 'fulfilled') {
    task.value = results[0].value
    if (task.value.can_edit === false) {
      loading.value = false
      await router.replace({ path: '/agent/content/versions', query: { taskId: task.value.id } })
      return
    }
    fillTaskForm(results[0].value)
    generationForm.knowledge_resource_ids = taskKnowledgeResourceIds(results[0].value)
  }
  else {
    pageError.value = messageOf(results[0].reason)
  }
  if (results[1].status === 'fulfilled') {
    workflow.value = results[1].value
  }
  else {
    pageError.value ||= messageOf(results[1].reason)
  }
  if (results[2].status === 'fulfilled') {
    artifacts.value = results[2].value
  }
  else {
    supportWarning.value = messageOf(results[2].reason)
  }
  if (results[3].status === 'fulfilled') {
    resources.value = results[3].value
  }
  if (results[4].status === 'fulfilled') {
    capabilities.value = results[4].value
  }
  else {
    supportWarning.value ||= `能力参数不可用：${messageOf(results[4].reason)}`
  }
  if (results[5].status === 'fulfilled') {
    serviceStatus.value = results[5].value
  }
  else {
    supportWarning.value ||= `服务状态不可用：${messageOf(results[5].reason)}`
  }
  if (results[6].status === 'fulfilled') {
    allTasks.value = results[6].value
  }
  const generationRuns = results[7].status === 'fulfilled' ? results[7].value : []
  const latestRequestedCount = latestGenerationRequestedCount(generationRuns)
  if (latestRequestedCount != null) {
    generationForm.count = latestRequestedCount
  }
  if (activeStageKey.value === 'image') {
    Object.assign(
      generationForm,
      recoverImageGenerationDraft(
        generationRuns,
        artifacts.value,
      ),
    )
  }
  loading.value = false
  if (task.value && workflow.value) {
    // Opening an old editor URL must not mount an autosaving editor against
    // the archive. A deliberate return to an earlier step creates a draft.
    if (taskArchived.value && activeStageKey.value !== 'export') {
      await router.replace(`/agent/content/tasks/${task.value.id}/stages/export`)
      return
    }
    const allowedStages = modeStageKeys[task.value.mode]
    if (!allowedStages.includes(activeStageKey.value)) {
      const workflowStage = workflow.value.current_stage as StageKey
      const fallbackStage = workflowStageRoute(task.value.mode, activeStageKey.value, workflowStage)
      await router.replace(`/agent/content/tasks/${task.value.id}/stages/${fallbackStage}`)
    }
  }
}

async function loadCreationSupport() {
  loading.value = true
  const results = await Promise.allSettled([contentApi.listLibrary(), contentApi.capabilities()])
  if (results[0].status === 'fulfilled') {
    resources.value = results[0].value
  }
  else {
    supportWarning.value = `企业知识库暂不可用，仍可手工填写产品和任务信息：${messageOf(results[0].reason)}`
  }
  if (results[1].status === 'fulfilled') {
    capabilities.value = results[1].value
  }
  loading.value = false
}

async function refreshWorkflowAndArtifacts() {
  if (!task.value) {
    return
  }
  const [nextTask, nextWorkflow, nextArtifacts] = await Promise.all([
    contentApi.getTask(task.value.id),
    contentApi.getWorkflow(task.value.id),
    contentApi.listArtifacts(task.value.id),
  ])
  task.value = nextTask
  workflow.value = nextWorkflow
  artifacts.value = nextArtifacts
  fillTaskForm(nextTask)
}

function isVersionConflictError(error: unknown) {
  const message = error instanceof Error
    ? error.message
    : typeof error === 'string' ? error : ''
  return /版本冲突|版本已变化|版本不匹配|version (?:conflict|mismatch)|\b409\b/i.test(message)
}

async function reconcileWorkflowState(options: { preserveTaskForm?: boolean } = {}) {
  if (!task.value) {
    return false
  }
  const attemptedStage = activeStageKey.value
  const pendingRequirements = stageRequirements.value
  const hasUnsavedRequirements = pendingRequirements !== currentStage.value?.requirements
  const pendingTaskForm = options.preserveTaskForm
    ? {
        ...taskForm,
        knowledge_resource_ids: [...taskForm.knowledge_resource_ids],
      }
    : undefined
  try {
    await refreshWorkflowAndArtifacts()
    if (hasUnsavedRequirements && activeStageKey.value === attemptedStage) {
      stageRequirements.value = pendingRequirements
    }
    if (pendingTaskForm) {
      Object.assign(taskForm, pendingTaskForm)
    }
    return true
  }
  catch {
    return false
  }
}

async function handleWorkflowMutationError(
  error: unknown,
  options: { reconcileOnAnyError?: boolean, preserveTaskForm?: boolean } = {},
) {
  const versionConflict = isVersionConflictError(error)
  const reconciled = versionConflict || options.reconcileOnAnyError
    ? await reconcileWorkflowState({ preserveTaskForm: options.preserveTaskForm })
    : false
  if (versionConflict && reconciled) {
    ElMessage.warning('页面状态已自动同步到最新版本，请重试刚才的操作')
    return
  }
  ElMessage.error(messageOf(error))
}

async function refreshServiceState() {
  try {
    serviceStatus.value = await contentApi.serviceStatus()
  }
  catch {
    // Generation errors remain primary; a later manual refresh can retry service status.
  }
}

async function createTask() {
  if (!taskForm.name.trim()) {
    ElMessage.warning('请填写任务名称')
    return
  }
  busy.value = 'create-task'
  pageError.value = ''
  try {
    const created = await contentApi.createTask({
      mode: currentMode.value,
      name: taskForm.name.trim(),
      topic: taskForm.topic.trim(),
      product: taskForm.product.trim(),
      product_resource_id: taskForm.product_resource_id || null,
      knowledge_resource_ids: [...taskForm.knowledge_resource_ids],
      keywords: taskForm.keywords
        .split(/[，,]/)
        .map(item => item.trim())
        .filter(Boolean),
      audience: taskForm.audience.trim(),
      content_type: taskForm.content_type,
      style: taskForm.style,
      length: taskForm.length,
      requirements: taskForm.requirements,
    })
    const importStage = currentMode.value === 'video' ? 'storyboard' : undefined
    const importText = importStage === 'storyboard' ? existingScriptText.value.trim() : ''
    if (importStage && importText) {
      const importKey = importDraftKey(created.id, importStage)
      sessionStorage.setItem(importKey, importText)
      try {
        const createdWorkflow = await contentApi.getWorkflow(created.id)
        const stage = createdWorkflow.stages.find(item => item.key === importStage)
        if (!stage) {
          throw new Error('新任务未返回可导入的目标阶段')
        }
        await contentApi.importStoryboard(created.id, {
          text: importText,
          expected_version: stage.version,
        })
        sessionStorage.removeItem(importKey)
        ElMessage.success('任务及已有脚本已保存为分镜草稿')
      }
      catch (error) {
        ElMessage.error(`任务已创建，但已有内容暂未保存：${messageOf(error)}。可在下一页重试。`)
      }
      await router.replace(`/agent/content/tasks/${created.id}/stages/${importStage}`)
      return
    }
    const targetStage = firstProductionStage[currentMode.value]
    ElMessage.success(`任务配置已保存，正在进入${stageDefinition(targetStage).label}`)
    await router.replace(`/agent/content/tasks/${created.id}/stages/${targetStage}`)
  }
  catch (error) {
    pageError.value = messageOf(error)
  }
  finally {
    busy.value = ''
  }
}

async function importExistingContent(stageKey: 'storyboard') {
  if (!task.value || !workflow.value) {
    return
  }
  const text = existingScriptText.value.trim()
  if (!text) {
    ElMessage.warning('请先输入已有脚本')
    return
  }
  const stage = workflow.value.stages.find(item => item.key === stageKey)
  if (!stage) {
    ElMessage.error('当前任务不包含该导入阶段')
    return
  }
  busy.value = `import-${stageKey}`
  try {
    await contentApi.importStoryboard(task.value.id, { text, expected_version: stage.version })
    sessionStorage.removeItem(importDraftKey(task.value.id, stageKey))
    await refreshWorkflowAndArtifacts()
    ElMessage.success('已有脚本已保存为可编辑分镜')
  }
  catch (error) {
    sessionStorage.setItem(importDraftKey(task.value.id, stageKey), text)
    await handleWorkflowMutationError(error)
  }
  finally {
    busy.value = ''
  }
}

async function saveConfiguration() {
  if (!task.value) {
    return
  }
  busy.value = 'save-configuration'
  try {
    task.value = await contentApi.updateTask(task.value.id, {
      expected_version: task.value.version,
      name: taskForm.name.trim(),
      topic: taskForm.topic.trim(),
      product: taskForm.product.trim(),
      product_resource_id: taskForm.product_resource_id || null,
      knowledge_resource_ids: [...taskForm.knowledge_resource_ids],
      keywords: taskForm.keywords
        .split(/[，,]/)
        .map(item => item.trim())
        .filter(Boolean),
      audience: taskForm.audience.trim(),
      content_type: taskForm.content_type,
      style: taskForm.style,
      length: taskForm.length,
      requirements: taskForm.requirements,
    })
    generationForm.knowledge_resource_ids = [...task.value.knowledge_resource_ids]
    await refreshWorkflowAndArtifacts()
    ElMessage.success('任务配置已保存，上游变化会将相关下游标记为需更新')
  }
  catch (error) {
    await handleWorkflowMutationError(error, { preserveTaskForm: true })
  }
  finally {
    busy.value = ''
  }
}

async function saveStageRequirements() {
  if (!task.value || !currentStage.value) {
    return false
  }
  if (!await flushArtifactSelection()) {
    return false
  }
  if (stageRequirements.value === currentStage.value.requirements) {
    return true
  }
  await contentApi.updateStageRequirements(task.value.id, activeStageKey.value, {
    requirements: stageRequirements.value,
    expected_version: currentStage.value.version,
  })
  await refreshWorkflowAndArtifacts()
  return true
}

function cleanParameters(value: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(value).filter(
      ([, item]) =>
        item !== ''
        && item !== undefined
        && item !== null
        && (!Array.isArray(item) || item.length > 0),
    ),
  )
}

function numericParameter(value: string) {
  if (!value.trim()) {
    return undefined
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function generationKnowledgeParameters() {
  const taskDefaults = task.value ? taskKnowledgeResourceIds(task.value) : []
  const knowledgeResourceIds = generationForm.knowledge_resource_ids.length
    ? generationForm.knowledge_resource_ids
    : taskDefaults
  if (knowledgeResourceIds.length) {
    return { knowledge_resource_ids: [...knowledgeResourceIds] }
  }
  return {}
}

function normalizedResourceIds(value: unknown) {
  return Array.isArray(value)
    ? [...new Set(value.map(id => String(id).trim()).filter(Boolean))]
    : []
}

function imageRevisionKnowledgeResourceIds(source: Artifact) {
  const sourceResourceIds = normalizedResourceIds(source.content.knowledge_resource_ids)
  if (sourceResourceIds.length) {
    return sourceResourceIds
  }
  const currentResourceIds = normalizedResourceIds(generationForm.knowledge_resource_ids)
  if (currentResourceIds.length) {
    return currentResourceIds
  }
  return task.value ? taskKnowledgeResourceIds(task.value) : []
}

function imageRevisionSize(source: Artifact) {
  const size = typeof source.content.size === 'string' ? source.content.size.trim() : ''
  if (size) {
    return size
  }
  const width = Number(source.content.width)
  const height = Number(source.content.height)
  if (Number.isFinite(width) && Number.isFinite(height) && width >= 32 && height >= 32) {
    return `${Math.round(width)}x${Math.round(height)}`
  }
  return generationForm.image_size
}

function generationParameters(operation?: string) {
  if (activeStageKey.value === 'image') {
    return {
      ...cleanParameters({
        prompt: generationForm.image_prompt,
        scene: generationForm.image_scene,
        style: generationForm.image_style,
        ratio: generationForm.image_ratio,
        size: generationForm.image_size,
        reference_artifact_ids: generationForm.reference_artifact_ids,
        reference_asset_ids: generationForm.reference_asset_ids,
      }),
      form_state: imageGenerationFormState(generationForm),
      ...generationKnowledgeParameters(),
    }
  }
  if (activeStageKey.value === 'copy') {
    return {}
  }
  if (activeStageKey.value === 'voice_subtitles') {
    return cleanParameters({
      operation,
      voice: generationForm.voice,
      speed: generationForm.speed,
      volume: generationForm.volume,
      audio_format: generationForm.audio_format,
      sample_rate: numericParameter(generationForm.sample_rate),
      max_chars_per_line: generationForm.subtitle_max_chars_per_line,
      max_lines: generationForm.subtitle_max_lines,
    })
  }
  if (activeStageKey.value === 'video_preview') {
    return cleanParameters({
      operation,
      duration_seconds: numericParameter(generationForm.ai_video_duration),
      ratio: generationForm.ai_video_ratio,
      resolution: generationForm.ai_video_resolution,
      generate_audio: supportsParameter(aiVideoCapability.value, 'generate_audio')
        ? generationForm.generate_audio
        : undefined,
      return_last_frame: supportsParameter(aiVideoCapability.value, 'return_last_frame')
        ? generationForm.return_last_frame
        : undefined,
      width: numericParameter(generationForm.video_width),
      height: numericParameter(generationForm.video_height),
      fps: numericParameter(generationForm.video_fps),
      fit_mode: generationForm.fit_mode,
      transition: generationForm.transition,
      source_audio_policy: generationForm.source_audio_policy,
      source_asset_ids: generationForm.source_asset_ids,
      voice_asset_id: generationForm.voice_asset_id,
      uploaded_voice_text: generationForm.uploaded_voice_text,
      bgm_asset_id: generationForm.bgm_asset_id,
      background_music_volume: generationForm.background_music_volume,
      logo_asset_id: generationForm.logo_asset_id,
      logo_position: generationForm.logo_position,
      logo_width: generationForm.logo_width,
      intro_asset_id: generationForm.intro_asset_id,
      intro_duration_seconds: generationForm.intro_duration_seconds,
      outro_asset_id: generationForm.outro_asset_id,
      outro_duration_seconds: generationForm.outro_duration_seconds,
      subtitle_style: cleanParameters({
        font_size: generationForm.subtitle_font_size,
        primary_color: generationForm.subtitle_primary_color,
        outline_color: generationForm.subtitle_outline_color,
        margin_v: generationForm.subtitle_margin_vertical,
      }),
    })
  }
  return {}
}

async function generateStage(operation?: string, parameterOverrides: Record<string, unknown> = {}) {
  if (!task.value || busy.value === 'checking-service' || !await ensureGenerationAvailable(operation)) {
    return
  }
  if (activeStageKey.value === 'image' && !validateImageGenerationParameters()) {
    return
  }
  busy.value = `generate-${operation || activeStageKey.value}`
  activeRun.value = undefined
  try {
    if (!await saveStageRequirements()) {
      return
    }
    const instruction
      = activeStageKey.value === 'voice_subtitles'
        ? operation === 'voice'
          ? generationForm.voice_text
          : ''
        : stageRequirements.value
    const run = await contentApi.generateStage(task.value.id, activeStageKey.value, {
      count: generationForm.count ?? 3,
      instruction,
      source_artifact_id: generationForm.source_artifact_id || undefined,
      parameters: { ...generationParameters(operation), ...parameterOverrides },
      idempotency_key: createIdempotencyKey(task.value.id, activeStageKey.value),
    })
    activeRun.value = run
    const jobIds = run.job_ids?.length ? run.job_ids : run.job_id ? [run.job_id] : []
    for (const jobId of jobIds) {
      await waitForJob(jobId)
    }
    await refreshWorkflowAndArtifacts()
    ElMessage.success(run.status === 'succeeded' ? '生成完成，请查看并选择结果' : '生成任务已提交')
  }
  catch (error) {
    await handleWorkflowMutationError(error, { reconcileOnAnyError: true })
  }
  finally {
    await refreshServiceState()
    busy.value = ''
  }
}

async function waitForJob(jobId: string) {
  for (let index = 0; index < 180; index += 1) {
    const job = await contentApi.getJob(jobId)
    activeJob.value = job
    if (job.status === 'succeeded') {
      return
    }
    if (['failed', 'cancelled'].includes(job.status)) {
      const message
        = job.error || job.status === 'failed'
          ? generationJobError(job)
          : `后台任务${displayLabel(job.status, jobStatusLabels, '状态待确认')}`
      throw ['topic', 'copy', 'image', 'video_preview'].includes(activeStageKey.value)
        ? new ContentGenerationError(job, message)
        : new Error(message)
    }
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error('任务仍在处理中，可稍后回到本任务继续查看')
}

async function cancelJob() {
  if (!activeJob.value) {
    return
  }
  try {
    activeJob.value = await contentApi.cancelJob(activeJob.value.id)
    ElMessage.success('已提交取消请求')
  }
  catch (error) {
    await handleWorkflowMutationError(error, { reconcileOnAnyError: true })
  }
}

async function retryJob() {
  if (!activeJob.value) {
    return
  }
  try {
    activeJob.value = await contentApi.retryJob(activeJob.value.id)
    await waitForJob(activeJob.value.id)
    await refreshWorkflowAndArtifacts()
  }
  catch (error) {
    await handleWorkflowMutationError(error, { reconcileOnAnyError: true })
  }
}

async function flushArtifactSelection() {
  return artifactSelectionQueue.flush()
}

function toggleArtifact(artifact: Artifact) {
  if (!task.value || !currentStage.value || busy.value) {
    return
  }
  const previous = [...selectedArtifactIds.value]
  const selectableArtifacts = currentArtifacts.value.filter(
    item => !['legacy_mock', 'stale', 'discarded'].includes(item.status),
  )
  if (
    activeStageKey.value === 'voice_subtitles'
    && artifact.kind === 'subtitle'
    && !previous.includes(artifact.id)
    && !selectableArtifacts.some(
      item => item.kind === 'voice' && item.id === artifact.content.source_artifact_id,
    )
  ) {
    ElMessage.warning('该字幕关联的配音版本已不可用，请重新生成字幕')
    return
  }
  const next = nextArtifactSelection(
    previous,
    artifact,
    activeStageKey.value,
    selectableArtifacts,
  )
  if (next.length === previous.length && next.every((id, index) => id === previous[index])) {
    return
  }
  selectedArtifactIds.value = next
  artifactSelectionQueue.enqueue({
    taskId: task.value.id,
    stageKey: activeStageKey.value,
    artifactIds: next,
  })
}

async function chooseCandidate(artifact: Artifact, index: number) {
  const candidate = artifact.content.candidates?.[index]
  if (!candidate) {
    return
  }
  busy.value = 'candidate'
  try {
    if (!await flushArtifactSelection()) {
      return
    }
    const updated = await contentApi.patchArtifact(
      artifact.id,
      {
        ...artifact.content,
        selected_candidate_index: index,
        selected_candidate: candidate,
      },
      artifact.version,
      artifact.linked ? task.value?.id : undefined,
    )
    await refreshWorkflowAndArtifacts()
    selectedArtifactIds.value = [updated.id]
    ElMessage.success('候选内容已选中，并保存为新版本')
  }
  catch (error) {
    await handleWorkflowMutationError(error)
  }
  finally {
    busy.value = ''
  }
}

async function continueFromCurrentCopy() {
  if (!task.value || !currentStage.value) {
    return
  }
  if (!selectedArtifactIds.value.length) {
    ElMessage.warning('请先生成文案或选择一个文案版本')
    return
  }
  busy.value = 'continue-copy'
  try {
    if (!await flushArtifactSelection()) {
      return
    }
    ElMessage.success('已使用当前文案')
    await navigateToNextStage()
  }
  catch (error) {
    await handleWorkflowMutationError(error)
  }
  finally {
    busy.value = ''
  }
}

async function confirmCurrentStage() {
  if (!task.value || !currentStage.value) {
    return
  }
  if (activeStageKey.value === 'copy') {
    await continueFromCurrentCopy()
    return
  }
  if (
    activeStageKey.value !== 'configuration'
    && activeStageKey.value !== 'export'
    && !selectedArtifactIds.value.length
  ) {
    ElMessage.warning('请先选择本阶段确认使用的结果')
    return
  }
  busy.value = 'confirm'
  try {
    if (!await flushArtifactSelection()) {
      return
    }
    if (activeStageKey.value === 'configuration') {
      await saveConfiguration()
    }
    const artifactIds = selectedArtifactIds.value
    const latestStage = workflow.value?.stages.find(item => item.key === activeStageKey.value)
    if (!latestStage) {
      throw new Error('阶段状态已变化，请刷新后重试')
    }
    await contentApi.confirmStage(task.value.id, activeStageKey.value, {
      expected_version: latestStage.version,
      artifact_ids: artifactIds.length ? artifactIds : undefined,
    })
    await refreshWorkflowAndArtifacts()
    ElMessage.success('本阶段已确认')
    await navigateToNextStage()
  }
  catch (error) {
    await handleWorkflowMutationError(error)
  }
  finally {
    busy.value = ''
  }
}

async function skipCurrentStage() {
  if (!task.value || !currentStage.value) {
    return
  }
  try {
    if (!await flushArtifactSelection()) {
      return
    }
    await ElMessageBox.confirm('跳过后将直接使用任务主题进入下一阶段，是否继续？', '跳过当前阶段', {
      type: 'warning',
    })
    await contentApi.skipStage(task.value.id, activeStageKey.value, currentStage.value.version)
    await refreshWorkflowAndArtifacts()
    await navigateToNextStage()
  }
  catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      await handleWorkflowMutationError(error)
    }
  }
}

async function navigateToStage(key: StageKey) {
  if (!task.value) {
    return
  }
  if (!await flushArtifactSelection()) {
    return
  }
  if (taskArchived.value && key !== 'export') {
    try {
      await ElMessageBox.confirm(
        '原归档内容保持不变。修改内容只有在最终确认后才另存为新任务；未确认就退出，不会增加任务。是否开始修改？',
        '修改归档内容',
        { type: 'info', confirmButtonText: '开始修改', cancelButtonText: '取消' },
      )
      const draft = await contentApi.restoreTask(task.value.id)
      // Route to the independent draft without replacing the archive's local
      // task/workflow pair or mounting its editor with the old route taskId.
      await router.push(`/agent/content/tasks/${draft.id}/stages/${key}`)
      return
    }
    catch (error) {
      if (error !== 'cancel' && error !== 'close') {
        ElMessage.error(messageOf(error))
      }
      return
    }
  }
  await router.push(`/agent/content/tasks/${task.value.id}/stages/${key}`)
}

async function navigateToNextStage() {
  const index = visibleStageKeys.value.indexOf(activeStageKey.value)
  const next = visibleStageKeys.value[index + 1]
  if (next) {
    await navigateToStage(next)
  }
}

async function handleVideoTaskCreated(taskId: string) {
  const sourcePath = router.currentRoute.value.fullPath
  const targetPath = `/agent/content/tasks/${taskId}/stages/video_preview`
  try {
    const [createdTask, createdWorkflow, createdArtifacts] = await Promise.all([
      contentApi.getTask(taskId),
      contentApi.getWorkflow(taskId),
      contentApi.listArtifacts(taskId),
    ])
    if (router.currentRoute.value.fullPath !== sourcePath) {
      return
    }
    // Populate the parent before taskId changes, so the existing editor's v-if stays true.
    task.value = createdTask
    workflow.value = createdWorkflow
    artifacts.value = createdArtifacts
    beginVideoEditorRouteHandoff(
      sourcePath,
      taskId,
      props.taskId
        ? `${props.taskId}:${props.mode}`
        : contentCreateViewKey(props.mode ?? currentMode.value, router.currentRoute.value.query.new),
    )
    await router.replace(targetPath)
  }
  catch {
    // The task is already saved. Keep its canonical address even if this optional read failed.
    if (router.currentRoute.value.fullPath === sourcePath) {
      await router.replace(targetPath)
    }
  }
}

async function handleWorkflowVideoCompleted(
  payload: { artifactId: string, baseline: WorkflowStageBaseline },
) {
  if (!task.value || !['full', 'video'].includes(currentMode.value)) {
    return
  }
  const showConfirmed = (message: string) => {
    ElMessage.success(message)
    navigateToStage('export')
  }
  busy.value = 'confirm-video'
  try {
    await refreshWorkflowAndArtifacts()
    const stage = workflow.value?.stages.find(item => item.key === 'video_preview')
    if (!stage) {
      throw new Error('视频生成阶段不存在，请刷新任务后重试')
    }
    const decision = workflowFinalizationDecision(stage, payload.baseline, payload.artifactId)
    if (decision === 'already_confirmed') {
      showConfirmed('视频成片已确认')
      return
    }
    if (decision === 'conflict') {
      throw new Error('视频渲染期间阶段选择或确认结果已发生变化，请人工复核；系统未覆盖现有选择')
    }

    let selectedStage = stage
    if (decision === 'select_and_confirm') {
      try {
        selectedStage = await contentApi.selectStageArtifacts(task.value.id, 'video_preview', {
          artifact_ids: [payload.artifactId],
          expected_version: stage.version,
        })
      }
      catch (error) {
        if (!isVersionConflictError(error)) {
          throw error
        }
        await refreshWorkflowAndArtifacts()
        const latestStage = workflow.value?.stages.find(item => item.key === 'video_preview')
        if (latestStage && isOnlyWorkflowArtifact(
          stableWorkflowArtifactIds(latestStage.confirmed_artifact_ids),
          payload.artifactId,
        )) {
          showConfirmed('视频成片已由其他操作确认')
          return
        }
        throw new Error('视频渲染完成时阶段已被其他操作修改，请人工复核；系统未覆盖现有选择')
      }
    }

    let expectedVersion = selectedStage.version
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await contentApi.confirmStage(task.value.id, 'video_preview', {
          expected_version: expectedVersion,
        })
        break
      }
      catch (error) {
        if (!isVersionConflictError(error)) {
          throw error
        }
        await refreshWorkflowAndArtifacts()
        const latestStage = workflow.value?.stages.find(item => item.key === 'video_preview')
        if (!latestStage) {
          throw new Error('视频生成阶段不存在，请刷新任务后重试')
        }
        const retryDecision = finalConfirmationRetryDecision(latestStage, payload.artifactId)
        if (retryDecision === 'already_confirmed') {
          showConfirmed('视频成片已由其他操作确认')
          return
        }
        if (retryDecision !== 'confirm_selected' || attempt === 1) {
          throw new Error('确认期间最终成片选择已被其他操作替换，请人工复核；系统未覆盖现有选择')
        }
        expectedVersion = latestStage.version
      }
    }

    await refreshWorkflowAndArtifacts()
    const confirmedStage = workflow.value?.stages.find(item => item.key === 'video_preview')
    const latestConfirmedIds = stableWorkflowArtifactIds(confirmedStage?.confirmed_artifact_ids)
    if (!isOnlyWorkflowArtifact(latestConfirmedIds, payload.artifactId)) {
      throw new Error('视频阶段确认结果发生变化，请人工复核；系统未覆盖现有选择')
    }
    showConfirmed('视频成片已确认，正在进入导出')
  }
  catch (error) {
    await reconcileWorkflowState()
    ElMessage.error(friendlyContentError(error, '视频已渲染，但确认失败，请重试'))
  }
  finally {
    busy.value = ''
  }
}

async function handleWorkflowVideoBack() {
  await refreshWorkflowAndArtifacts()
  navigateToStage(currentMode.value === 'full' ? 'image' : 'voice_subtitles')
}

async function handleWorkflowCopyEdit() {
  await refreshWorkflowAndArtifacts()
  navigateToStage('copy')
}

async function handleVideoEditorHome() {
  // Replace the hidden create route so the shortcut editor always returns to
  // the content home and does not leave an empty draft in browser history.
  await router.replace('/agent/content')
}

async function runCompliance(artifact?: Artifact) {
  if (artifact) {
    busy.value = 'compliance'
    try {
      const result = await contentApi.compliance(artifact.id)
      complianceResults[result.artifact_id] = result
      ElMessage.success('检测完成，系统未自动修改任何内容')
    }
    catch (error) {
      ElMessage.error(messageOf(error))
    }
    finally {
      busy.value = ''
    }
    return
  }

  if (!copyArtifacts.value.length) {
    ElMessage.warning('请先生成或选择文案版本')
    return
  }
  busy.value = 'compliance'
  try {
    if (!await flushArtifactSelection()) {
      return
    }
    await refreshWorkflowAndArtifacts()
    if (!task.value || activeStageKey.value !== 'compliance') {
      return
    }
    const copyStage = workflow.value?.stages.find(item => item.key === 'copy')
    const copySelection = selectedIds(copyStage?.selected_artifact_ids)
    const sourceId = copySelection.length === 1 ? copySelection[0] : undefined
    const run = await contentApi.generateStage(task.value.id, 'compliance', {
      count: 1,
      instruction: stageRequirements.value,
      ...(sourceId ? { source_artifact_id: sourceId } : {}),
      idempotency_key: createIdempotencyKey(task.value.id, 'compliance'),
    })
    await refreshWorkflowAndArtifacts()
    const complianceArtifactIds = run.artifact_ids.filter(id =>
      artifacts.value.some(item => item.id === id && item.kind === 'compliance'),
    )
    const refreshedStage = workflow.value?.stages.find(item => item.key === 'compliance')
    if (!refreshedStage || !complianceArtifactIds.length) {
      throw new Error('检测结果未正确保存，请刷新后重试')
    }
    await contentApi.selectStageArtifacts(task.value.id, 'compliance', {
      artifact_ids: complianceArtifactIds,
      expected_version: refreshedStage.version,
    })
    await refreshWorkflowAndArtifacts()
    ElMessage.success('检测完成，系统未自动修改任何内容')
  }
  catch (error) {
    await handleWorkflowMutationError(error, { reconcileOnAnyError: true })
  }
  finally {
    busy.value = ''
  }
}

function openArtifactEditor(artifact: Artifact) {
  artifactEditorSession += 1
  editingArtifact.value = artifact
  artifactDraft.value = JSON.parse(JSON.stringify(artifact.content)) as ArtifactContent
  transformOperation.value = 'rewrite'
  transformInstruction.value
    = artifact.kind === 'topic' ? '' : '请保持品牌事实准确，并按要求调整表达'
  imageRevisionInstruction.value = ''
  imageRevisionNotice.value = ''
  editDialogOpen.value = true
  if (artifact.kind === 'image' && !mediaUrls[artifact.id]) {
    void loadArtifactPreview(artifact)
  }
}

async function generateImageRevision() {
  const source = editingArtifact.value
  const instruction = imageRevisionInstruction.value.trim()
  if (!task.value || source?.kind !== 'image') {
    return
  }
  if (!instruction) {
    ElMessage.warning('请先填写图片修改要求')
    return
  }
  if (busy.value === 'checking-service' || !await ensureGenerationAvailable()) {
    return
  }
  if (imageReferenceLimit.value == null || imageReferenceLimit.value < 1) {
    ElMessage.warning('当前图片模型不支持参考图片，无法基于现有图片继续生成')
    return
  }
  busy.value = 'image-revision'
  activeRun.value = undefined
  try {
    if (!await flushArtifactSelection()) {
      return
    }
    const revisionPrompt = activeStageKey.value === 'image'
      && ['full', 'image'].includes(currentMode.value)
      ? instruction
      : [
          '以当前参考图片为基础生成新版本，保留未明确要求修改的主体、构图和品牌元素。',
          `本次必须执行的修改要求：${instruction}`,
        ].join('\n')
    const run = await contentApi.generateStage(task.value.id, 'image', {
      count: 1,
      instruction,
      source_artifact_id: source.id,
      parameters: {
        ...cleanParameters({
          prompt: revisionPrompt,
          ratio: typeof source.content.ratio === 'string' ? source.content.ratio : generationForm.image_ratio,
          size: imageRevisionSize(source),
        }),
        reference_artifact_ids: [source.id],
        reference_asset_ids: [],
        knowledge_resource_ids: imageRevisionKnowledgeResourceIds(source),
        form_state: imageGenerationFormState(generationForm),
      },
      idempotency_key: createIdempotencyKey(task.value.id, 'image', revisionPrompt),
    })
    activeRun.value = run
    const jobIds = run.job_ids?.length ? run.job_ids : run.job_id ? [run.job_id] : []
    for (const jobId of jobIds) {
      await waitForJob(jobId)
    }
    await refreshWorkflowAndArtifacts()
    const generated = artifacts.value.filter(artifact => run.artifact_ids.includes(artifact.id))
    imageRevisionNotice.value = generated.length
      ? `已生成新版本：${generated.map(item => `图片版本 ${item.version}`).join('、')}。请在生成结果中预览并确认。`
      : '新图片版本已生成，请在生成结果中预览并确认。'
    editDialogOpen.value = false
    ElMessage.success(imageRevisionNotice.value)
  }
  catch (error) {
    await handleWorkflowMutationError(error, { reconcileOnAnyError: true })
  }
  finally {
    await refreshServiceState()
    busy.value = ''
  }
}

async function saveArtifactEdit() {
  if (!editingArtifact.value || busy.value === 'artifact-save' || busy.value === 'transform') {
    return
  }
  if (!hasArtifactDraftChanges.value) {
    ElMessage.info('内容没有变化，无需重复保存新版本')
    return
  }
  busy.value = 'artifact-save'
  try {
    if (!await flushArtifactSelection()) {
      return
    }
    const nextContent = editingArtifact.value.kind === 'copy'
      ? copyContentWithConfirmedTitle(artifactDraft.value)
      : artifactDraft.value
    const updated = await contentApi.patchArtifact(
      editingArtifact.value.id,
      nextContent,
      editingArtifact.value.version,
      editingArtifact.value.linked ? task.value?.id : undefined,
    )
    await refreshWorkflowAndArtifacts()
    editingArtifact.value = updated
    artifactDraft.value = JSON.parse(JSON.stringify(updated.content)) as ArtifactContent
    ElMessage.success('人工修改已保存为新版本')
  }
  catch (error) {
    await handleWorkflowMutationError(error)
  }
  finally {
    busy.value = ''
  }
}

async function transformCopy() {
  const source = editingArtifact.value
  if (!source || !['topic', 'copy'].includes(source.kind) || busy.value) {
    return
  }
  const editorSession = artifactEditorSession
  const taskId = task.value?.id
  const operation = transformOperation.value
  const instruction = transformInstruction.value
  const draftSnapshot = JSON.stringify(artifactDraft.value)
  const sourceContent = JSON.parse(draftSnapshot) as ArtifactContent
  const draftContent = source.kind === 'copy'
    ? copyContentWithConfirmedTitle(sourceContent)
    : sourceContent
  const isCurrentPreview = () => editDialogOpen.value
    && artifactEditorSession === editorSession
    && editingArtifact.value?.id === source.id
    && task.value?.id === taskId
  busy.value = 'transform'
  try {
    // AI operates on the visible draft without creating/selecting a version.
    // Only the explicit save action persists the user's final draft.
    const preview = await contentApi.previewTransformCopy(
      source.id,
      operation,
      instruction,
      source.linked ? taskId : undefined,
      draftContent,
      source.version,
    )
    if (!isCurrentPreview()) {
      return
    }
    if (JSON.stringify(artifactDraft.value) !== draftSnapshot) {
      ElMessage.info('生成期间内容已被修改，已保留你的修改；如需继续 AI 调整，请重新生成')
      return
    }
    if (!hasMaterialTransformChange(
      source.kind,
      draftContent,
      preview.content,
      instruction,
    )) {
      throw new Error('AI 未按要求修改内容，请补充或调整指令后重试')
    }
    const previewContent = source.kind === 'copy'
      ? copyContentWithConfirmedTitle(preview.content)
      : preview.content
    artifactDraft.value = JSON.parse(JSON.stringify(previewContent)) as ArtifactContent
    ElMessage.success('AI 内容已生成，请检查或修改后点击保存')
  }
  catch (error) {
    if (isCurrentPreview()) {
      ElMessage.error(messageOf(error))
    }
  }
  finally {
    if (busy.value === 'transform') {
      busy.value = ''
    }
  }
}

async function removeArtifact(artifact: Artifact) {
  const currentlySelected = selectedArtifactIds.value.includes(artifact.id)
  const currentlyConfirmed = currentStageConfirmedArtifactIds.value.has(artifact.id)
  const removableStatus = ['draft', 'selected', 'confirmed'].includes(artifact.status)
  if (currentlySelected || currentlyConfirmed || !removableStatus) {
    ElMessage.warning(
      currentlySelected
        ? artifact.kind === 'copy'
          ? '当前文案不能删除，请先选择其他版本'
          : '当前使用中的版本不能删除，请先选择其他版本'
        : currentlyConfirmed
          ? '已确认版本不能删除'
          : '只有待确认的草稿版本可以删除',
    )
    return
  }
  try {
    await ElMessageBox.confirm('确定删除这个未确认版本吗？', '删除内容版本', { type: 'warning' })
    if (!await flushArtifactSelection()) {
      return
    }
    await contentApi.deleteArtifact(artifact.id)
    await refreshWorkflowAndArtifacts()
  }
  catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(messageOf(error))
    }
  }
}

async function downloadArtifact(artifact: Artifact, format?: string) {
  try {
    const { blob, fileName } = await contentApi.downloadArtifact(artifact.id, format)
    const fallbackName
      = `${displayLabel(artifact.kind, artifactKindLabels, '内容')}-版本-${artifact.version}`
    saveBlob(
      blob,
      fileName
      || (format ? `${fallbackName}.${format}` : fallbackName),
    )
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
}

async function downloadFullArtifact() {
  const artifact = selectedFullExportArtifact.value
  if (!artifact) {
    ElMessage.warning('当前版本暂无可导出的内容')
    return
  }
  if (!exportStageConfirmed.value) {
    ElMessage.warning('请先确认导出阶段，再下载最终文件')
    return
  }
  busy.value = `export-single-${artifact.id}`
  try {
    await downloadArtifact(artifact, fullSingleExportFormat(artifact))
  }
  finally {
    busy.value = ''
  }
}

async function loadArtifactPreview(artifact: Artifact) {
  try {
    const { blob } = await contentApi.downloadArtifact(artifact.id)
    if (mediaUrls[artifact.id]) {
      URL.revokeObjectURL(mediaUrls[artifact.id])
    }
    mediaUrls[artifact.id] = URL.createObjectURL(blob)
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
}

async function saveStoryboard() {
  if (!selectedStoryboard.value) {
    return
  }
  busy.value = 'storyboard-edit'
  try {
    if (!await flushArtifactSelection()) {
      return
    }
    const updated = await contentApi.patchArtifact(
      selectedStoryboard.value.id,
      {
        ...selectedStoryboard.value.content,
        shots: storyboardDraft.value,
      },
      selectedStoryboard.value.version,
      selectedStoryboard.value.linked ? task.value?.id : undefined,
    )
    await refreshWorkflowAndArtifacts()
    selectedArtifactIds.value = [updated.id]
    storyboardDraft.value = normalizeShots(updated.content)
    ElMessage.success('分镜修改已保存为新版本')
  }
  catch (error) {
    await handleWorkflowMutationError(error)
  }
  finally {
    busy.value = ''
  }
}

async function exportDirectFiles() {
  if (!task.value || !directExportKind.value) {
    return
  }
  if (!exportStageConfirmed.value) {
    ElMessage.warning('请先确认导出阶段，再下载最终文件')
    return
  }
  if (!directExportArtifacts.value.length) {
    ElMessage.warning('请先确认要导出的最终版本')
    return
  }
  busy.value = 'export-direct'
  try {
    const selectedFormat = currentMode.value === 'video' ? undefined : exportFormat.value
    for (const artifact of directExportArtifacts.value) {
      const { blob, fileName } = await contentApi.downloadArtifact(artifact.id, selectedFormat)
      const extension = selectedFormat || (artifact.kind === 'video' ? 'mp4' : 'bin')
      saveBlob(
        blob,
        fileName
        || `${task.value.name}-${displayLabel(artifact.kind, artifactKindLabels, '内容')}-v${artifact.version}.${extension}`,
      )
    }
    ElMessage.success(
      directExportArtifacts.value.length > 1
        ? `已导出 ${directExportArtifacts.value.length} 个已确认文件`
        : '文件已导出',
    )
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
  finally {
    busy.value = ''
  }
}

async function exportTaskBundle() {
  if (!task.value) {
    return
  }
  if (!exportStageConfirmed.value) {
    ElMessage.warning('请先确认导出阶段，再下载素材包')
    return
  }
  busy.value = 'export-bundle'
  try {
    const { blob, fileName } = await contentApi.exportTask(task.value.id)
    saveBlob(blob, fileName || `${task.value.name}-当前成品包.zip`)
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
  finally {
    busy.value = ''
  }
}

function openLinkDialog() {
  linkTargetTaskId.value = linkTargetTasks.value[0]?.id ?? ''
  linkArtifactIds.value = linkableArtifacts.value.map(item => item.id)
  linkDialogOpen.value = true
}

async function linkToFullTask() {
  if (!task.value || !linkTargetTaskId.value || !linkArtifactIds.value.length) {
    ElMessage.warning('请选择目标内容任务和至少一个已确认结果')
    return
  }
  const target = allTasks.value.find(item => item.id === linkTargetTaskId.value)
  if (!target || target.can_edit === false) {
    ElMessage.error('目标任务不存在或没有编辑权限，请重新选择')
    return
  }
  busy.value = 'link-artifacts'
  try {
    await contentApi.linkArtifacts(target.id, {
      source_task_id: task.value.id,
      artifact_ids: linkArtifactIds.value,
      expected_version: target.version,
    })
    linkDialogOpen.value = false
    ElMessage.success('已通过引用关系加入目标内容任务')
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
  finally {
    busy.value = ''
  }
}

watch(editDialogOpen, (open) => {
  if (!open) {
    artifactEditorSession += 1
  }
}, { flush: 'sync' })

watch(
  currentStage,
  (value) => {
    stageRequirements.value = value?.requirements ?? ''
    const persistedIds = selectedIds(value?.selected_artifact_ids)
    lastPersistedSelectionIds.value = persistedIds
    if (!selectionSaving.value) {
      selectedArtifactIds.value = persistedIds
    }
  },
  { immediate: true },
)

watch(
  selectedStoryboard,
  (value) => {
    storyboardDraft.value = value ? normalizeShots(value.content) : []
  },
  { immediate: true },
)

watch(
  currentMode,
  (mode) => {
    exportFormat.value = mode === 'image' ? 'png' : 'docx'
  },
  { immediate: true },
)

watch(
  candidateCountRange,
  (range) => {
    if (!range) {
      generationForm.count = undefined
      return
    }
    if (
      generationForm.count == null
      || (range.min != null && generationForm.count < range.min)
      || (range.max != null && generationForm.count > range.max)
    ) {
      generationForm.count = range.min ?? range.max
    }
  },
  { immediate: true },
)

const initializedImageDefaults = ref(false)
watch(
  [activeStageKey, activeCapability],
  ([stage, capability]) => {
    if (stage !== 'image' || !capability || initializedImageDefaults.value) {
      return
    }
    generationForm.image_ratio ||= parameterDefault(capability, 'ratio')
    generationForm.image_size ||= parameterDefault(capability, 'size')
    initializedImageDefaults.value = true
  },
  { immediate: true },
)

watch(
  () => props.stageKey,
  async (stage, previous) => {
    if (stage === previous) {
      return
    }
    editDialogOpen.value = false
    if (!props.taskId || !generationHistoryStages.has(stage)) {
      return
    }
    try {
      const generationRuns = await contentApi.listGenerationRuns(props.taskId, {
        stageKey: stage,
        status: 'succeeded',
        pageSize: stage === 'image' ? 10 : 1,
      })
      const latestRequestedCount = latestGenerationRequestedCount(generationRuns)
      if (latestRequestedCount != null) {
        generationForm.count = latestRequestedCount
      }
      if (stage === 'image') {
        Object.assign(
          generationForm,
          recoverImageGenerationDraft(generationRuns, artifacts.value),
        )
      }
    }
    catch {
      // Keep the current generation form if history is unavailable.
    }
  },
)

let skipNextActivate = false
onMounted(() => {
  skipNextActivate = true
  if (props.taskId) {
    existingScriptText.value
      = sessionStorage.getItem(importDraftKey(props.taskId, 'storyboard')) ?? ''
  }
  return props.taskId ? loadCore() : loadCreationSupport()
})
onActivated(() => {
  if (skipNextActivate) {
    skipNextActivate = false
    if (!props.taskId) {
      resetCreationForm()
    }
    return
  }
  if (!props.taskId) {
    resetCreationForm()
    return
  }
  void refreshWorkflowAndArtifacts()
})
onBeforeUnmount(() => {
  artifactEditorSession += 1
  void artifactSelectionQueue.flush()
  Object.values(mediaUrls).forEach(url => URL.revokeObjectURL(url))
})
</script>

<template>
  <ElSkeleton
    v-if="taskId && stageKey === 'video_preview' && !task && !pageError"
    :rows="8"
    animated
    role="status"
    aria-label="正在加载视频任务"
  />
  <div
    v-else
    class="content-task-view"
    :class="{ 'has-video-editor': showVideoEditor }"
    :aria-busy="loading || Boolean(busy) || selectionSaving"
  >
    <ElAlert
      v-if="taskEditDraft"
      type="info"
      title="正在修改归档内容"
      description="原归档内容不会改变。完成最终确认后另存为新任务；未确认就退出，不会增加任务。"
      :closable="false"
      show-icon
    />
    <VideoEditorWorkspace
      v-if="showVideoEditor"
      :task-id="taskId"
      :initial-task="task"
      :workflow-mode="currentMode === 'video' || activeStageKey === 'video_preview'"
      @task-created="handleVideoTaskCreated"
      @workflow-completed="handleWorkflowVideoCompleted"
      @workflow-back="handleWorkflowVideoBack"
      @workflow-edit-copy="handleWorkflowCopyEdit"
      @editor-home="handleVideoEditorHome"
    />
    <section v-if="!showVideoEditor" class="task-heading">
      <div>
        <span>{{ modeLabels[currentMode] }}</span>
        <h2>{{ task?.name || `新建${modeLabels[currentMode]}` }}</h2>
      </div>
      <div class="task-heading-actions">
        <ElTag v-if="taskArchived" type="info">
          已归档
        </ElTag>
        <ElTag v-else-if="taskEditDraft" type="info">修改草稿</ElTag>
        <ElTag v-else-if="task" :type="taskWorkflowStatus === 'confirmed' ? 'success' : 'info'">
          {{ displayLabel(taskWorkflowStatus, taskStatusLabels, '其他状态') }}
        </ElTag>
        <ElTag v-else type="warning" effect="light">配置中</ElTag>
        <ElButton @click="router.push('/agent/content')">返回工作台</ElButton>
      </div>
    </section>

    <ElAlert
      v-if="!showVideoEditor && pageError"
      type="error"
      title="任务工作流不可用"
      :description="pageError"
      :closable="false"
      show-icon
    />
    <ElAlert
      v-else-if="!showVideoEditor && supportWarning"
      type="warning"
      title="部分辅助服务不可用"
      :description="supportWarning"
      :closable="false"
      show-icon
    />
    <ElAlert
      v-if="!showVideoEditor && archivedReadOnly"
      type="info"
      title="当前为已归档历史版本"
      description="此版本保持归档时的内容。需要修改可点击前面的步骤；最终确认后另存为新任务，未确认就退出不会增加任务。"
      :closable="false"
      show-icon
    />

    <template v-if="!showVideoEditor && !taskId">
      <div class="workflow-layout">
        <nav class="stage-navigation" aria-label="内容生产流程">
          <button
            v-for="(key, index) in modeStageKeys[currentMode]"
            :key="key"
            type="button"
            :class="{ active: key === 'configuration' }"
            :disabled="key !== 'configuration'"
            :aria-current="key === 'configuration' ? 'step' : undefined"
            :aria-label="
              key === 'configuration'
                ? '当前阶段：任务配置'
                : `${stageDefinition(key).label}，创建任务后可进入`
            "
          >
            <span class="stage-number">{{ index + 1 }}</span>
            <span class="stage-name">
              <strong>{{ stageDefinition(key).label }}</strong>
              <small>{{ key === 'configuration' ? '进行中' : '未开始' }}</small>
            </span>
            <ElTag size="small" :type="key === 'configuration' ? 'warning' : 'info'" effect="plain">
              {{ key === 'configuration' ? '进行中' : '未开始' }}
            </ElTag>
          </button>
        </nav>

        <main class="stage-content">
          <header class="stage-heading">
            <div>
              <span>阶段 1 / {{ modeStageKeys[currentMode].length }}</span>
              <h3>任务配置</h3>
            </div>
            <ElTag type="warning" effect="light">进行中</ElTag>
          </header>

          <section class="stage-panel">
            <TaskConfigurationForm
              :model-value="taskForm"
              :content-type-options="contentTypeOptions"
              :style-options="styleOptions"
              :resources="resources"
              :show-length="currentMode !== 'image'"
              :show-requirements="currentMode === 'video'"
              @resources-refreshed="resources = $event"
              @update:model-value="updateTaskForm"
            />
            <ElForm v-if="props.mode === 'video'" label-position="top" class="existing-script-form">
              <ElFormItem label="已有脚本文本（可选）">
                <ElInput
                  v-model="existingScriptText"
                  type="textarea"
                  :rows="8"
                  placeholder="可粘贴已有短视频脚本；创建任务后将保存为可编辑分镜草稿"
                />
              </ElFormItem>
            </ElForm>
            <div class="panel-actions">
              <ElButton type="primary" :loading="busy === 'create-task'" @click="createTask">
                {{ creationGuidance[currentMode].action }}
              </ElButton>
            </div>
          </section>
        </main>
      </div>
    </template>

    <template v-else-if="!showVideoEditor && task && task.can_edit !== false && workflow">
      <div class="workflow-layout">
        <nav class="stage-navigation" aria-label="内容生产流程">
          <button
            v-for="(key, index) in visibleStageKeys"
            :key="key"
            type="button"
            :class="{ active: activeStageKey === key }"
            :aria-current="activeStageKey === key ? 'step' : undefined"
            @click="navigateToStage(key)"
          >
            <span class="stage-number">{{ index + 1 }}</span>
            <span class="stage-name"><strong>{{ stageDefinition(key).label }}</strong><small>{{ presentationStageLabel(key) }}</small></span>
            <ElTag size="small" :type="stateTagType(presentationStageState(key))" effect="plain">
              {{ presentationStageLabel(key) }}
            </ElTag>
          </button>
        </nav>

        <main class="stage-content">
          <header class="stage-heading">
            <div>
              <span>阶段 {{ visibleStageKeys.indexOf(activeStageKey) + 1 }} /
                {{ visibleStageKeys.length }}</span>
              <h3>{{ currentStageDefinition.label }}</h3>
            </div>
            <ElTag :type="stateTagType(presentationStageState(activeStageKey))" effect="light">
              {{ presentationStageLabel(activeStageKey) }}
            </ElTag>
          </header>

          <ElAlert
            v-if="showUpstreamChangedAlert"
            type="warning"
            title="上游已发生变化，本阶段结果需要重新检查或生成"
            :closable="false"
            show-icon
          />

          <section v-if="activeStageKey === 'configuration'" class="stage-panel">
            <TaskConfigurationForm
              :model-value="taskForm"
              :content-type-options="contentTypeOptions"
              :style-options="styleOptions"
              :resources="resources"
              :show-length="currentMode !== 'image'"
              :show-requirements="currentMode === 'video'"
              @resources-refreshed="resources = $event"
              @update:model-value="updateTaskForm"
            />
            <div class="panel-actions">
              <ElButton :loading="busy === 'save-configuration'" @click="saveConfiguration">
                保存修改
              </ElButton>
              <ElButton type="primary" :loading="busy === 'confirm'" @click="confirmCurrentStage">
                确认配置并继续
              </ElButton>
            </div>
          </section>

          <template v-else-if="activeStageKey === 'export'">
            <section class="stage-panel export-summary">
              <ElResult
                :icon="exportResultIcon"
                :title="exportResultTitle"
                :sub-title="exportResultDescription"
              >
                <template #extra>
                  <div
                    v-if="currentMode === 'full'"
                    class="full-export-controls"
                    aria-label="完整创作内容导出"
                  >
                    <div class="full-export-field">
                      <label for="full-export-artifact">导出内容</label>
                      <ElSelect
                        id="full-export-artifact"
                        :model-value="selectedFullExportArtifact?.id ?? ''"
                        :disabled="!exportStageConfirmed || !fullExportArtifacts.length"
                        placeholder="暂无可导出的内容"
                        aria-label="选择要导出的内容"
                        @update:model-value="selectedFullExportArtifactId = String($event)"
                      >
                        <ElOption
                          v-for="artifact in fullExportArtifacts"
                          :key="artifact.id"
                          :label="fullExportArtifactLabel(artifact)"
                          :value="artifact.id"
                        />
                      </ElSelect>
                    </div>
                    <div class="full-export-field">
                      <label for="full-export-format">导出格式</label>
                      <ElSelect
                        id="full-export-format"
                        :model-value="selectedFullExportFormat"
                        :disabled="!exportStageConfirmed || !selectedFullExportArtifact || selectedFullExportFormatOptions.length <= 1"
                        aria-label="选择所选内容的导出格式"
                        @update:model-value="selectedFullExportArtifact && updateFullSingleExportFormat(selectedFullExportArtifact.id, String($event))"
                      >
                        <ElOption
                          v-for="option in selectedFullExportFormatOptions"
                          :key="option.value"
                          :label="option.label"
                          :value="option.value"
                        />
                      </ElSelect>
                    </div>
                    <div class="full-export-primary-actions">
                      <ElButton
                        v-if="!archivedReadOnly && !exportStageConfirmed"
                        type="primary"
                        :loading="busy === 'confirm'"
                        @click="confirmCurrentStage"
                      >
                        确认并开放导出
                      </ElButton>
                      <ElButton
                        type="primary"
                        :disabled="!exportStageConfirmed || !selectedFullExportArtifact"
                        :loading="busy === `export-single-${selectedFullExportArtifact?.id}`"
                        @click="downloadFullArtifact"
                      >
                        导出所选内容
                      </ElButton>
                    </div>
                  </div>
                  <div v-else class="export-controls">
                    <div v-if="exportFormatOptions.length" class="export-format-field">
                      <label for="content-export-format">导出格式</label>
                      <ElSelect
                        id="content-export-format"
                        v-model="exportFormat"
                        aria-label="选择导出格式"
                      >
                        <ElOption
                          v-for="option in exportFormatOptions"
                          :key="option.value"
                          :label="option.label"
                          :value="option.value"
                        />
                      </ElSelect>
                    </div>
                    <div class="export-primary-actions">
                      <ElButton
                        v-if="!archivedReadOnly && !exportStageConfirmed"
                        type="primary"
                        :loading="busy === 'confirm'"
                        @click="confirmCurrentStage"
                      >
                        确认导出
                      </ElButton>
                      <ElButton
                        v-if="directExportKind"
                        :type="exportStageConfirmed ? 'primary' : 'default'"
                        :disabled="!exportStageConfirmed || !directExportArtifacts.length"
                        :loading="busy === 'export-direct'"
                        @click="exportDirectFiles"
                      >
                        {{ exportPresentation.button }}
                      </ElButton>
                    </div>
                  </div>
                  <div class="export-secondary-actions">
                    <ElButton
                      v-if="currentMode === 'full'"
                      :disabled="!exportStageConfirmed"
                      :loading="busy === 'export-bundle'"
                      @click="exportTaskBundle"
                    >
                      一键下载当前成品包（ZIP）
                    </ElButton>
                    <ElButton
                      v-if="currentMode !== 'full'"
                      :disabled="!exportStageConfirmed"
                      :loading="busy === 'export-bundle'"
                      @click="exportTaskBundle"
                    >
                      下载当前成品包（ZIP）
                    </ElButton>
                    <ElButton
                      v-if="currentMode !== 'full'"
                      :disabled="!linkableArtifacts.length || !linkTargetTasks.length"
                      @click="openLinkDialog"
                    >
                      加入完整创作或快捷视频
                    </ElButton>
                    <ElButton @click="router.push({ path: '/agent/content/versions', query: { taskId: task?.id } })">
                      查看全部历史版本
                    </ElButton>
                  </div>
                  <p v-if="currentMode !== 'full'" class="bundle-note">
                    {{ bundleDescription }}
                  </p>
                </template>
              </ElResult>
              <ElDescriptions :column="2" border>
                <ElDescriptionsItem label="任务">{{ task.name }}</ElDescriptionsItem>
                <ElDescriptionsItem label="模式">{{ modeLabels[task.mode] }}</ElDescriptionsItem>
                <ElDescriptionsItem label="确认阶段">
                  {{ workflow.stages.filter((item) =>
                    item.state === 'confirmed' && visibleStageKeys.includes(item.key)).length }} /
                  {{ visibleStageKeys.length }}
                </ElDescriptionsItem>
                <ElDescriptionsItem label="内容版本">{{ artifacts.length }}</ElDescriptionsItem>
              </ElDescriptions>
            </section>
          </template>

          <template v-else>
            <section class="stage-panel generation-panel">
              <div
                v-if="props.mode === 'video' && activeStageKey === 'storyboard'"
                class="existing-import-panel"
              >
                <div>
                  <h4>直接使用已有脚本</h4>
                </div>
                <ElInput v-model="existingScriptText" type="textarea" :rows="6" />
                <ElButton
                  type="primary"
                  :loading="busy === 'import-storyboard'"
                  @click="importExistingContent('storyboard')"
                >
                  保存为分镜草稿
                </ElButton>
              </div>
              <div v-if="activeStageKey !== 'copy'" class="generation-status">
                <div>
                  <span>阶段补充要求</span>
                  <ElTag v-if="activeStageKey === 'compliance'" type="success" effect="plain">
                    本地规则检测
                  </ElTag>
                  <ElTag
                    v-else-if="activeCapability"
                    type="success"
                    effect="plain"
                  >
                    {{ providerLabel(activeCapability.provider_id) }} · 可使用
                  </ElTag>
                  <ElTag v-else type="success" effect="plain">AI 功能可使用</ElTag>
                </div>
                <ElInput
                  v-model="stageRequirements"
                  type="textarea"
                  :rows="3"
                  placeholder="补充本阶段要求；保存后可重新生成"
                />
              </div>

              <div
                v-if="activeStageKey !== 'compliance' && activeStageKey !== 'copy'"
                class="generation-fields"
              >
                <ElFormItem v-if="candidateCountRange" label="本次生成数量">
                  <ElInputNumber
                    v-model="generationForm.count"
                    :min="candidateCountRange.min"
                    :max="candidateCountRange.max"
                  />
                </ElFormItem>
                <ElFormItem
                  v-if="activeStageKey === 'image'"
                  label="本次引用的知识库资料与素材（可多选）"
                  class="generation-instruction"
                >
                  <ContentKnowledgePicker
                    :model-value="generationForm.knowledge_resource_ids"
                    :resources="resources"
                    title="选择本次图片参考资料与素材"
                    description="可同时选择产品信息、企业文化、Logo、图片和品牌视觉等资料与素材，所选内容仅作用于本次生成。"
                    button-text="调整本次知识库引用"
                    :selection-validator="validateImageKnowledgeReferenceSelection"
                    @resources-refreshed="resources = $event"
                    @update:model-value="updateImageKnowledgeReferences"
                  />
                </ElFormItem>

                <template v-if="activeStageKey === 'storyboard'">
                  <ElFormItem label="引用文案版本" class="generation-instruction">
                    <!-- eslint-disable unocss/order-attributify -- Element Plus props use the component's semantic order -->
                    <ElSelect
                      v-model="generationForm.source_artifact_id"
                      clearable
                      placeholder="可不选，直接按任务主题生成"
                    >
                      <ElOption
                        v-for="item in copyArtifacts"
                        :key="item.id"
                        :label="`${item.linked ? '已回引 · ' : ''}文案版本 ${item.version}`"
                        :value="item.id"
                      />
                    </ElSelect>
                  </ElFormItem>
                </template>

                <template v-if="activeStageKey === 'image'">
                  <ImageGenerationControls
                    :task-id="task.id"
                    :model-value="generationForm"
                    :resources="imageResources"
                    :ratio-options="parameterOptions(activeCapability, 'ratios')"
                    :size-options="parameterOptions(activeCapability, 'sizes')"
                    :allow-custom-ratio="supportsCustomParameter(activeCapability, 'ratio')"
                    :allow-custom-size="supportsCustomParameter(activeCapability, 'size')"
                    :reference-limit="imageReferenceLimit"
                    :reference-error="imageReferenceError"
                    @update:model-value="updateImageGenerationForm"
                    @resources-uploaded="handleImageReferenceResourcesUploaded"
                  />
                </template>

                <template v-if="activeStageKey === 'voice_subtitles'">
                  <ElFormItem label="需要朗读的文字（可选）" class="generation-instruction">
                    <ElInput
                      v-model="generationForm.voice_text"
                      type="textarea"
                      :rows="5"
                      placeholder="留空时使用当前已选分镜的旁白；没有分镜旁白时使用已选文案"
                    />
                  </ElFormItem>
                  <ElFormItem label="音色">
                    <ElSelect
                      v-if="parameterOptions(speechCapability, 'voices').length"
                      v-model="generationForm.voice"
                      clearable
                      placeholder="使用服务默认音色"
                    >
                      <ElOption
                        v-for="option in parameterOptions(speechCapability, 'voices')"
                        :key="option.value"
                        :label="option.label"
                        :value="option.value"
                      />
                    </ElSelect>
                    <ElInput
                      v-else
                      v-model="generationForm.voice"
                      placeholder="由服务能力决定，可留空"
                    />
                  </ElFormItem>
                  <ElFormItem label="语速">
                    <ElInputNumber
                      v-model="generationForm.speed"
                      :min="parameterRange(speechCapability, 'speed')?.min"
                      :max="parameterRange(speechCapability, 'speed')?.max"
                      placeholder="使用服务默认值"
                    />
                  </ElFormItem>
                  <ElFormItem label="音量">
                    <ElInputNumber
                      v-model="generationForm.volume"
                      :min="parameterRange(speechCapability, 'volume')?.min"
                      :max="parameterRange(speechCapability, 'volume')?.max"
                      placeholder="使用服务默认值"
                    />
                  </ElFormItem>
                  <ElCollapse class="generation-instruction voice-advanced-options">
                    <ElCollapseItem title="音频与字幕分段高级设置（可选）" name="voice-advanced">
                      <div class="video-option-grid">
                        <ElFormItem label="音频格式">
                          <ElSelect
                            v-model="generationForm.audio_format"
                            clearable
                            filterable
                            allow-create
                            placeholder="使用服务默认格式"
                          >
                            <ElOption
                              v-for="option in parameterOptions(speechCapability, 'formats')"
                              :key="option.value"
                              :label="option.label.toUpperCase()"
                              :value="option.value"
                            />
                          </ElSelect>
                        </ElFormItem>
                        <ElFormItem label="采样率">
                          <ElSelect
                            v-model="generationForm.sample_rate"
                            clearable
                            filterable
                            allow-create
                            placeholder="使用服务默认值"
                          >
                            <ElOption label="16 kHz" value="16000" />
                            <ElOption label="24 kHz" value="24000" />
                            <ElOption label="48 kHz" value="48000" />
                          </ElSelect>
                        </ElFormItem>
                        <ElFormItem label="字幕每段字数">
                          <ElInputNumber
                            v-model="generationForm.subtitle_max_chars_per_line"
                            :min="1"
                            :max="100"
                            placeholder="使用服务默认值"
                          />
                        </ElFormItem>
                        <ElFormItem label="字幕每段行数">
                          <ElInputNumber
                            v-model="generationForm.subtitle_max_lines"
                            :min="1"
                            :max="4"
                            placeholder="使用服务默认值"
                          />
                        </ElFormItem>
                      </div>
                    </ElCollapseItem>
                  </ElCollapse>
                </template>

                <template v-if="activeStageKey === 'video_preview'">
                  <ElFormItem label="从本机导入素材" class="generation-instruction">
                    <ElCollapse class="inline-media-upload">
                      <ElCollapseItem title="上传图片、视频或已有配音" name="upload-media">
                        <ContentMediaUploader @uploaded="handleTaskMediaUploaded" />
                      </ElCollapseItem>
                    </ElCollapse>
                  </ElFormItem>
                  <ElFormItem label="成片前检查" class="generation-instruction">
                    <ElDescriptions
                      :column="4"
                      border
                      size="small"
                      class="composition-readiness"
                    >
                      <ElDescriptionsItem label="分镜">
                        {{ compositionReadiness.shotCount }} 个
                      </ElDescriptionsItem>
                      <ElDescriptionsItem label="预计时长">
                        {{ compositionReadiness.duration.toFixed(1) }} 秒
                      </ElDescriptionsItem>
                      <ElDescriptionsItem label="缺少逐镜素材">
                        <ElTag
                          :type="compositionReadiness.missingShotAssets ? 'warning' : 'success'"
                          effect="plain"
                        >
                          {{ compositionReadiness.missingShotAssets }} 个
                        </ElTag>
                      </ElDescriptionsItem>
                      <ElDescriptionsItem label="声音与字幕">
                        {{ compositionReadiness.hasVoice ? '有主配音' : '无主配音' }} ·
                        {{ compositionReadiness.hasSubtitle ? '有字幕' : '无字幕' }}
                      </ElDescriptionsItem>
                    </ElDescriptions>
                  </ElFormItem>
                  <ElFormItem
                    v-if="storyboardShots.length"
                    label="逐镜 AI 视频（可选）"
                    class="generation-instruction"
                  >
                    <div class="shot-generation-list">
                      <article v-for="(shot, index) in storyboardShots" :key="shot.id || index">
                        <div>
                          <strong>镜头 {{ index + 1 }}</strong>
                          <span>{{ shot.visual || shot.narration || '未填写画面说明' }}</span>
                          <small>{{ shot.duration_seconds }} 秒 · 已有
                            {{ aiVideoCandidatesByShot.get(index) || 0 }} 个候选</small>
                        </div>
                        <ElButton
                          :disabled="busy === 'checking-service'"
                          :loading="busy === 'generate-clips'"
                          @click="generateStage('clips', { shot_index: index })"
                        >
                          {{ aiVideoCandidatesByShot.get(index) ? '重新生成本镜头' : '生成本镜头' }}
                        </ElButton>
                      </article>
                    </div>
                  </ElFormItem>
                  <ElFormItem label="企业图片 / 视频镜头（可选）" class="generation-instruction">
                    <ContentResourcePicker
                      v-model="generationForm.source_asset_ids"
                      :resources="resources"
                      :kinds="imageVideoResourceKinds"
                      title="选择企业图片或视频镜头"
                      description="按服务分类浏览并预览；选择后可直接参与成片合成。"
                      button-text="浏览企业素材库"
                    />
                  </ElFormItem>
                  <ElFormItem
                    v-if="compositionArtifactOptions.length"
                    label="本任务生成的图片 / 视频（可选）"
                    class="generation-instruction"
                  >
                    <ElSelect
                      v-model="selectedCompositionArtifactIds"
                      clearable
                      filterable
                      collapse-tags
                      collapse-tags-tooltip
                      multiple
                      placeholder="选择本任务或回引任务已经生成的内容"
                    >
                      <ElOption
                        v-for="item in compositionArtifactOptions"
                        :key="item.id"
                        :label="item.label"
                        :value="item.id"
                      />
                    </ElSelect>
                    <!-- eslint-enable unocss/order-attributify -->
                  </ElFormItem>
                  <section class="generation-instruction video-options">
                    <ElCollapse>
                      <ElCollapseItem title="AI 镜头参数（可选）" name="ai-video-options">
                        <div class="video-option-grid">
                          <ElFormItem
                            v-if="parameterOptions(aiVideoCapability, 'durations').length"
                            label="AI 镜头时长"
                          >
                            <ElSelect
                              v-model="generationForm.ai_video_duration"
                              clearable
                              placeholder="使用模型默认值"
                            >
                              <ElOption
                                v-for="option in parameterOptions(aiVideoCapability, 'durations')"
                                :key="option.value"
                                :label="`${option.label} 秒`"
                                :value="option.value"
                              />
                            </ElSelect>
                          </ElFormItem>
                          <ElFormItem
                            v-if="parameterOptions(aiVideoCapability, 'ratios').length"
                            label="AI 镜头比例"
                          >
                            <ElSelect
                              v-model="generationForm.ai_video_ratio"
                              clearable
                              placeholder="使用模型默认值"
                            >
                              <ElOption
                                v-for="option in parameterOptions(aiVideoCapability, 'ratios')"
                                :key="option.value"
                                :label="option.label"
                                :value="option.value"
                              />
                            </ElSelect>
                          </ElFormItem>
                          <ElFormItem
                            v-if="parameterOptions(aiVideoCapability, 'resolutions').length"
                            label="AI 镜头清晰度"
                          >
                            <ElSelect
                              v-model="generationForm.ai_video_resolution"
                              clearable
                              placeholder="使用模型默认值"
                            >
                              <ElOption
                                v-for="option in parameterOptions(aiVideoCapability, 'resolutions')"
                                :key="option.value"
                                :label="option.label"
                                :value="option.value"
                              />
                            </ElSelect>
                          </ElFormItem>
                          <ElFormItem
                            v-if="
                              supportsParameter(aiVideoCapability, 'generate_audio')
                                || supportsParameter(aiVideoCapability, 'return_last_frame')
                            "
                            label="AI 镜头输出"
                          >
                            <ElCheckbox
                              v-if="supportsParameter(aiVideoCapability, 'generate_audio')"
                              v-model="generationForm.generate_audio"
                            >
                              同步生成声音
                            </ElCheckbox>
                            <ElCheckbox
                              v-if="supportsParameter(aiVideoCapability, 'return_last_frame')"
                              v-model="generationForm.return_last_frame"
                            >
                              返回尾帧
                            </ElCheckbox>
                          </ElFormItem>
                        </div>
                      </ElCollapseItem>
                      <ElCollapseItem title="成片尺寸与转场（可选）" name="composition-options">
                        <div class="video-option-grid">
                          <ElFormItem label="常用成片尺寸">
                            <ElSelect
                              :model-value="generationForm.video_size_preset"
                              clearable
                              filterable
                              allow-create
                              placeholder="选择常用尺寸或输入宽x高"
                              @update:model-value="applyVideoSizePreset(String($event || ''))"
                            >
                              <ElOption label="横版高清 1920 × 1080" value="1920x1080" />
                              <ElOption label="竖版高清 1080 × 1920" value="1080x1920" />
                              <ElOption label="方形 1080 × 1080" value="1080x1080" />
                              <ElOption label="横版 1280 × 720" value="1280x720" />
                            </ElSelect>
                          </ElFormItem>
                          <ElFormItem label="成片宽度">
                            <ElInput
                              v-model="generationForm.video_width"
                              inputmode="numeric"
                              placeholder="由合成能力决定"
                            />
                          </ElFormItem>
                          <ElFormItem label="成片高度">
                            <ElInput
                              v-model="generationForm.video_height"
                              inputmode="numeric"
                              placeholder="由合成能力决定"
                            />
                          </ElFormItem>
                          <ElFormItem label="帧率">
                            <ElInput
                              v-model="generationForm.video_fps"
                              inputmode="numeric"
                              placeholder="由合成能力决定"
                            />
                          </ElFormItem>
                          <ElFormItem label="默认画面适配">
                            <ElSelect
                              v-model="generationForm.fit_mode"
                              clearable
                              placeholder="逐镜设置优先"
                            >
                              <ElOption label="完整显示（可能留边）" value="contain" />
                              <ElOption label="铺满画面（可能裁切）" value="cover" />
                            </ElSelect>
                          </ElFormItem>
                          <ElFormItem label="默认转场">
                            <ElSelect
                              v-if="parameterOptions(compositionCapability, 'transitions').length"
                              v-model="generationForm.transition"
                              clearable
                              placeholder="逐镜设置优先"
                            >
                              <ElOption
                                v-for="option in parameterOptions(
                                  compositionCapability,
                                  'transitions',
                                )"
                                :key="option.value"
                                :label="option.label"
                                :value="option.value"
                              />
                            </ElSelect>
                            <ElInput
                              v-else
                              v-model="generationForm.transition"
                              placeholder="逐镜设置优先"
                            />
                          </ElFormItem>
                        </div>
                      </ElCollapseItem>
                      <ElCollapseItem
                        title="声音、品牌与字幕样式（可选）"
                        name="brand-audio-options"
                      >
                        <div class="video-option-grid">
                          <ElFormItem
                            label="已有配音 / 主音轨（可选）"
                            class="generation-instruction"
                          >
                            <ContentResourcePicker
                              :model-value="
                                generationForm.voice_asset_id ? [generationForm.voice_asset_id] : []
                              "
                              :resources="resources"
                              :kinds="audioResourceKinds"
                              title="选择已有配音"
                              description="选择用户录音或已登记音频作为主配音；不选择时使用当前任务确认的 AI 配音。"
                              button-text="浏览已有配音"
                              :multiple="false"
                              :preserve-unknown="false"
                              @update:model-value="updateSingleResource('voice_asset_id', $event)"
                            />
                          </ElFormItem>
                          <ElFormItem label="源视频声音处理">
                            <ElSelect
                              v-model="generationForm.source_audio_policy"
                              placeholder="选择音轨处理方式"
                            >
                              <ElOption label="移除原视频声音" value="mute" />
                              <ElOption label="由主配音替换原声音" value="replace_by_voice" />
                            </ElSelect>
                          </ElFormItem>
                          <ElFormItem
                            v-if="generationForm.voice_asset_id"
                            label="上传配音对应文字（可选）"
                            class="generation-instruction"
                          >
                            <ElInput
                              v-model="generationForm.uploaded_voice_text"
                              type="textarea"
                              :rows="3"
                              maxlength="20000"
                              show-word-limit
                              placeholder="可粘贴录音对应文稿，便于记录和后续生成字幕；未填写时不会伪造字幕时间轴"
                            />
                          </ElFormItem>
                          <ElFormItem label="背景音乐（可选）" class="generation-instruction">
                            <ContentResourcePicker
                              :model-value="
                                generationForm.bgm_asset_id ? [generationForm.bgm_asset_id] : []
                              "
                              :resources="resources"
                              :kinds="audioResourceKinds"
                              title="选择背景音乐"
                              description="按企业素材分类搜索并试听音频。"
                              button-text="浏览音频素材"
                              :multiple="false"
                              :preserve-unknown="false"
                              @update:model-value="updateSingleResource('bgm_asset_id', $event)"
                            />
                          </ElFormItem>
                          <ElFormItem label="背景音乐音量">
                            <ElInputNumber
                              v-model="generationForm.background_music_volume"
                              :min="0"
                              :max="1"
                              :step="0.05"
                              :precision="2"
                              placeholder="使用服务默认值"
                            />
                          </ElFormItem>
                          <ElFormItem label="Logo 素材（可选）" class="generation-instruction">
                            <ContentResourcePicker
                              :model-value="
                                generationForm.logo_asset_id ? [generationForm.logo_asset_id] : []
                              "
                              :resources="resources"
                              :kinds="logoResourceKinds"
                              title="选择 Logo 或品牌图片"
                              description="按分类浏览并预览，选择一项用于成片品牌露出。"
                              button-text="浏览品牌素材"
                              :multiple="false"
                              :preserve-unknown="false"
                              @update:model-value="updateSingleResource('logo_asset_id', $event)"
                            />
                          </ElFormItem>
                          <ElFormItem label="Logo 位置">
                            <ElSelect
                              v-model="generationForm.logo_position"
                              clearable
                              placeholder="使用服务默认位置"
                            >
                              <ElOption label="左上" value="top_left" />
                              <ElOption label="右上" value="top_right" />
                              <ElOption label="左下" value="bottom_left" />
                              <ElOption label="右下" value="bottom_right" />
                            </ElSelect>
                          </ElFormItem>
                          <ElFormItem label="Logo 宽度（像素）">
                            <ElInputNumber
                              v-model="generationForm.logo_width"
                              :min="16"
                              :max="1200"
                              placeholder="使用服务默认值"
                            />
                          </ElFormItem>
                          <ElFormItem label="片头素材（可选）" class="generation-instruction">
                            <ContentResourcePicker
                              :model-value="
                                generationForm.intro_asset_id ? [generationForm.intro_asset_id] : []
                              "
                              :resources="resources"
                              :kinds="videoResourceKinds"
                              title="选择片头视频"
                              description="按分类浏览并预览企业视频素材。"
                              button-text="浏览片头素材"
                              :multiple="false"
                              :preserve-unknown="false"
                              @update:model-value="updateSingleResource('intro_asset_id', $event)"
                            />
                          </ElFormItem>
                          <ElFormItem label="片头时长（秒）">
                            <ElInputNumber
                              v-model="generationForm.intro_duration_seconds"
                              :min="0.1"
                              :max="60"
                              :step="0.1"
                              placeholder="使用素材或服务默认值"
                            />
                          </ElFormItem>
                          <ElFormItem label="片尾素材（可选）" class="generation-instruction">
                            <ContentResourcePicker
                              :model-value="
                                generationForm.outro_asset_id ? [generationForm.outro_asset_id] : []
                              "
                              :resources="resources"
                              :kinds="videoResourceKinds"
                              title="选择片尾视频"
                              description="按分类浏览并预览企业视频素材。"
                              button-text="浏览片尾素材"
                              :multiple="false"
                              :preserve-unknown="false"
                              @update:model-value="updateSingleResource('outro_asset_id', $event)"
                            />
                          </ElFormItem>
                          <ElFormItem label="片尾时长（秒）">
                            <ElInputNumber
                              v-model="generationForm.outro_duration_seconds"
                              :min="0.1"
                              :max="60"
                              :step="0.1"
                              placeholder="使用素材或服务默认值"
                            />
                          </ElFormItem>
                          <ElFormItem label="字幕字号">
                            <ElInputNumber
                              v-model="generationForm.subtitle_font_size"
                              :min="12"
                              :max="120"
                              placeholder="使用服务默认值"
                            />
                          </ElFormItem>
                          <ElFormItem label="字幕颜色">
                            <ElInput
                              v-model="generationForm.subtitle_primary_color"
                              placeholder="可选，例如 #FFFFFF"
                            />
                          </ElFormItem>
                          <ElFormItem label="字幕描边颜色">
                            <ElInput
                              v-model="generationForm.subtitle_outline_color"
                              placeholder="可选，例如 #000000"
                            />
                          </ElFormItem>
                          <ElFormItem label="字幕底部距离（像素）">
                            <ElInputNumber
                              v-model="generationForm.subtitle_margin_vertical"
                              :min="0"
                              :max="1000"
                              placeholder="使用服务默认值"
                            />
                          </ElFormItem>
                        </div>
                      </ElCollapseItem>
                    </ElCollapse>
                  </section>
                </template>
              </div>

              <div class="generation-actions">
                <template v-if="activeStageKey === 'compliance'">
                  <ElButton
                    type="primary"
                    :loading="busy === 'compliance'"
                    @click="runCompliance()"
                  >
                    检测所选文案
                  </ElButton>
                </template>
                <template v-else-if="activeStageKey === 'voice_subtitles'">
                  <ElButton
                    type="primary"
                    :disabled="busy === 'checking-service'"
                    :loading="busy === 'generate-voice'"
                    @click="generateStage('voice')"
                  >
                    生成配音
                  </ElButton>
                  <ElButton
                    :disabled="busy === 'checking-service'"
                    :loading="busy === 'generate-subtitle'"
                    @click="generateStage('subtitle')"
                  >
                    生成字幕
                  </ElButton>
                </template>
                <template v-else-if="activeStageKey === 'video_preview'">
                  <ElButton
                    type="primary"
                    :disabled="busy === 'checking-service'"
                    :loading="busy === 'generate-clips'"
                    @click="generateStage('clips')"
                  >
                    生成全部 AI 镜头
                  </ElButton>
                  <ElButton
                    :disabled="busy === 'checking-service'"
                    :loading="busy === 'generate-compose'"
                    @click="generateStage('compose')"
                  >
                    合成成片
                  </ElButton>
                </template>
                <ElButton
                  v-else
                  type="primary"
                  :disabled="busy === 'checking-service'"
                  :loading="busy.startsWith('generate-')"
                  @click="generateStage()"
                >
                  {{ currentArtifacts.length ? '重新生成' : '开始生成' }}
                </ElButton>
                <ElButton
                  v-if="activeStageKey !== 'copy'"
                  :disabled="!currentStage"
                  @click="saveStageRequirements"
                >
                  保存补充要求
                </ElButton>
                <ElButton v-if="currentStage?.skippable" text @click="skipCurrentStage">
                  跳过本阶段
                </ElButton>
              </div>
            </section>

            <ElAlert
              v-if="activeJob"
              class="job-status"
              :type="activeJob.status === 'failed' ? 'error' : 'info'"
              :title="`后台任务：${displayLabel(activeJob.status, jobStatusLabels, '其他状态')}`"
              :description="
                activeJob.error || activeJob.status === 'failed'
                  ? generationJobError(activeJob)
                  : `处理进度：${activeJob.progress ?? 0}%`
              "
              :closable="false"
              show-icon
            >
              <template #default>
                <ElButton
                  v-if="['queued', 'running'].includes(activeJob.status)"
                  link
                  @click="cancelJob"
                >
                  取消
                </ElButton>
                <ElButton
                  v-if="
                    activeJob.status === 'failed'
                      && !isReferenceImagePrivacyError(activeJob.error)
                  "
                  link
                  type="primary"
                  @click="retryJob"
                >
                  重试
                </ElButton>
              </template>
            </ElAlert>

            <section v-if="editableStoryboard" class="stage-panel storyboard-panel">
              <div class="panel-heading">
                <div>
                  <h3>逐镜编辑</h3>
                </div>
                <ElButton
                  type="primary"
                  plain
                  :loading="busy === 'storyboard-edit'"
                  @click="saveStoryboard"
                >
                  保存为新版本
                </ElButton>
              </div>
              <StoryboardEditor
                v-model="storyboardDraft"
                :enterprise-assets="storyboardEnterpriseAssets"
                :task-assets="storyboardTaskAssetOptions"
                :transitions="parameterOptions(compositionCapability, 'transitions').map(String)"
              />
            </section>

            <ElAlert
              v-if="activeStageKey === 'image' && imageRevisionNotice"
              type="success"
              :title="imageRevisionNotice"
              :closable="true"
              show-icon
              @close="imageRevisionNotice = ''"
            />

            <section class="result-section">
              <div class="panel-heading">
                <div><h3>生成结果</h3></div>
                <span>{{ selectionSaving ? '正在保存选择…' : `${currentArtifacts.length} 个版本` }}</span>
              </div>
              <ElEmpty v-if="!currentArtifacts.length" description="当前阶段还没有生成结果" />
              <div v-else class="artifact-grid">
                <ContentArtifactCard
                  v-for="artifact in currentArtifacts"
                  :key="artifact.id"
                  :artifact="artifact"
                  :selected="selectedArtifactIds.includes(artifact.id)"
                  :media-url="mediaUrls[artifact.id]"
                  :compliance="complianceResults[artifact.id]"
                  :fixed-title="artifact.kind === 'copy' ? confirmedTopicTitle : ''"
                  :currently-confirmed="currentStageConfirmedArtifactIds.has(artifact.id)"
                  :selection-disabled="
                    Boolean(busy)
                      || ['legacy_mock', 'stale', 'discarded'].includes(artifact.status)
                  "
                  @select="toggleArtifact"
                  @edit="openArtifactEditor"
                  @remove="removeArtifact"
                  @preview="loadArtifactPreview"
                  @download="downloadArtifact"
                  @compliance="runCompliance"
                  @choose-candidate="chooseCandidate"
                />
              </div>
            </section>

            <footer class="stage-footer">
              <ElButton
                v-if="activeStageKey === 'copy'"
                type="primary"
                :loading="busy === 'continue-copy' || selectionSaving"
                @click="continueFromCurrentCopy"
              >
                使用当前文案并继续
              </ElButton>
              <ElButton
                v-else
                type="primary"
                :loading="busy === 'confirm' || selectionSaving"
                @click="confirmCurrentStage"
              >
                确认本阶段并继续
              </ElButton>
            </footer>
          </template>
        </main>
      </div>
    </template>

    <ElDialog
      v-model="editDialogOpen"
      :title="
        editingArtifact?.kind === 'image'
          ? '查看与继续生成图片'
          : editingArtifact && isArtifactContentEditable(editingArtifact.kind)
            ? '查看与修改内容版本'
            : '查看内容详情'
      "
      width="min(960px, 92vw)"
      destroy-on-close
      @closed="imageRevisionPreviewOpen = false"
    >
      <div
        v-if="editingArtifact && ['topic', 'copy'].includes(editingArtifact.kind)"
        class="transform-toolbar"
      >
        <ElSelect v-model="transformOperation">
          <ElOption label="改写" value="rewrite" />
          <ElOption v-if="editingArtifact.kind === 'copy'" label="扩写" value="expand" />
          <ElOption v-if="editingArtifact.kind === 'copy'" label="缩写" value="shorten" />
          <ElOption v-if="editingArtifact.kind === 'copy'" label="摘要" value="summarize" />
          <ElOption v-if="editingArtifact.kind === 'copy'" label="风格转换" value="style" />
        </ElSelect>
        <ElInput
          v-model="transformInstruction"
          :placeholder="
            editingArtifact.kind === 'topic' ? '告诉 AI 标题要怎么改' : '告诉 AI 文案要怎么改'
          "
        />
        <ElButton
          type="primary"
          plain
          :loading="busy === 'transform'"
          :disabled="Boolean(busy)"
          @click="transformCopy"
        >
          AI 生成新版本
        </ElButton>
      </div>
      <section v-if="editingArtifact?.kind === 'image'" class="image-revision-panel">
        <div class="image-revision-preview">
          <ElImage
            v-if="mediaUrls[editingArtifact.id]"
            :src="mediaUrls[editingArtifact.id]"
            :preview-src-list="[]"
            fit="contain"
            alt="当前图片版本"
            role="button"
            tabindex="0"
            title="双击查看大图"
            aria-label="当前图片版本，双击或按回车查看大图"
            @click="imageRevisionPreviewOpen = true"
            @dblclick.stop="imageRevisionPreviewOpen = true"
            @keydown.enter.stop.prevent="imageRevisionPreviewOpen = true"
            @keydown.space.stop.prevent="imageRevisionPreviewOpen = true"
          />
          <ElSkeleton v-else animated :rows="5" />
          <ElImageViewer
            v-if="imageRevisionPreviewOpen && mediaUrls[editingArtifact.id]"
            :url-list="[mediaUrls[editingArtifact.id]]"
            teleported
            @close="imageRevisionPreviewOpen = false"
          />
        </div>
        <div class="image-revision-form">
          <ElFormItem label="告诉 AI 图片要怎么改">
            <ElInput
              v-model="imageRevisionInstruction"
              type="textarea"
              :rows="5"
              maxlength="2000"
              show-word-limit
              placeholder="示例：保持产品和 Logo 不变，把背景改成夜间宴会场景"
              @keydown.ctrl.enter.prevent="generateImageRevision"
            />
          </ElFormItem>
          <ElButton
            type="primary"
            plain
            :loading="busy === 'image-revision'"
            :disabled="!imageRevisionInstruction.trim()"
            @click="generateImageRevision"
          >
            AI 生成新图片
          </ElButton>
        </div>
      </section>
      <ContentArtifactEditor
        v-if="editingArtifact"
        v-model="artifactDraft"
        :artifact="editingArtifact"
        :fixed-title="editingArtifact.kind === 'copy' ? confirmedTopicTitle : ''"
        :transitions="
          parameterOptions(compositionCapability, 'transitions').map((option) => option.value)
        "
      />
      <template #footer>
        <ElButton @click="editDialogOpen = false">关闭</ElButton>
        <ElButton
          v-if="editingArtifact && isArtifactContentEditable(editingArtifact.kind)"
          type="primary"
          :loading="busy === 'artifact-save'"
          :disabled="!hasArtifactDraftChanges || busy === 'transform'"
          @click="saveArtifactEdit"
        >
          保存为新版本
        </ElButton>
      </template>
    </ElDialog>

    <ElDialog v-model="linkDialogOpen" title="加入内容任务" width="min(720px, 92vw)">
      <ElAlert
        title="结果将以引用关系加入目标任务，不复制或覆盖原始版本；目标任务相关下游阶段会按规则标记为需更新。"
        type="info"
        :closable="false"
        show-icon
      />
      <ElForm label-position="top" class="link-form">
        <ElFormItem label="目标完整任务" required>
          <ElSelect
            v-model="linkTargetTaskId"
            filterable
            placeholder="请选择完整创作或快捷视频任务"
          >
            <ElOption
              v-for="item in linkTargetTasks"
              :key="item.id"
              :label="`${item.name} · ${modeLabels[item.mode]}`"
              :value="item.id"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="引用已确认结果" required>
          <ElCheckboxGroup v-model="linkArtifactIds" class="link-artifact-list">
            <ElCheckbox v-for="item in linkableArtifacts" :key="item.id" :value="item.id">
              {{ displayLabel(item.kind, artifactKindLabels, '其他内容') }} · 版本
              {{ item.version }}
            </ElCheckbox>
          </ElCheckboxGroup>
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="linkDialogOpen = false">取消</ElButton>
        <ElButton type="primary" :loading="busy === 'link-artifacts'" @click="linkToFullTask">
          确认加入
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.content-task-view {
  display: grid;
  gap: var(--xi-component-content-gap-md);
}

.task-heading,
.task-heading-actions,
.stage-heading,
.panel-heading,
.generation-status > div,
.generation-actions,
.panel-actions,
.stage-footer,
.transform-toolbar {
  display: flex;
  align-items: center;
}

.task-heading,
.stage-heading,
.panel-heading {
  justify-content: space-between;
  gap: var(--xi-component-content-gap-md);
}

.task-heading h2,
.task-heading p,
.stage-heading h3,
.stage-heading p,
.panel-heading h3,
.panel-heading p {
  margin: 0;
}

.task-heading > div:first-child > span,
.stage-heading > div > span {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.task-heading h2 {
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading1-font-size);
  font-weight: 600;
  line-height: 28px;
}

.task-heading p,
.stage-heading p,
.panel-heading p {
  margin-top: var(--xi-component-content-gap-xxs);
  color: var(--xi-semantic-color-text-secondary);
}

.task-heading-actions,
.generation-status > div,
.generation-actions,
.panel-actions,
.stage-footer,
.transform-toolbar {
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-sm);
}

.creation-panel,
.stage-panel,
.result-section {
  padding: var(--xi-component-content-gap-lg);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.creation-panel :deep(.task-form-grid),
.stage-panel :deep(.task-form-grid) {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 var(--xi-component-content-gap-md);
}

.creation-panel :deep(.full-field),
.stage-panel :deep(.full-field) {
  grid-column: span 3;
}

.panel-actions {
  justify-content: flex-end;
}

.export-summary :deep(.el-result__extra) {
  width: min(760px, 100%);
}

.export-controls,
.export-primary-actions,
.export-secondary-actions {
  display: flex;
  align-items: flex-end;
  justify-content: center;
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-sm);
}

.export-controls {
  padding: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand);
  border-radius: var(--xi-component-content-radius-card);
}

.full-export-controls {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(180px, 0.8fr) auto;
  align-items: end;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-md);
  text-align: left;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand);
  border-radius: var(--xi-component-content-radius-card);
}

.full-export-field {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-xxs);
  color: var(--xi-semantic-color-text-primary);
}

.full-export-field :deep(.el-select) {
  width: 100%;
}

.full-export-field :deep(.el-select__wrapper) {
  min-height: var(--xi-component-content-control-height-large);
}

.full-export-primary-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--xi-component-content-gap-xs);
}

.full-export-primary-actions .el-button {
  min-height: var(--xi-component-content-control-height-large);
}

.export-format-field {
  display: grid;
  min-width: min(320px, 100%);
  gap: var(--xi-component-content-gap-xxs);
  color: var(--xi-semantic-color-text-primary);
  text-align: left;
}

.export-format-field .el-select {
  width: 100%;
}

.export-secondary-actions {
  flex-wrap: wrap;
  margin-top: var(--xi-component-content-gap-md);
}

.bundle-note {
  margin: var(--xi-component-content-gap-sm) 0 0;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.workflow-layout {
  display: grid;
  grid-template-columns: var(--xi-component-content-sidebar-width) minmax(0, 1fr);
  align-items: start;
  gap: var(--xi-component-content-gap-md);
}

.stage-navigation {
  display: grid;
  overflow: hidden;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.stage-navigation button {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--xi-component-content-gap-xs);
  padding: var(--xi-component-content-gap-sm);
  color: var(--xi-semantic-color-text-secondary);
  font: inherit;
  text-align: left;
  background: var(--xi-semantic-color-background-container);
  border: 0;
  border-bottom: var(--xi-component-content-border);
  cursor: pointer;
}

.stage-navigation button:last-child {
  border-bottom: 0;
}

.stage-navigation button:disabled {
  color: var(--xi-semantic-color-text-tertiary);
  cursor: not-allowed;
}

.stage-navigation button:disabled:hover {
  color: var(--xi-semantic-color-text-tertiary);
  background: var(--xi-semantic-color-background-container);
}

.stage-navigation button:hover,
.stage-navigation button.active {
  color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-brand-subtle);
}

.stage-navigation button.active {
  border-left: var(--xi-component-content-focus-width) solid var(--xi-semantic-color-border-brand-default);
}

.stage-number {
  display: inline-grid;
  width: var(--xi-component-content-control-height);
  height: var(--xi-component-content-control-height);
  color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-brand-light);
  place-items: center;
}

.stage-navigation button.active .stage-number {
  color: var(--xi-semantic-color-text-inverse);
  background: var(--xi-semantic-color-background-brand-section);
}

.stage-name {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: var(--xi-component-content-gap-xxs);
}

.stage-name small {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.stage-content {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-md);
}

.stage-heading {
  padding: var(--xi-component-content-gap-lg);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.stage-heading h3,
.panel-heading h3 {
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading2-font-size);
}

.generation-panel {
  display: grid;
  gap: var(--xi-component-content-gap-md);
}

.generation-status {
  display: grid;
  gap: var(--xi-component-content-gap-xs);
}

.generation-status > div {
  justify-content: space-between;
}

.generation-status > div > span {
  color: var(--xi-semantic-color-text-primary);
}

.generation-fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(var(--xi-component-content-card-min-width), 1fr));
  gap: 0 var(--xi-component-content-gap-md);
}

.generation-instruction {
  grid-column: 1 / -1;
}

.generation-instruction :deep(.el-form-item__content),
.inline-media-upload,
.composition-readiness {
  width: 100%;
}

.inline-media-upload {
  border-top: var(--xi-component-content-border);
}

.video-options {
  display: grid;
  gap: var(--xi-component-content-gap-sm);
}

.video-option-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(var(--xi-component-content-card-min-width), 1fr));
  gap: 0 var(--xi-component-content-gap-md);
  padding: var(--xi-component-content-gap-sm) 0;
}

.video-option-grid .generation-instruction {
  grid-column: 1 / -1;
}

.shot-generation-list {
  display: grid;
  width: 100%;
  gap: var(--xi-component-content-gap-xs);
}

.shot-generation-list article,
.shot-generation-list article > div {
  display: flex;
}

.shot-generation-list article {
  align-items: center;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-md);
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
}

.shot-generation-list article > div {
  min-width: 0;
  flex-direction: column;
  gap: var(--xi-component-content-gap-xxs);
}

.shot-generation-list span,
.shot-generation-list small {
  overflow: hidden;
  color: var(--xi-semantic-color-text-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.existing-script-form {
  margin-top: var(--xi-component-content-gap-md);
}

.existing-import-panel {
  display: grid;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand);
}

.existing-import-panel h4,
.existing-import-panel p {
  margin: 0;
}

.existing-import-panel p {
  margin-top: var(--xi-component-content-gap-xxs);
  color: var(--xi-semantic-color-text-secondary);
}

.existing-import-panel .el-button {
  justify-self: start;
}

.generation-actions {
  justify-content: flex-start;
}

.job-status {
  margin: 0;
}

.storyboard-panel {
  display: grid;
  gap: var(--xi-component-content-gap-md);
}

.result-section {
  display: grid;
  gap: var(--xi-component-content-gap-md);
}

.panel-heading > span {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.artifact-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(var(--xi-component-content-card-min-width), 1fr));
  gap: var(--xi-component-content-gap-md);
}

.stage-footer {
  position: sticky;
  z-index: 1;
  bottom: 0;
  justify-content: flex-end;
  padding: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
}

.transform-toolbar {
  margin: var(--xi-component-content-gap-md) 0;
}

.transform-toolbar > .el-select {
  width: var(--xi-component-content-sidebar-width);
}

.transform-toolbar > .el-input {
  flex: 1;
  min-width: var(--xi-component-content-card-min-width);
}

.image-revision-panel {
  display: grid;
  grid-template-columns: minmax(240px, 0.9fr) minmax(280px, 1.1fr);
  gap: var(--xi-component-content-gap-lg);
  padding: var(--xi-component-content-gap-md);
  margin-bottom: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.image-revision-preview {
  display: grid;
  min-height: 260px;
  overflow: hidden;
  place-items: center;
  background: var(--xi-component-card-background);
  border-radius: var(--xi-component-content-radius-control);
}

.image-revision-preview :deep(.el-image) {
  width: 100%;
  height: 320px;
  cursor: zoom-in;
}

.image-revision-preview :deep(.el-image:focus-within) {
  outline: 2px solid var(--xi-semantic-color-border-brand-default);
  outline-offset: -2px;
}

.image-revision-form {
  display: flex;
  min-width: 0;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
}

.image-revision-form .el-form-item {
  width: 100%;
}

.link-form {
  margin-top: var(--xi-component-content-gap-md);
}

.link-artifact-list {
  display: grid;
  gap: var(--xi-component-content-gap-xs);
}

@media (max-width: 900px) {
  .image-revision-panel {
    grid-template-columns: 1fr;
  }

  .export-controls,
  .export-primary-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .export-format-field,
  .export-primary-actions,
  .export-primary-actions .el-button {
    width: 100%;
  }

  .full-export-controls {
    grid-template-columns: minmax(0, 1fr);
    align-items: stretch;
  }

  .full-export-primary-actions {
    align-items: stretch;
    flex-direction: column;
    flex-wrap: wrap;
  }

  .full-export-primary-actions .el-button {
    width: 100%;
    margin-left: 0;
  }
}
</style>
