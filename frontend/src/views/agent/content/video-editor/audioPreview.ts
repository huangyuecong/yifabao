/* eslint-disable financial/no-division */
export interface AudioPreviewInput {
  playhead: number
  clipStart: number
  clipDuration: number
  trimIn: number
  trimOut: number
  speed: number
  volume: number
  sourceDuration: number
  loop: boolean
  fadeIn: number
  fadeOut: number
}

export interface AudioPreviewState {
  sourceTime: number
  sourceStart: number
  sourceEnd: number
  shouldPlay: boolean
  playbackRate: number
  /** Linear gain, including fades. Values above one require a Web Audio GainNode. */
  volume: number
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

/** Calculate looped source time and the instantaneous fade envelope for HTML audio. */
export function audioPreviewState(input: AudioPreviewInput): AudioPreviewState {
  const clipDuration = Number.isFinite(input.clipDuration) ? Math.max(0, input.clipDuration) : 0
  const clipStart = Number.isFinite(input.clipStart) ? input.clipStart : 0
  const playhead = Number.isFinite(input.playhead) ? input.playhead : clipStart - 1
  const localSeconds = clamp(playhead - clipStart, 0, clipDuration)
  const speed = clamp(input.speed || 1, 0.1, 8)
  const sourceDuration = Number.isFinite(input.sourceDuration) ? Math.max(0, input.sourceDuration) : 0
  const trimIn = clamp(input.trimIn || 0, 0, sourceDuration)
  const sourceEnd = Math.max(trimIn, sourceDuration - Math.max(0, input.trimOut || 0))
  const sourceWindow = sourceEnd - trimIn
  const sourceOffset = localSeconds * speed
  const normalizedOffset = input.loop && sourceWindow > 0
    ? sourceOffset % sourceWindow
    : Math.min(sourceOffset, sourceWindow)
  const withinClip = playhead >= clipStart && playhead < clipStart + clipDuration
  const shouldPlay = withinClip && sourceWindow > 0 && (input.loop || sourceOffset < sourceWindow)
  const fadeIn = Math.max(0, input.fadeIn || 0)
  const fadeOut = Math.max(0, input.fadeOut || 0)
  const fadeInGain = fadeIn > 0 ? clamp(localSeconds / fadeIn, 0, 1) : 1
  const audibleDuration = input.loop ? clipDuration : Math.min(clipDuration, sourceWindow / speed)
  const remaining = Math.max(0, audibleDuration - localSeconds)
  const fadeOutGain = fadeOut > 0 ? clamp(remaining / fadeOut, 0, 1) : 1
  return {
    sourceTime: clamp(trimIn + normalizedOffset, trimIn, sourceEnd),
    sourceStart: trimIn,
    sourceEnd,
    shouldPlay,
    playbackRate: speed,
    volume: shouldPlay ? clamp(input.volume || 0, 0, 2) * Math.min(fadeInGain, fadeOutGain) : 0,
  }
}
