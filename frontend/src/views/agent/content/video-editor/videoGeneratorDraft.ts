export interface VideoGeneratorDraftStorage {
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
}

export interface VideoGeneratorDraft {
  version: 1
  voice: {
    text: string
    label: string
    voice: string
    speed: number
    volume: number
    audioFormat: 'wav' | 'mp3' | 'ogg_opus'
    instruction: string
    generateSubtitles: boolean
  }
  subtitle: {
    sourceMode: 'voice' | 'text'
    sourceProjectAssetId: string
    sourceText: string
    text: string
    label: string
    maxCharsPerLine: number
    maxLines: number
  }
  image: {
    prompt: string
    label: string
    role: 'video' | 'overlay' | 'logo' | 'intro' | 'outro'
    count: number
    size: string
    ratio: string
    watermark: boolean
  }
  aiVideo: {
    prompt: string
    referenceImageAssetIds: string[]
    referenceVideoAssetIds: string[]
    durationSeconds: number
    ratio: string
    resolution: string
    generateAudio: boolean
    returnLastFrame: boolean
  }
  music: {
    prompt: string
    duration: number
    format: 'mp3' | 'wav'
  }
}

export type VideoGeneratorDraftInput = Omit<VideoGeneratorDraft, 'version'>

const DRAFT_VERSION = 1
const DRAFT_KEY_PREFIX = 'content-video-generator-draft'
const MAX_TEXT_LENGTH = 20_000
const MAX_REFERENCE_IDS = 50

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function safeString(value: unknown, fallback: string) {
  return typeof value === 'string' ? value.slice(0, MAX_TEXT_LENGTH) : fallback
}

function safeNumber(value: unknown, fallback: number, minimum: number, maximum: number) {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(maximum, Math.max(minimum, value))
    : fallback
}

function safeInteger(value: unknown, fallback: number, minimum: number, maximum: number) {
  return Math.round(safeNumber(value, fallback, minimum, maximum))
}

function safeBoolean(value: unknown, fallback: boolean) {
  return typeof value === 'boolean' ? value : fallback
}

function safeStringIds(value: unknown, fallback: string[]) {
  if (!Array.isArray(value)) {
    return [...fallback]
  }
  return [...new Set(value.filter(item => typeof item === 'string').map(item => item.trim()))]
    .filter(Boolean)
    .slice(0, MAX_REFERENCE_IDS)
}

function safeEnum<const T extends string>(value: unknown, options: readonly T[], fallback: T): T {
  return typeof value === 'string' && options.includes(value as T) ? value as T : fallback
}

export function videoGeneratorDraftKey(taskId: string) {
  return taskId.trim() ? `${DRAFT_KEY_PREFIX}:${taskId.trim()}` : ''
}

