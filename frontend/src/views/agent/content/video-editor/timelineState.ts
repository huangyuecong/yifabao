/* eslint-disable financial/no-float-calculation, financial/no-division */
export type VideoTrackKind = 'video' | 'overlay' | 'subtitle' | 'voice' | 'music' | 'sfx'
export type VisualTrackKind = Extract<VideoTrackKind, 'video' | 'overlay'>

export interface VideoCanvasState {
  width: number
  height: number
  fps: number
  aspect_ratio: string
}

export interface VideoTransformState {
  x?: number
  y?: number
  scale?: number
  rotation?: number
  opacity?: number
  [key: string]: unknown
}

export interface VideoTimelineItemState {
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
  transform: VideoTransformState
  metadata: Record<string, unknown>
}

export interface VideoTimelineTrackState {
  id: string
  kind: VideoTrackKind
  name: string
  muted: boolean
  locked: boolean
  items: VideoTimelineItemState[]
}

export interface VideoTimelineState {
  canvas: VideoCanvasState
  tracks: VideoTimelineTrackState[]
}

export interface SubtitleCue {
  start_seconds: number
  end_seconds: number
  text: string
}

export const TRACK_DEFINITIONS: ReadonlyArray<{
  kind: VideoTrackKind
  name: string
}> = [
  { kind: 'video', name: '画面' },
  { kind: 'overlay', name: '叠加' },
  { kind: 'subtitle', name: '字幕' },
  { kind: 'voice', name: '配音' },
  { kind: 'music', name: '音乐' },
  { kind: 'sfx', name: '音效' },
]

function finiteNumber(value: unknown, fallback: number) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function nonNegative(value: unknown, fallback = 0) {
  return Math.max(0, finiteNumber(value, fallback))
}

export function createTimeline(): VideoTimelineState {
  return {
    canvas: {
      width: 1080,
      height: 1920,
      fps: 25,
      aspect_ratio: '9:16',
    },
    tracks: TRACK_DEFINITIONS.map(definition => ({
      id: `track-${definition.kind}`,
      kind: definition.kind,
      name: definition.name,
      muted: false,
      locked: false,
      items: [],
    })),
  }
}

export function cloneTimeline(value: VideoTimelineState): VideoTimelineState {
  return {
    canvas: { ...value.canvas },
    tracks: value.tracks.map(track => ({
      ...track,
      items: track.items.map(item => ({
        ...item,
        transform: { ...item.transform },
        metadata: { ...item.metadata },
      })),
    })),
  }
}

export function normalizeTimeline(value?: Partial<VideoTimelineState> | null): VideoTimelineState {
  const fallback = createTimeline()
  const incomingTracks = Array.isArray(value?.tracks) ? value.tracks : []
  const tracks = TRACK_DEFINITIONS.map((definition) => {
    const incoming = incomingTracks.find(track => track?.kind === definition.kind)
    const items = Array.isArray(incoming?.items) ? incoming.items : []
    return {
      id: incoming?.id || `track-${definition.kind}`,
      kind: definition.kind,
      name: incoming?.name || definition.name,
      muted: Boolean(incoming?.muted),
      locked: Boolean(incoming?.locked),
      items: items.map((item, index) => normalizeItem(item, definition.kind, index)),
    }
  })
  const canvas = value?.canvas
  return compactVideoTrack({
    canvas: {
      width: Math.max(16, Math.round(finiteNumber(canvas?.width, fallback.canvas.width))),
      height: Math.max(16, Math.round(finiteNumber(canvas?.height, fallback.canvas.height))),
      fps: Math.min(60, Math.max(1, Math.round(finiteNumber(canvas?.fps, fallback.canvas.fps)))),
      aspect_ratio: String(canvas?.aspect_ratio || fallback.canvas.aspect_ratio),
    },
    tracks,
  })
}

export function normalizeItem(
  value: Partial<VideoTimelineItemState>,
  kind: VideoTrackKind,
  index = 0,
): VideoTimelineItemState {
  const duration = Math.max(0.1, finiteNumber(value.duration_seconds, kind === 'subtitle' ? 3 : 4))
  return {
    id: value.id || `item-${kind}-${index + 1}`,
    project_asset_id: value.project_asset_id ?? null,
    asset_ref_id: value.asset_ref_id ?? null,
    artifact_id: value.artifact_id ?? null,
    library_item_id: value.library_item_id ?? null,
    resource_entry_id: value.resource_entry_id ?? null,
    label: String(value.label || `${TRACK_DEFINITIONS.find(item => item.kind === kind)?.name || '素材'} ${index + 1}`),
    start_seconds: nonNegative(value.start_seconds),
    duration_seconds: duration,
    trim_in_seconds: isTimedTimelineItem(value, kind) ? nonNegative(value.trim_in_seconds) : 0,
    trim_out_seconds: isTimedTimelineItem(value, kind) ? nonNegative(value.trim_out_seconds) : 0,
    volume: Math.min(2, nonNegative(value.volume, 1)),
    speed: Math.max(0.1, finiteNumber(value.speed, 1)),
    enabled: value.enabled !== false,
    content: value.content == null ? null : String(value.content),
    transform: { ...(value.transform || {}) },
    metadata: { ...(value.metadata || {}) },
  }
}

