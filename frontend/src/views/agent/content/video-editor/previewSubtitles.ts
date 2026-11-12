/* eslint-disable financial/no-division */
import type { VideoTimelineState } from './timelineState'

function timestamp(milliseconds: number) {
  const hours = Math.floor(milliseconds / 3_600_000)
  const minutes = Math.floor(milliseconds / 60_000) % 60
  const seconds = Math.floor(milliseconds / 1000) % 60
  const fraction = milliseconds % 1000
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(fraction).padStart(3, '0')}`
}

function cueText(value: string) {
  const normalized = value
    .replace(/\r\n?/gu, '\n')
    // eslint-disable-next-line no-control-regex -- Remove forbidden control bytes from the WebVTT cue payload.
    .replace(/[\u0000-\u0008\v\f\u000E-\u001F\u007F]/gu, '')
    .trim()
  if (!normalized) {
    return ''
  }
  return normalized
    .replace(/&/gu, '&amp;')
    .replace(/</gu, '&lt;')
    .replace(/>/gu, '&gt;')
    .split('\n')
    // A blank line terminates a WebVTT cue. Keep an intended blank subtitle
    // line inside its cue so user text cannot inject another cue or NOTE block.
    .map(line => line.trim() ? line : '\u200B')
    .join('\n')
}

/** Browser preview sidecar for inline timeline captions, without changing export files. */
export function timelineWebVtt(timeline: VideoTimelineState): string {
  const cues: Array<{ start: number, end: number, text: string }> = []
  for (const track of timeline.tracks) {
    if (track.kind !== 'subtitle' || track.muted) {
      continue
    }
    for (const item of track.items) {
      if (item.enabled === false || typeof item.content !== 'string'
        || !Number.isFinite(item.start_seconds) || item.start_seconds < 0
        || !Number.isFinite(item.duration_seconds) || item.duration_seconds <= 0) {
        continue
      }
      const text = cueText(item.content)
      const start = Math.round(item.start_seconds * 1000)
      const end = Math.max(start + 1, Math.round((item.start_seconds + item.duration_seconds) * 1000))
      if (!text || !Number.isSafeInteger(start) || !Number.isSafeInteger(end)) {
        continue
      }
      cues.push({ start, end, text })
    }
  }
  // ES stable sort preserves timeline order when cues start at the same time.
  cues.sort((first, second) => first.start - second.start)
  return `WEBVTT\n\n${cues.map((cue, index) =>
    `${index + 1}\n${timestamp(cue.start)} --> ${timestamp(cue.end)}\n${cue.text}\n\n`,
  ).join('')}`
}
