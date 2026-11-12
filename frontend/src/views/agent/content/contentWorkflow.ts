import type {
  Artifact,
  ArtifactContent,
  ComplianceHit,
  ContentTask,
  StageKey,
  StoryboardShot,
  TaskMode,
  TaskStageState,
} from '@/api/agent/content'

export const MIN_AI_VIDEO_DURATION_SECONDS = 5

export const contentFixedPopperOptions = {
  strategy: 'fixed' as const,
  modifiers: [{ name: 'hide', enabled: false }],
}

export interface StageDefinition {
  key: StageKey
  label: string
  shortLabel: string
  description: string
  artifactKinds: string[]
}

export const stageDefinitions: StageDefinition[] = [
  {
    key: 'configuration',
    label: '任务配置',
    shortLabel: '配置',
    description: '明确主题、产品、受众、内容类型与表达风格。',
    artifactKinds: [],
  },
  {
    key: 'topic',
    label: '智能标题',
    shortLabel: '标题',
    description: '结合任务要求和企业资料生成多个标题，支持人工修改或告诉 AI 如何调整。',
    artifactKinds: ['topic'],
  },
  {
    key: 'copy',
    label: '智能文案',
    shortLabel: '文案',
    description: '生成多版本文案，并支持改写、扩写、缩写、摘要和风格转换。',
    artifactKinds: ['copy'],
  },
  {
    key: 'compliance',
    label: '合规检测',
    shortLabel: '合规',
    description: '标记关键词、敏感词和禁用词命中，由人工判断、修改或重新生成。',
    artifactKinds: ['compliance'],
  },
  {
    key: 'image',
    label: '智能图片',
    shortLabel: '图片',
    description: '根据主题、文案和企业素材生成多张候选配图。',
    artifactKinds: ['image'],
  },
  {
    key: 'storyboard',
    label: '视频脚本',
    shortLabel: '分镜',
    description: '逐镜编辑画面、旁白、字幕、建议时长、转场和引用素材。',
    artifactKinds: ['storyboard'],
  },
  {
    key: 'voice_subtitles',
    label: '配音字幕',
    shortLabel: '音字',
    description: '生成配音和时间化字幕，支持试听、修改和重新生成。',
    artifactKinds: ['voice', 'subtitle'],
  },
  {
    key: 'video_preview',
    label: '视频生成',
    shortLabel: '视频',
    description: '调用已确认的标题、文案和图片，在轻量视频工作台中生成并完成成片。',
    artifactKinds: ['ai_video_clip', 'video'],
  },
  {
    key: 'export',
    label: '确认导出',
    shortLabel: '导出',
    description: '核对确认版本，导出文案、图片、音频、字幕、视频和素材包。',
    artifactKinds: [],
  },
]

export const modeLabels: Record<TaskMode, string> = {
  full: '完整内容创作',
  copy: '文案生成',
  image: '图片生成',
  video: '快捷视频制作',
}

export const modeDescriptions: Record<TaskMode, string> = {
  full: '从任务配置到视频生成、确认导出的七步完整内容生产流程',
  copy: '先生成并确认标题，再自动生成、修改和检测文案',
  image: '根据创意、文案或企业素材生成图片并独立导出',
  video: '配置任务后进入视频生成，完成剪辑并确认导出',
}

export const modeStageKeys: Record<TaskMode, StageKey[]> = {
  full: ['configuration', 'topic', 'copy', 'compliance', 'image', 'video_preview', 'export'],
  copy: ['configuration', 'topic', 'copy', 'compliance', 'export'],
  image: ['configuration', 'image', 'export'],
  video: ['configuration', 'video_preview', 'export'],
}

export function workflowStageRoute(mode: TaskMode, requestedStage: StageKey, currentStage: StageKey): StageKey {
  const allowed = modeStageKeys[mode]
  if (allowed.includes(requestedStage)) {
    return requestedStage
  }
  const legacyVideoStage = ['storyboard', 'voice_subtitles'].includes(requestedStage)
    || (mode === 'video' && requestedStage === 'image')
  if (['full', 'video'].includes(mode) && legacyVideoStage) {
    return 'video_preview'
  }
  return allowed.includes(currentStage) ? currentStage : allowed[0]
}