export function findTrack(timeline: VideoTimelineState, kind: VideoTrackKind) {
  return timeline.tracks.find(track => track.kind === kind)
}

export function findItem(timeline: VideoTimelineState, itemId: string) {
  for (const track of timeline.tracks) {
    const item = track.items.find(candidate => candidate.id === itemId)
    if (item) {
      return { track, item }
    }
  }
  return undefined
}

interface TimelineTrackOperation {
  type: string
  track_id?: string
  track_kind?: VideoTrackKind
  item_id?: string
  [key: string]: unknown
}

function asVideoTrackKind(value: unknown): VideoTrackKind | undefined {
  return TRACK_DEFINITIONS.some(definition => definition.kind === value)
    ? value as VideoTrackKind
    : undefined
}

/**
 * Replace editor-only/default track ids with the ids owned by a persisted
 * project while keeping every local item and edit intact.
 */
export function rebaseTimelineTrackIds(
  value: VideoTimelineState,
  persisted: Pick<VideoTimelineState, 'tracks'>,
) {
  const persistedIds = new Map(
    persisted.tracks
      .filter(track => Boolean(track.id))
      .map(track => [track.kind, track.id]),
  )
  const next = cloneTimeline(value)
  next.tracks = next.tracks.map(track => ({
    ...track,
    id: persistedIds.get(track.kind) || track.id,
  }))
  return next
}

/**
 * Repoint queued operations at persisted track ids. This is needed when a
 * user edits a brand-new local timeline before its server project exists.
 */
export function rebaseTimelineOperationTrackIds<T extends TimelineTrackOperation>(
  operations: T[],
  local: VideoTimelineState,
  persisted: Pick<VideoTimelineState, 'tracks'>,
) {
  const localKindsByTrackId = new Map(local.tracks.map(track => [track.id, track.kind]))
  const localKindsByItemId = new Map(
    local.tracks.flatMap(track => track.items.map(item => [item.id, track.kind] as const)),
  )
  const persistedTracksByKind = new Map(
    persisted.tracks
      .filter(track => Boolean(track.id))
      .map(track => [track.kind, track]),
  )

  return operations.map((operation) => {
    const kind
      = asVideoTrackKind(operation.track_kind)
        || (operation.track_id ? localKindsByTrackId.get(operation.track_id) : undefined)
        || (operation.item_id ? localKindsByItemId.get(operation.item_id) : undefined)
    const persistedTrack = kind ? persistedTracksByKind.get(kind) : undefined
    if (!kind || !persistedTrack) {
      return operation
    }
    return {
      ...operation,
      track_id: persistedTrack.id,
      ...(operation.type === 'add' ? { track_kind: kind } : {}),
    }
  })
}

export function timelineDuration(timeline: VideoTimelineState) {
  return timeline.tracks.reduce((maximum, track) => Math.max(
    maximum,
    ...(track.muted ? [] : track.items.filter(item => item.enabled))
      .map(item => item.start_seconds + item.duration_seconds),
  ), 0)
}

export function addItem(
  timeline: VideoTimelineState,
  kind: VideoTrackKind,
  item: Partial<VideoTimelineItemState>,
  index?: number,
) {
  const next = cloneTimeline(timeline)
  const track = findTrack(next, kind)
  if (!track || track.locked) {
    return next
  }
  const normalized = normalizeItem(item, kind, track.items.length)
  const target = index == null ? track.items.length : Math.max(0, Math.min(index, track.items.length))
  track.items.splice(target, 0, normalized)
  return kind === 'video' ? compactVideoTrack(next) : next
}

export function removeItem(timeline: VideoTimelineState, itemId: string) {
  const next = cloneTimeline(timeline)
  for (const track of next.tracks) {
    if (track.locked) {
      continue
    }
    const index = track.items.findIndex(item => item.id === itemId)
    if (index >= 0) {
      track.items.splice(index, 1)
      return track.kind === 'video' ? compactVideoTrack(next) : next
    }
  }
  return next
}

