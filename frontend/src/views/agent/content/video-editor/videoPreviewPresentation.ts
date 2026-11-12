/* eslint-disable financial/no-division */
import type { VideoCanvasState, VideoTimelineItemState, VideoTimelineState } from './timelineState'

export interface VisualPreviewLayer {
  item: VideoTimelineItemState
  opacity: number
  sourceTime: number
  hold: boolean
  backgroundColor?: string
}

function number(value: unknown, fallback: number) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.max(minimum, Math.min(maximum, value))
}

function sourceDuration(item: VideoTimelineItemState) {
  const metadata = item.metadata
  const probe = metadata.probe as Record<string, unknown> | undefined
  for (const candidate of [metadata.source_duration_seconds, metadata.duration_seconds, probe?.duration_seconds]) {
    const value = Number(candidate)
    if (Number.isFinite(value) && value > 0) {
      return value
    }
  }
  return undefined
}

/** Hold the last usable source frame after the clip, without replaying its audio. */
export function videoPreviewTiming(
  item: VideoTimelineItemState,
  playhead: number,
  knownSourceDuration?: number,
  fps = 25,
) {
  const speed = Math.max(0.1, number(item.speed, 1))
  const trimIn = Math.max(0, number(item.trim_in_seconds, 0))
  const trimOut = Math.max(0, number(item.trim_out_seconds, 0))
  const duration = Math.max(0.1, number(item.duration_seconds, 0.1))
  const known = knownSourceDuration && Number.isFinite(knownSourceDuration) && knownSourceDuration > 0
    ? knownSourceDuration
    : sourceDuration(item)
  const sourceEnd = Math.max(trimIn, Math.min(
    trimIn + duration * speed,
    known == null ? Number.POSITIVE_INFINITY : Math.max(0, known - trimOut),
  ))
  const local = number(playhead, 0) - item.start_seconds
  const unclamped = trimIn + Math.max(0, local) * speed
  const hold = local < 0 || local >= duration || unclamped >= sourceEnd
  const frameDuration = speed / clamp(number(fps, 25), 1, 60)
  const lastFrame = Math.max(trimIn, sourceEnd - frameDuration)
  return {
    sourceTime: clamp(unclamped, trimIn, hold ? lastFrame : sourceEnd),
    hold,
  }
}

export function visualFitMode(item: VideoTimelineItemState): 'contain' | 'cover' {
  return item.metadata.fit_mode === 'cover' ? 'cover' : 'contain'
}

/** Main-track overlap uses the same entrance-transition timing as the renderer. */
export function visualLayers(timeline: VideoTimelineState, playhead: number): VisualPreviewLayer[] {
  const track = timeline.tracks.find(item => item.kind === 'video')
  if (!track || track.muted) {
    return []
  }
  const items = track.items.filter(item => item.enabled)
  if (!items.length) {
    return []
  }
  const layers = items.filter(item => playhead >= item.start_seconds
    && playhead < item.start_seconds + item.duration_seconds)
  const selected = layers.length ? layers : [playhead < items[0]!.start_seconds ? items[0]! : items.at(-1)!]
  const result: VisualPreviewLayer[] = selected.map(item => ({
    item,
    opacity: 1,
    ...videoPreviewTiming(item, playhead, undefined, timeline.canvas.fps),
  }))
  if (result.length < 2) {
    return result
  }
  const previous = result[result.length - 2]!
  const current = result[result.length - 1]!
  const overlap = previous.item.start_seconds + previous.item.duration_seconds - current.item.start_seconds
  const progress = clamp((playhead - current.item.start_seconds) / Math.max(0.001, overlap), 0, 1)
  const transition = String(current.item.metadata.transition || 'none')
  if (transition === 'fadeblack' || transition === 'fadewhite') {
    const color = transition === 'fadewhite' ? '#FFFFFF' : '#000000'
    previous.opacity = Math.max(0, 1 - progress * 2)
    current.opacity = Math.max(0, progress * 2 - 1)
    previous.backgroundColor = color
    current.backgroundColor = color
  }
  else if (!['none', 'cut', ''].includes(transition)) {
    // Opaque previous frame beneath the incoming layer gives a true linear blend.
    current.opacity = progress
  }
  return result
}

function canvasScale(canvas: VideoCanvasState, viewportWidth: number) {
  return Math.max(0.001, number(viewportWidth, canvas.width) / Math.max(1, canvas.width))
}

function color(value: unknown, fallback: string) {
  return typeof value === 'string' && /^#[0-9a-f]{6}$/iu.test(value) ? value : fallback
}

/** Values are canvas pixels; the CSS preview scales them to its actual viewport. */
export function subtitlePreviewStyle(item: VideoTimelineItemState, canvas: VideoCanvasState, viewportWidth: number) {
  const metadata = item.metadata.subtitle_style
  const style = metadata && typeof metadata === 'object' ? metadata as Record<string, unknown> : {}
  const scale = canvasScale(canvas, viewportWidth)
  const fontSize = clamp(number(style.font_size, 28), 8, 120) * scale
  const outline = clamp(number(style.outline_width, 2), 0, 10) * scale
  const shadow = clamp(number(style.shadow, 0), 0, 10) * scale
  const alignment = clamp(Math.round(number(style.alignment, 2)), 1, 9)
  const horizontal = (alignment - 1) % 3
  const vertical = Math.floor((alignment - 1) / 3)
  const margin = clamp(number(style.margin_v, 36), 0, 500) * scale
  return {
    position: 'absolute' as const,
    left: horizontal === 0 ? `${36 * scale}px` : horizontal === 1 ? '50%' : 'auto',
    right: horizontal === 2 ? `${36 * scale}px` : 'auto',
    top: vertical === 2 ? `${margin}px` : vertical === 1 ? '50%' : 'auto',
    bottom: vertical === 0 ? `${margin}px` : 'auto',
    transform: `translate(${horizontal === 1 ? '-50%' : '0'}, ${vertical === 1 ? '-50%' : '0'})`,
    maxWidth: '94%',
    width: 'max-content',
    textAlign: (['left', 'center', 'right'] as const)[horizontal],
    whiteSpace: 'pre-wrap' as const,
    overflowWrap: 'anywhere' as const,
    fontSize: `${fontSize}px`,
    lineHeight: '1.2',
    fontWeight: style.bold ? 700 : 400,
    color: color(style.primary_color, '#FFFFFF'),
    WebkitTextStroke: `${outline}px ${color(style.outline_color, '#000000')}`,
    paintOrder: 'stroke fill',
    textShadow: shadow ? `${shadow}px ${shadow}px 0 ${color(style.outline_color, '#000000')}` : 'none',
  }
}

export function overlayPreviewStyle(item: VideoTimelineItemState, canvas: VideoCanvasState, viewportWidth: number) {
  const scale = canvasScale(canvas, viewportWidth)
  const transform = item.transform || {}
  const width = Math.max(1, number(item.metadata.width, 160)) * clamp(number(transform.scale, 1), 0.1, 10) * scale
  return {
    position: 'absolute' as const,
    left: `${clamp(number(transform.x, 50), 0, 100)}%`,
    top: `${clamp(number(transform.y, 15), 0, 100)}%`,
    width: `${width}px`,
    maxWidth: 'none',
    opacity: clamp(number(transform.opacity, 1), 0, 1),
    transform: 'translate(-50%, -50%)',
    objectFit: visualFitMode(item),
  }
}