export const stateLabels: Record<TaskStageState, string> = {
  not_started: '未开始',
  in_progress: '进行中',
  awaiting_confirmation: '待确认',
  confirmed: '已确认',
  stale: '需更新',
  skipped: '已跳过',
  failed: '失败',
}

export const artifactKindLabels: Record<string, string> = {
  topic: '标题',
  copy: '文案',
  compliance: '合规检测结果',
  image: '图片',
  storyboard: '视频分镜',
  voice: '配音',
  subtitle: '字幕',
  ai_video_clip: 'AI 镜头视频',
  video: '视频成片',
}

export const artifactStatusLabels: Record<string, string> = {
  draft: '待确认',
  selected: '已选择',
  confirmed: '已确认',
  stale: '需更新',
  discarded: '已删除',
  legacy_mock: '历史模拟结果',
  failed: '生成失败',
}

export const taskStatusLabels: Record<string, string> = {
  draft: '配置中',
  active: '进行中',
  in_progress: '进行中',
  // Completion is a workflow detail, not a task-list lifecycle state. The
  // history UI intentionally exposes only 配置中 and 已归档.
  confirmed: '配置中',
  archived: '已归档',
  failed: '处理失败',
}

export const jobStatusLabels: Record<string, string> = {
  pending: '等待处理',
  queued: '排队中',
  running: '处理中',
  succeeded: '处理完成',
  completed: '处理完成',
  failed: '处理失败',
  cancelled: '已取消',
  expired: '已过期',
}

export const dataLevelLabels: Record<string, string> = {
  public: '公开',
  internal: '内部',
  restricted: '受限',
  confidential: '机密',
}

export const resourceKindLabels: Record<string, string> = {
  document: '文档',
  product: '产品资料',
  logo: 'Logo',
  image: '图片',
  video: '视频',
  audio: '音频',
  rule: '内容规范',
}

export const resourceCategoryLabels: Record<string, string> = {
  enterprise: '企业介绍',
  brand: '品牌资料',
  product: '产品资料',
  culture: '企业文化',
  story: '品牌故事',
  positioning: '品牌定位',
  selling_point: '产品卖点',
  publicity: '宣传资料',
  specification: '内容规范',
  material: '视觉与媒体素材',
  video_material: '视频素材',
}

export const providerLabels: Record<string, string> = {
  'hiagent-chat-ds-flash': 'HiAgent 文本生成',
  'hiagent-seedream-image': 'HiAgent 图片生成',
  'hiagent-seedance-video': 'HiAgent AI 视频',
  'chat-tts': '智能配音服务',
  'volcengine-ark-seedance-music': '火山方舟背景音乐',
  'local-ffmpeg-timeline': '本地时间线合成',
  'volcengine-ark-text': '火山方舟文本生成',
  'content-service-library-db': '企业资料库',
  'content-library-db': '企业资料库',
  'volcengine-ark-image': '火山方舟图片生成',
  'openai-compatible-image': 'OpenAI 兼容图片生成',
  'volcengine-doubao-speech-tts': '豆包智能配音',
  'volcengine-doubao-speech-tts-2': '豆包智能配音',
  'local-subtitle-alignment': '本地字幕时间对齐',
  'timestamp-aware-subtitle-alignment': '本地字幕时间对齐',
  'volcengine-ark-ai-video': '火山方舟 AI 视频',
  'local-ffmpeg': '本地视频合成',
  'ffmpeg-video-composer': '本地视频合成',
  'content-workflow': '内容创作流程',
  '人工/系统': '人工编辑或系统处理',
}

export const capabilityLabels: Record<string, string> = {
  'text-generation': '文本生成',
  'knowledge-search': '企业资料检索',
  'image-generation': '图片生成',
  'speech-synthesis': '智能配音',
  'music-generation': '背景音乐生成',
  'subtitle-alignment': '字幕时间对齐',
  'ai-video-generation': 'AI 视频生成',
  'video-composition': '视频合成',
  'timeline-video-composition': '时间线视频合成',
  'workflow-orchestration': '内容流程编排',
}

export const serviceModeLabels: Record<string, string> = {
  'api': '在线接口',
  'async-api': '异步在线接口',
  'local': '本地处理',
  'database': '本地资料库',
  'mock': '模拟能力',
}

export const serviceStatusLabels: Record<string, string> = {
  available: '可使用',
  degraded: '部分服务受限',
  unavailable: '暂不可用',
  disabled: '未启用',
}