export function updateItem(
  timeline: VideoTimelineState,
  itemId: string,
  patch: Partial<VideoTimelineItemState>,
  knownSourceDuration?: number,
) {
  const next = cloneTimeline(timeline)
  const located = findItem(next, itemId)
  if (!located || located.track.locked) {
    return next
  }
  const normalizedPatch = trimPatch(located.item, patch, knownSourceDuration, located.track.kind)
  const merged = normalizeItem({
    ...located.item,
    ...normalizedPatch,
    transform: patch.transform ? { ...located.item.transform, ...patch.transform } : located.item.transform,
    metadata: patch.metadata ? { ...located.item.metadata, ...patch.metadata } : located.item.metadata,
  }, located.track.kind)
  const index = located.track.items.findIndex(item => item.id === itemId)
  located.track.items.splice(index, 1, merged)
  return located.track.kind === 'video' ? compactVideoTrack(next) : next
}

export function reorderItem(
  timeline: VideoTimelineState,
  kind: VideoTrackKind,
  itemId: string,
  targetIndex: number,
) {
  const next = cloneTimeline(timeline)
  const track = findTrack(next, kind)
  if (!track || track.locked) {
    return next
  }
  const sourceIndex = track.items.findIndex(item => item.id === itemId)
  if (sourceIndex < 0) {
    return next
  }
  const [item] = track.items.splice(sourceIndex, 1)
  if (!item) {
    return next
  }
  track.items.splice(Math.max(0, Math.min(targetIndex, track.items.length)), 0, item)
  return kind === 'video' ? compactVideoTrack(next) : next
}

export function moveItemByOffset(
  timeline: VideoTimelineState,
  itemId: string,
  offset: number,
) {
  const located = findItem(timeline, itemId)
  if (!located) {
    return timeline
  }
  const index = located.track.items.findIndex(item => item.id === itemId)
  return reorderItem(timeline, located.track.kind, itemId, index + offset)
}

export function isImageTimelineItem(item: VideoTimelineItemState) {
  const mediaKind = String(item.metadata.media_kind || item.metadata.resource_kind || '')
  const mediaType = String(item.metadata.mime_type || item.metadata.media_type || '')
  return mediaKind === 'image' || mediaType.startsWith('image/')
}

export function isTimedTimelineItem(item: Partial<VideoTimelineItemState>, kind?: VideoTrackKind) {
  const metadata = item.metadata || {}
  const mediaKind = String(metadata.media_kind || metadata.resource_kind || '')
  const mediaType = String(metadata.mime_type || metadata.media_type || '')
  if (kind === 'subtitle' || ['image', 'logo', 'subtitle'].includes(mediaKind) || mediaType.startsWith('image/')) {
    return false
  }
  return ['audio', 'video'].includes(mediaKind)
    || /^(?:audio|video)\//u.test(mediaType)
    || ['voice', 'music', 'sfx', 'video'].includes(kind || '')
}

export function timelineItemSourceDuration(item: Partial<VideoTimelineItemState>) {
  const metadata = item.metadata || {}
  const probe = metadata.probe as Record<string, unknown> | undefined
  for (const value of [metadata.source_duration_seconds, metadata.duration_seconds, probe?.duration_seconds]) {
    const duration = Number(value)
    if (Number.isFinite(duration) && duration > 0) {
      return duration
    }
  }
  return undefined
}

/** Trim controls change the selected source window, not just its playback offset. */
export function trimPatch(
  item: VideoTimelineItemState,
  patch: Partial<VideoTimelineItemState>,
  knownDuration?: number,
  kind?: VideoTrackKind,
): Partial<VideoTimelineItemState> {
  if (!isTimedTimelineItem(item, kind)) {
    return { ...patch, trim_in_seconds: 0, trim_out_seconds: 0 }
  }
  const changingTrim = patch.trim_in_seconds != null || patch.trim_out_seconds != null
  const changingDuration = patch.duration_seconds != null
  if (!changingTrim && !changingDuration) {
    return patch
  }
  const speed = Math.max(0.1, finiteNumber(patch.speed, item.speed))
  const oldSpeed = Math.max(0.1, item.speed)
  const sourceDuration = knownDuration && Number.isFinite(knownDuration) && knownDuration > 0
    ? knownDuration
    : timelineItemSourceDuration(item)
  const inferredSpan = item.trim_in_seconds + item.duration_seconds * oldSpeed + item.trim_out_seconds
  const sourceLimit = sourceDuration ?? inferredSpan
  const minimumSpan = Math.min(sourceLimit, 0.1 * speed)
  let trimIn = nonNegative(patch.trim_in_seconds ?? item.trim_in_seconds)
  let trimOut = nonNegative(patch.trim_out_seconds ?? item.trim_out_seconds)
  if (changingTrim && sourceLimit > 0) {
    if (patch.trim_in_seconds != null && patch.trim_out_seconds == null) {
      trimOut = Math.min(trimOut, Math.max(0, sourceLimit - minimumSpan))
      trimIn = Math.min(trimIn, Math.max(0, sourceLimit - trimOut - minimumSpan))
    }
    else {
      trimIn = Math.min(trimIn, Math.max(0, sourceLimit - minimumSpan))
      trimOut = Math.min(trimOut, Math.max(0, sourceLimit - trimIn - minimumSpan))
    }
  }
  let duration = changingDuration
    ? Math.max(0.1, finiteNumber(patch.duration_seconds, item.duration_seconds))
    : Math.max(0.1, item.duration_seconds
    + (item.trim_in_seconds - trimIn + item.trim_out_seconds - trimOut) / speed)
  if (sourceDuration && !item.metadata.loop) {
    duration = Math.min(duration, Math.max(0.1, (sourceDuration - trimIn - trimOut) / speed))
  }
  return { ...patch, trim_in_seconds: trimIn, trim_out_seconds: trimOut, duration_seconds: duration }
}