export function normalizeVideoGeneratorDraft(
  value: unknown,
  defaults: VideoGeneratorDraftInput,
): VideoGeneratorDraft | undefined {
  if (!isRecord(value) || value.version !== DRAFT_VERSION) {
    return undefined
  }
  const voice = isRecord(value.voice) ? value.voice : {}
  const subtitle = isRecord(value.subtitle) ? value.subtitle : {}
  const image = isRecord(value.image) ? value.image : {}
  const aiVideo = isRecord(value.aiVideo) ? value.aiVideo : {}
  const music = isRecord(value.music) ? value.music : {}
  return {
    version: DRAFT_VERSION,
    voice: {
      text: safeString(voice.text, defaults.voice.text),
      label: safeString(voice.label, defaults.voice.label),
      voice: safeString(voice.voice, defaults.voice.voice),
      speed: safeNumber(voice.speed, defaults.voice.speed, 0.1, 8),
      volume: safeNumber(voice.volume, defaults.voice.volume, 0, 10),
      audioFormat: safeEnum(
        voice.audioFormat,
        ['wav', 'mp3', 'ogg_opus'] as const,
        defaults.voice.audioFormat,
      ),
      instruction: safeString(voice.instruction, defaults.voice.instruction),
      generateSubtitles: safeBoolean(
        voice.generateSubtitles,
        defaults.voice.generateSubtitles,
      ),
    },
    subtitle: {
      sourceMode: safeEnum(
        subtitle.sourceMode,
        ['voice', 'text'] as const,
        defaults.subtitle.sourceMode,
      ),
      sourceProjectAssetId: safeString(
        subtitle.sourceProjectAssetId,
        defaults.subtitle.sourceProjectAssetId,
      ),
      sourceText: safeString(subtitle.sourceText, defaults.subtitle.sourceText),
      text: safeString(subtitle.text, defaults.subtitle.text),
      label: safeString(subtitle.label, defaults.subtitle.label),
      maxCharsPerLine: safeInteger(
        subtitle.maxCharsPerLine,
        defaults.subtitle.maxCharsPerLine,
        1,
        100,
      ),
      maxLines: safeInteger(subtitle.maxLines, defaults.subtitle.maxLines, 1, 4),
    },
    image: {
      prompt: safeString(image.prompt, defaults.image.prompt),
      label: safeString(image.label, defaults.image.label),
      role: safeEnum(
        image.role,
        ['video', 'overlay', 'logo', 'intro', 'outro'] as const,
        defaults.image.role,
      ),
      count: safeInteger(image.count, defaults.image.count, 1, 8),
      size: safeString(image.size, defaults.image.size),
      ratio: safeString(image.ratio, defaults.image.ratio),
      watermark: safeBoolean(image.watermark, defaults.image.watermark),
    },
    aiVideo: {
      prompt: safeString(aiVideo.prompt, defaults.aiVideo.prompt),
      referenceImageAssetIds: safeStringIds(
        aiVideo.referenceImageAssetIds,
        defaults.aiVideo.referenceImageAssetIds,
      ),
      referenceVideoAssetIds: safeStringIds(
        aiVideo.referenceVideoAssetIds,
        defaults.aiVideo.referenceVideoAssetIds,
      ),
      durationSeconds: safeNumber(
        aiVideo.durationSeconds,
        defaults.aiVideo.durationSeconds,
        1,
        600,
      ),
      ratio: safeString(aiVideo.ratio, defaults.aiVideo.ratio),
      resolution: safeString(aiVideo.resolution, defaults.aiVideo.resolution),
      generateAudio: safeBoolean(aiVideo.generateAudio, defaults.aiVideo.generateAudio),
      returnLastFrame: safeBoolean(
        aiVideo.returnLastFrame,
        defaults.aiVideo.returnLastFrame,
      ),
    },
    music: {
      prompt: safeString(music.prompt, defaults.music.prompt),
      duration: safeNumber(music.duration, Math.min(15, defaults.music.duration), 1, 15),
      format: safeEnum(music.format, ['mp3', 'wav'] as const, defaults.music.format),
    },
  }
}

export function loadVideoGeneratorDraft(
  storage: VideoGeneratorDraftStorage,
  taskId: string,
  defaults: VideoGeneratorDraftInput,
) {
  const key = videoGeneratorDraftKey(taskId)
  if (!key) {
    return undefined
  }
  try {
    const serialized = storage.getItem(key)
    return serialized
      ? normalizeVideoGeneratorDraft(JSON.parse(serialized), defaults)
      : undefined
  }
  catch {
    return undefined
  }
}

export function saveVideoGeneratorDraft(
  storage: VideoGeneratorDraftStorage,
  taskId: string,
  value: VideoGeneratorDraftInput,
) {
  const key = videoGeneratorDraftKey(taskId)
  if (!key) {
    return false
  }
  try {
    const normalized = normalizeVideoGeneratorDraft(
      { ...value, version: DRAFT_VERSION },
      value,
    )
    if (!normalized) {
      return false
    }
    storage.setItem(key, JSON.stringify(normalized))
    return true
  }
  catch {
    return false
  }
}