export const transitionLabels: Record<string, string> = {
  '': '不设置转场',
  'none': '无转场',
  'fade': '淡入淡出',
  'fadeblack': '淡入黑场',
  'fadewhite': '淡入白场',
}

export const voiceLabels: Record<string, string> = {
  'aiden': '艾登（Aiden）',
  'dylan': '迪伦（Dylan）',
  'eric': '埃里克（Eric）',
  'ono_anna': '安娜（Ono Anna）',
  'ryan': '瑞安（Ryan）',
  'serena': '塞琳娜（Serena）',
  'sohee': '素熙（Sohee）',
  'uncle_fu': '傅叔（Uncle Fu）',
  'vivian': '薇薇安（Vivian）',
  'zh_female_vv_uranus_bigtts': 'Vivi 2.0 女声（普通话）',
  'zh_male_dayi_saturn_bigtts': '大壹男声（视频配音）',
  'zh_female_santongyongns_saturn_bigtts': '流畅女声（视频配音）',
  'mock-voice': '模拟音色',
}

export const operationLabels: Record<string, string> = {
  topics: '智能标题',
  copies: '文案生成',
  transform_copy: '文案调整',
  storyboard: '视频分镜',
  voice: '智能配音',
  subtitle: '字幕生成',
  clips: '逐镜视频生成',
  compose: '视频合成',
}

export const complianceFieldLabels: Record<string, string> = {
  title: '标题',
  source_title: '已确认标题',
  summary: '摘要',
  body: '文案正文',
  text: '文本内容',
  topic: '主题',
  subtitle: '字幕内容',
  narration: '旁白内容',
  visual: '画面描述',
  requirements: '补充要求',
}

const complianceContainerLabels: Record<string, string> = {
  candidates: '候选文案',
  selected_candidate: '当前选用文案',
  shots: '分镜',
  segments: '字幕',
}

export function complianceTypeLabel(value?: string | null) {
  if (value === 'forbidden') {
    return '禁用词'
  }
  if (value === 'sensitive') {
    return '敏感词'
  }
  return '关键词'
}

export function displayLabel(
  value: string | null | undefined,
  labels: Record<string, string>,
  fallbackPrefix = '其他',
) {
  const normalized = value?.trim()
  if (!normalized) {
    return '—'
  }
  if (labels[normalized]) {
    return labels[normalized]
  }
  return /[\u3400-\u9FFF]/.test(normalized) ? normalized : fallbackPrefix
}

export function providerLabel(value?: string | null) {
  return displayLabel(value, providerLabels, '其他服务')
}

export function modelLabel(value?: string | null) {
  const normalized = value?.trim()
  if (!normalized) {
    return '未指定模型'
  }
  const exact: Record<string, string> = {
    'doubao-seed-2-0-lite-260428': '豆包文本生成模型',
    'doubao-seedream-5-0-260128': '豆包图片生成模型',
    'seed-tts-2.0': '豆包语音合成 2.0',
    'word-timing-or-estimator-v1': '字幕时间轴匹配',
    'doubao-seedance-2-0-fast-260128': '豆包视频生成模型',
    'deterministic-text': '本地模拟文本模型',
    'deterministic-image': '本地模拟图片模型',
    'deterministic-video-task': '本地模拟视频模型',
  }
  if (exact[normalized]) {
    return exact[normalized]
  }
  if (normalized.startsWith('doubao-seedream')) {
    return '豆包图片生成模型'
  }
  if (normalized.startsWith('doubao-seedance')) {
    return '豆包视频生成模型'
  }
  if (normalized.startsWith('doubao-seed')) {
    return '豆包文本生成模型'
  }
  return '其他模型'
}

export function sourceLabel(value?: string | null) {
  const normalized = value?.trim()
  if (!normalized || normalized === '人工/系统') {
    return '人工编辑或系统处理'
  }
  if (providerLabels[normalized]) {
    return providerLabels[normalized]
  }
  if (normalized.startsWith('doubao-') || normalized === 'seed-tts-2.0') {
    return `AI 生成 · ${modelLabel(normalized)}`
  }
  return /[\u3400-\u9FFF]/.test(normalized) ? normalized : '系统生成'
}