/** A clip's transition is its entrance from the preceding enabled main clip. */
export function visualTransitionOverlap(previous: VideoTimelineItemState, current: VideoTimelineItemState) {
  const transition = String(current.metadata.transition || 'none').toLowerCase()
  if (['', 'none', 'cut'].includes(transition)) {
    return 0
  }
  return Math.min(
    5,
    nonNegative(current.metadata.transition_duration_seconds, 0.4),
    previous.duration_seconds / 2,
    current.duration_seconds / 2,
  )
}

/**
 * Move a visual clip between the main and overlay tracks without changing its
 * identity or source reference. Overlay clips keep their absolute timing;
 * clips returning to the main track are inserted at their chronological
 * position before the compact main-track layout is rebuilt.
 */
export function moveVisualItemToTrack(
  timeline: VideoTimelineState,
  itemId: string,
  targetKind: VisualTrackKind,
) {
  const located = findItem(timeline, itemId)
  const target = findTrack(timeline, targetKind)
  if (
    !located
    || !target
    || located.track.kind === targetKind
    || !['video', 'overlay'].includes(located.track.kind)
    || located.track.locked
    || target.locked
    || (targetKind === 'overlay' && !isImageTimelineItem(located.item))
  ) {
    return timeline
  }

  const originalStart = located.item.start_seconds
  const next = cloneTimeline(timeline)
  const nextSource = findTrack(next, located.track.kind)
  const nextTarget = findTrack(next, targetKind)
  const sourceIndex = nextSource?.items.findIndex(item => item.id === itemId) ?? -1
  if (!nextSource || !nextTarget || sourceIndex < 0) {
    return timeline
  }
  const [item] = nextSource.items.splice(sourceIndex, 1)
  if (!item) {
    return timeline
  }

  const targetIndex = targetKind === 'video'
    ? nextTarget.items.findIndex(candidate => candidate.start_seconds >= originalStart)
    : nextTarget.items.length
  nextTarget.items.splice(targetIndex < 0 ? nextTarget.items.length : targetIndex, 0, {
    ...item,
    start_seconds: originalStart,
  })
  return compactVideoTrack(next)
}

/** Move only track membership; audio timing, source trims and mix settings stay intact. */
export function moveAudioItemToTrack(
  timeline: VideoTimelineState,
  itemId: string,
  targetKind: Extract<VideoTrackKind, 'voice' | 'music' | 'sfx'>,
) {
  const located = findItem(timeline, itemId)
  const target = findTrack(timeline, targetKind)
  if (
    !located
    || !target
    || !['voice', 'music', 'sfx'].includes(targetKind)
    || !['voice', 'music', 'sfx'].includes(located.track.kind)
    || located.track.kind === targetKind
    || located.track.locked
    || target.locked
  ) {
    return timeline
  }
  const next = cloneTimeline(timeline)
  const source = findTrack(next, located.track.kind)!
  const destination = findTrack(next, targetKind)!
  const index = source.items.findIndex(item => item.id === itemId)
  const [item] = source.items.splice(index, 1)
  if (!item) {
    return timeline
  }
  const destinationIndex = destination.items.findIndex(candidate => candidate.start_seconds > item.start_seconds)
  destination.items.splice(destinationIndex < 0 ? destination.items.length : destinationIndex, 0, item)
  return next
}