export function complianceFieldLabel(value?: string | null) {
  const path = value?.trim()
  if (!path) {
    return '文案正文'
  }
  if (complianceFieldLabels[path]) {
    return complianceFieldLabels[path]
  }
  const labels: string[] = []
  let pendingContainer: string | undefined
  const tokens = path.matchAll(/([A-Za-z_]\w*)|\[(\d+)\]/gu)
  for (const token of tokens) {
    const name = token[1]
    const index = token[2]
    if (name === 'content') {
      continue
    }
    if (name && complianceContainerLabels[name]) {
      pendingContainer = complianceContainerLabels[name]
      if (name === 'selected_candidate') {
        labels.push(pendingContainer)
        pendingContainer = undefined
      }
      continue
    }
    if (index != null) {
      labels.push(`${pendingContainer || '候选文案'} ${Number(index) + 1}`)
      pendingContainer = undefined
      continue
    }
    if (name) {
      labels.push(complianceFieldLabels[name] ?? '内容字段')
    }
  }
  return labels.join(' · ') || '文案正文'
}

export function complianceHitPresentation(hit: ComplianceHit) {
  const phrase = hit.phrase || hit.match || ''
  let prefix = hit.prefix ?? ''
  let match = hit.match || phrase
  let suffix = hit.suffix ?? ''
  if (!hit.prefix && !hit.suffix) {
    const source = hit.context || ''
    const index = source.indexOf(phrase)
    if (index >= 0) {
      prefix = source.slice(0, index)
      match = phrase
      suffix = source.slice(index + phrase.length)
    }
    else {
      prefix = source
      match = phrase
    }
  }
  return {
    typeLabel: complianceTypeLabel(hit.rule_type),
    fieldLabel: hit.field_label || complianceFieldLabel(hit.field),
    prefix,
    match,
    suffix,
    replacement: hit.replacement?.trim() || '',
  }
}

function contentErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : typeof error === 'string' ? error : ''
}

function isLocalizedReferenceImagePrivacyError(error: unknown) {
  const message = contentErrorMessage(error).trim()
  return (
    /^AI\s*视频生成未通过隐私安全检查[：:]/u.test(message)
    && /可能包含真实人物/u.test(message)
    && /请移除或更换/u.test(message)
  )
}

export function isReferenceImagePrivacyError(error: unknown) {
  const lower = contentErrorMessage(error).toLowerCase()
  return (
    /inputimagesensitivecontentdetected[.:_-]*privacyinformation/.test(lower)
    || /may contain (?:a )?real person/.test(lower)
    || isLocalizedReferenceImagePrivacyError(error)
  )
}