export function duplicateItem(timeline: VideoTimelineState, itemId: string, newId: string) {
  const located = findItem(timeline, itemId)
  if (!located) {
    return timeline
  }
  const index = located.track.items.findIndex(item => item.id === itemId)
  const copy: VideoTimelineItemState = {
    ...located.item,
    id: newId,
    label: `${located.item.label} 副本`,
    start_seconds: located.track.kind === 'video'
      ? located.item.start_seconds + located.item.duration_seconds
      : located.item.start_seconds + 0.25,
    transform: { ...located.item.transform },
    metadata: { ...located.item.metadata },
  }
  return addItem(timeline, located.track.kind, copy, index + 1)
}

export function splitItem(
  timeline: VideoTimelineState,
  itemId: string,
  splitAtSeconds: number,
  newId: string,
) {
  const located = findItem(timeline, itemId)
  if (!located || located.track.locked) {
    return timeline
  }
  const relative = splitAtSeconds - located.item.start_seconds
  if (relative <= 0.05 || relative >= located.item.duration_seconds - 0.05) {
    return timeline
  }
  const next = cloneTimeline(timeline)
  const target = findItem(next, itemId)
  if (!target) {
    return timeline
  }
  const index = target.track.items.findIndex(item => item.id === itemId)
  const originalDuration = target.item.duration_seconds
  const rightDuration = originalDuration - relative
  const right: VideoTimelineItemState = {
    ...target.item,
    id: newId,
    label: `${target.item.label}（后段）`,
    start_seconds: target.item.start_seconds + relative,
    duration_seconds: rightDuration,
    trim_in_seconds: isTimedTimelineItem(target.item, target.track.kind)
      ? target.item.trim_in_seconds + relative * target.item.speed
      : 0,
    transform: { ...target.item.transform },
    metadata: { ...target.item.metadata },
  }
  target.item.duration_seconds = relative
  target.item.trim_out_seconds = isTimedTimelineItem(target.item, target.track.kind)
    ? target.item.trim_out_seconds + rightDuration * target.item.speed
    : 0
  target.track.items.splice(index + 1, 0, right)
  return target.track.kind === 'video' ? compactVideoTrack(next) : next
}

export function compactVideoTrack(timeline: VideoTimelineState) {
  const next = cloneTimeline(timeline)
  const track = findTrack(next, 'video')
  if (!track) {
    return next
  }
  let cursor = 0
  let previous: VideoTimelineItemState | undefined
  track.items = track.items.map((item) => {
    if (item.enabled && previous) {
      cursor -= visualTransitionOverlap(previous, item)
    }
    const normalized = { ...item, start_seconds: cursor }
    if (item.enabled) {
      cursor += item.duration_seconds
      previous = item
    }
    return normalized
  })
  return next
}

function parseTimestamp(value: string) {
  const normalized = value.trim().replace(',', '.')
  const segments = normalized.split(':').map(Number)
  if (segments.some(segment => !Number.isFinite(segment))) {
    return undefined
  }
  if (segments.length === 3) {
    return segments[0]! * 3600 + segments[1]! * 60 + segments[2]!
  }
  if (segments.length === 2) {
    return segments[0]! * 60 + segments[1]!
  }
  return segments.length === 1 ? segments[0] : undefined
}

export function parseSubtitleText(source: string, extension = 'srt'): SubtitleCue[] {
  const normalized = source.replace(/^WEBVTT[^\n]*\n+/i, '').replace(/\r\n?/g, '\n').trim()
  if (!normalized) {
    return []
  }
  if (extension.toLowerCase() === 'txt') {
    return normalized.split(/\n+/).map(line => line.trim()).filter(Boolean).map((text, index) => ({
      start_seconds: index * 3,
      end_seconds: index * 3 + 2.8,
      text,
    }))
  }
  const blocks = normalized.split(/\n{2,}/)
  const cues: SubtitleCue[] = []
  for (const block of blocks) {
    const lines = block.split('\n').map(line => line.trim()).filter(Boolean)
    const timingIndex = lines.findIndex(line => line.includes('-->'))
    if (timingIndex < 0) {
      continue
    }
    const [startRaw, endRawWithSettings] = lines[timingIndex]!.split('-->').map(value => value.trim())
    const start = startRaw ? parseTimestamp(startRaw) : undefined
    const end = endRawWithSettings ? parseTimestamp(endRawWithSettings.split(/\s+/)[0] || '') : undefined
    const text = lines.slice(timingIndex + 1).join('\n')
    if (start == null || end == null || end <= start || !text) {
      continue
    }
    cues.push({ start_seconds: start, end_seconds: end, text })
  }
  return cues.sort((left, right) => left.start_seconds - right.start_seconds)
}