function providerReferenceImageNumber(message: string) {
  const patterns = [
    /\bcontent\s*\[\s*(\d+)\s*\]/i,
    /\bcontent at index[ :=#]+(\d+)/i,
    /\bcontent index[ :=#]+(\d+)/i,
    /\bcontent_index[\s:=#]+(\d+)/i,
  ]
  for (const pattern of patterns) {
    const matched = message.match(pattern)?.[1]
    if (matched == null) {
      continue
    }
    const index = Number(matched)
    // AI-video provider content[0] is the text prompt. Image references start
    // at content[1], so the provider index is also the user-facing ordinal.
    if (Number.isInteger(index) && index >= 1) {
      return index
    }
  }
  return undefined
}

function withoutRequestReference(message: string) {
  return message
    .replace(
      /[（(][ \t]*(?:请求编号|request[ _-]?id)[^()（）]*[）)]/giu,
      '',
    )
    .replace(/(?:请求编号|request[ _-]?id)[ :：=]+[\w-]+$/giu, '')
    .trim()
}

export function friendlyContentError(error: unknown, fallback = '操作失败，请稍后重试') {
  const normalized = withoutRequestReference(contentErrorMessage(error).trim())
  if (!normalized) {
    return fallback
  }

  const lower = normalized.toLowerCase()
  if (isLocalizedReferenceImagePrivacyError(normalized)) {
    return normalized
  }
  if (isReferenceImagePrivacyError(normalized)) {
    const contentIndex = providerReferenceImageNumber(normalized)
    const referenceLabel = contentIndex == null ? '参考图片' : `第 ${contentIndex} 张参考图`
    return `${referenceLabel}可能包含真实人物或其他隐私信息，AI 服务已拒绝处理。请移除该图，或改用不含真人及敏感个人信息的产品图、场景图或插画后重试`
  }
  if (/failed to fetch|networkerror|network error|econnrefused|connection refused/.test(lower)) {
    return '无法连接内容服务，请确认服务已启动后重试'
  }
  if (/timeout|timed out|etimedout/.test(lower)) {
    return '服务响应超时，请稍后重试'
  }
  if (/\b401\b|unauthorized|invalid api key|invalid token/.test(lower)) {
    return 'AI 服务鉴权失败，请联系管理员检查服务凭据'
  }
  if (/\b403\b|forbidden|permission denied/.test(lower)) {
    return '当前操作未获得授权，请联系管理员处理'
  }
  if (/\b404\b|not found/.test(lower)) {
    return '所需任务或内容不存在，可能已被移除，请刷新后重试'
  }
  if (/\b409\b|conflict|version mismatch|etag/.test(lower)) {
    return '内容已被其他操作更新，请刷新后再修改'
  }
  if (/\b422\b|validation error|invalid request|bad request/.test(lower)) {
    return '填写内容或生成参数不符合要求，请检查后重试'
  }
  if (/\b429\b|rate limit|too many requests/.test(lower)) {
    return 'AI 服务当前较忙，请稍后重试'
  }
  if (/\b5\d{2}\b|internal server error|bad gateway|service unavailable/.test(lower)) {
    return '内容服务暂时异常，请稍后重试'
  }
  if (
    /providercallerror|\bhttp(?:\/\d(?:\.\d)?)?\s*\d{3}\b|provider[_ -](?:task_)?id/.test(lower)
    || /(?:volcengine|doubao)-(?:ark|seed)/.test(lower)
    || /\b[a-z][\w.]*-[\w.]+-[\w.-]+\b/.test(lower)
    || /业务状态[ :：]?\d+/u.test(normalized)
    || /\bffmpeg\b/.test(lower)
    || /[a-z]:[\\/]|\/(?:tmp|var|home|app|srv)\//.test(lower)
    || /\b[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}\b/.test(lower)
    || /https?:\/\//.test(lower)
    || /\b[a-z][\w-]*(?:\s+[a-z][\w-]*){2,}\b/.test(lower)
  ) {
    return fallback
  }
  if (
    /[\u3400-\u9FFF]/.test(normalized)
    && !/traceback|stack|exception|sql|select\s|insert\s|update\s|delete\s/i.test(normalized)
  ) {
    return normalized
  }
  return fallback
}

export function parameterOptionLabel(key: string, value: string | number) {
  const normalized = String(value)
  if (normalized === 'adaptive') {
    return '模型自动'
  }
  if (key === 'voices') {
    return displayLabel(normalized, voiceLabels, '其他音色')
  }
  if (key === 'transitions') {
    return displayLabel(normalized, transitionLabels, '其他转场')
  }
  if (key === 'operations') {
    return displayLabel(normalized, operationLabels, '其他操作')
  }
  if (['sizes', 'ratios', 'durations', 'resolutions', 'formats'].includes(key)) {
    return normalized
  }
  return /[\u3400-\u9FFF]/.test(normalized) || typeof value === 'number' ? normalized : '其他选项'
}

export function isArtifactContentEditable(kind: string) {
  return ['topic', 'copy', 'storyboard', 'subtitle'].includes(kind)
}

export function stageDefinition(key: StageKey) {
  return stageDefinitions.find(item => item.key === key) ?? stageDefinitions[0]
}

export function normalizeStageKey(value?: string | null): StageKey {
  const legacyMap: Record<string, StageKey> = {
    configured: 'configuration',
    topics: 'topic',
    images: 'image',
    voice_subtitle: 'voice_subtitles',
    confirmed: 'export',
  }
  if (value && stageDefinitions.some(item => item.key === value)) {
    return value as StageKey
  }
  return value ? (legacyMap[value] ?? 'configuration') : 'configuration'
}

export function stageArtifacts(artifacts: Artifact[], key: StageKey) {
  const kinds = stageDefinition(key).artifactKinds
  return artifacts.filter(
    artifact =>
      artifact.status !== 'discarded'
      && (artifact.stage_key === key || kinds.includes(artifact.kind)),
  )
}

export function selectedIds(value: Record<string, string[]> | undefined) {
  return Object.values(value ?? {}).flat()
}

function transformCandidate(content: ArtifactContent): Record<string, unknown> {
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

function requestsTitleChange(instruction: string) {
  const normalized = instruction.toLocaleLowerCase().replace(/\s+/gu, ' ').trim()
  const titleChangeMarkers = [
    '修改标题',
    '改标题',
    '标题改',
    '标题更',
    '标题要',
    '题目改',
    '题目更',
    '优化标题',
    '调整标题',
    '重写标题',
    '改写标题',
    '精简标题',
    '缩短标题',
    '换个标题',
    '换一个标题',
    '标题换',
    '重新起标题',
    '重起标题',
    '另拟标题',
    '另起标题',
    '重新拟标题',
    'change title',
    'change the title',
    'edit title',
    'rewrite title',
    'shorten title',
    'replace title',
    'different title',
    'new title',
    'retitle',
    'change headline',
    'rewrite heading',
  ]
  const preserveMarkers = [
    '标题不变',
    '标题保持不变',
    '保留标题',
    '不要修改标题',
    '不要改标题',
    '不改标题',
    'keep the title',
  ]
  return titleChangeMarkers.some(marker => normalized.includes(marker))
    && !preserveMarkers.some(marker => normalized.includes(marker))
}

export function hasMaterialTransformChange(
  kind: string,
  sourceContent: ArtifactContent,
  transformedContent: ArtifactContent,
  instruction = '',
) {
  const source = transformCandidate(sourceContent)
  const transformed = transformCandidate(transformedContent)
  const normalize = (value: unknown) => String(value ?? '').replace(/\s+/gu, ' ').trim()
  const requiredFields = kind === 'topic'
    ? ['title']
    : requestsTitleChange(instruction)
      ? ['title', 'body']
      : ['body']
  return requiredFields.every(
    field => normalize(source[field]) !== normalize(transformed[field]),
  )
}

export function candidateText(candidate: Record<string, unknown>, artifactKind?: string) {
  const visibleFields = artifactKind === 'copy'
    ? [candidate.title, candidate.body || candidate.text]
    : [candidate.title, candidate.body, candidate.direction]
  return visibleFields
    .filter(item => typeof item === 'string' && item.trim())
    .join('\n')
}

export function artifactText(artifact: Artifact) {
  const content = artifact.content
  if (content.candidates?.length) {
    return content.candidates
      .map(candidate => candidateText(candidate, artifact.kind))
      .filter(Boolean)
      .join('\n\n')
  }
  const directFields = artifact.kind === 'copy'
    ? [content.title, content.body || content.text]
    : [content.title, content.body, content.text]
  const direct = directFields
    .filter(item => typeof item === 'string' && item.trim())
    .join('\n')
  if (direct) {
    return direct
  }
  if (artifact.kind === 'storyboard') {
    const shots = normalizeShots(content)
    if (!shots.length) {
      return '该分镜版本暂未包含镜头内容。'
    }
    const preview = shots
      .slice(0, 3)
      .map(shot => `镜头 ${shot.order}：${shot.visual || shot.narration || '未填写镜头内容'}`)
    return [
      `共 ${shots.length} 个镜头`,
      ...preview,
      shots.length > 3 ? '其余镜头请点击“查看/修改”查看。' : '',
    ]
      .filter(Boolean)
      .join('\n')
  }
  if (artifact.kind === 'subtitle') {
    const segments = Array.isArray(content.segments) ? content.segments : []
    if (!segments.length) {
      return '该字幕版本暂未包含字幕内容。'
    }
    const preview = segments
      .slice(0, 3)
      .map((segment, index) => `字幕 ${index + 1}：${segment.text || '未填写字幕内容'}`)
    return [
      `共 ${segments.length} 条字幕`,
      ...preview,
      segments.length > 3 ? '其余字幕请点击“查看/修改”查看。' : '',
    ]
      .filter(Boolean)
      .join('\n')
  }
  if (artifact.kind === 'compliance') {
    const hits = content.compliance?.hits ?? content.hits ?? []
    return hits.length
      ? `检测到 ${hits.length} 项需要人工处理的提示，请查看命中位置和上下文。`
      : '检测完成，未发现需要提示的关键词、敏感词或禁用词。'
  }
  const mediaLabels: Record<string, string> = {
    image: '图片',
    voice: '配音',
    ai_video_clip: 'AI 镜头视频',
    video: '视频成片',
  }
  if (mediaLabels[artifact.kind]) {
    return `该版本为${mediaLabels[artifact.kind]}文件，请使用预览或导出查看结果。`
  }
  return '该版本包含结构化内容，请点击“查看详情”查看。'
}

export function normalizeShots(content: ArtifactContent): StoryboardShot[] {
  const raw = Array.isArray(content.shots) ? content.shots : []
  return raw.map((shot, index) => {
    const value = shot as Record<string, unknown>
    const duration = value.duration_seconds ?? value.duration ?? value.seconds
    return {
      ...value,
      id: typeof value.id === 'string' ? value.id : undefined,
      order:
        typeof value.order === 'number'
          ? value.order
          : typeof value.sequence === 'number'
            ? value.sequence
            : index + 1,
      visual: String(value.visual ?? value.scene ?? value.picture ?? value.description ?? ''),
      narration: String(value.narration ?? value.voiceover ?? value.voice ?? ''),
      subtitle: String(value.subtitle ?? value.caption ?? value.narration ?? ''),
      duration_seconds: typeof duration === 'number' ? duration : Number(duration) || 3,
      transition: String(value.transition ?? ''),
      asset_id: typeof value.asset_id === 'string' ? value.asset_id : null,
    }
  })
}

export function createIdempotencyKey(taskId: string, stage: StageKey, requestScope = '') {
  const random
    = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  let requestFingerprint = 7
  for (const character of requestScope.trim()) {
    requestFingerprint
      = (requestFingerprint * 31 + (character.codePointAt(0) ?? 0)) % 2_147_483_647
  }
  const normalizedScope = requestScope.trim() ? `:${requestFingerprint.toString(36)}` : ''
  return `${taskId}:${stage}${normalizedScope}:${random}`
}

const BACKEND_DATE_TIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/iu
const EXPLICIT_TIME_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/iu

/**
 * Content-service timestamps are UTC. Older SQLite-backed responses omit the
 * trailing timezone, so normalize those values before browsers can treat them
 * as local wall-clock time. Explicit offsets remain authoritative.
 */
export function parseContentDateTime(value?: string | null): Date | null {
  const raw = value?.trim()
  if (!raw) {
    return null
  }
  const isoValue = BACKEND_DATE_TIME.test(raw) ? raw.replace(' ', 'T') : raw
  const normalized = BACKEND_DATE_TIME.test(raw) && !EXPLICIT_TIME_ZONE.test(raw)
    ? `${isoValue}Z`
    : isoValue
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function compareContentDateTimeDescending(
  left?: string | null,
  right?: string | null,
): number {
  const leftTime = parseContentDateTime(left)?.getTime() ?? Number.NEGATIVE_INFINITY
  const rightTime = parseContentDateTime(right)?.getTime() ?? Number.NEGATIVE_INFINITY
  return rightTime - leftTime
}

export function startOfBeijingCalendarDay(value: Date, dayOffset = 0): Date | null {
  if (Number.isNaN(value.getTime())) {
    return null
  }
  const calendarDay = new Date(Date.UTC(
    value.getFullYear(),
    value.getMonth(),
    value.getDate() + dayOffset,
  ))
  const year = calendarDay.getUTCFullYear().toString().padStart(4, '0')
  const month = (calendarDay.getUTCMonth() + 1).toString().padStart(2, '0')
  const day = calendarDay.getUTCDate().toString().padStart(2, '0')
  return parseContentDateTime(`${year}-${month}-${day}T00:00:00+08:00`)
}

export function formatDate(value?: string | null): string {
  if (!value) {
    return '—'
  }
  const parsed = parseContentDateTime(value)
  if (!parsed) {
    return value
  }
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(parsed)
}

export function isArchivedContentTask(
  task: Pick<ContentTask, 'archived' | 'status'>,
): boolean {
  return Boolean(task.archived || task.status === 'archived')
}

/** Home “最近任务” must follow the same recency as 历史内容, not raw API page order. */
export function recentContentTasks<T extends Pick<ContentTask, 'updated_at' | 'created_at' | 'archived' | 'status'>>(
  tasks: readonly T[],
  limit = 6,
): T[] {
  return [...tasks]
    .sort((left, right) => {
      const archivedDelta = Number(isArchivedContentTask(left)) - Number(isArchivedContentTask(right))
      if (archivedDelta !== 0) {
        return archivedDelta
      }
      return compareContentDateTimeDescending(
        left.updated_at || left.created_at,
        right.updated_at || right.created_at,
      )
    })
    .slice(0, limit)
}
