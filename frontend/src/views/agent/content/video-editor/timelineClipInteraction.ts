/* eslint-disable financial/no-division, financial/no-float-calculation */
import type { VideoTimelineItemState, VideoTrackKind } from './timelineState'
import { isTimedTimelineItem, timelineItemSourceDuration } from './timelineState'

export type ClipResizePatch = Pick<
  VideoTimelineItemState,
  'start_seconds' | 'duration_seconds' | 'trim_in_seconds' | 'trim_out_seconds' | 'speed'
>

const MIN_DURATION = 0.1
const MAX_DURATION = 600
export const CLIP_GESTURE_THRESHOLD = 4

function clamp(value: number, minimum: number, maximum: number) {
  return Math.max(minimum, Math.min(maximum, value))
}

/** Both handles and keyboard edits use the same source window and bounds. */
export function resizeTimelineClip(
  item: VideoTimelineItemState,
  kind: VideoTrackKind,
  leftDelta: number,
  rightDelta: number,
  knownSourceDuration?: number,
): ClipResizePatch {
  const speed = clamp(item.speed, 0.1, 8)
  const unchanged = {
    start_seconds: item.start_seconds,
    duration_seconds: item.duration_seconds,
    trim_in_seconds: item.trim_in_seconds,
    trim_out_seconds: item.trim_out_seconds,
    speed: item.speed,
  }
  if (!Number.isFinite(leftDelta) || !Number.isFinite(rightDelta) || (!leftDelta && !rightDelta)) {
    return unchanged
  }

  if (kind === 'video' && isTimedTimelineItem(item, kind)) {
    const sourceDuration = knownSourceDuration && Number.isFinite(knownSourceDuration) && knownSourceDuration > 0
      ? knownSourceDuration
      : timelineItemSourceDuration(item)
        ?? item.trim_in_seconds + item.duration_seconds * speed + item.trim_out_seconds
    const minimumSpan = Math.min(sourceDuration, MIN_DURATION * speed)
    const originalIn = clamp(item.trim_in_seconds, 0, sourceDuration - minimumSpan)
    // A previous duration-only edit may not have set trim_out. The displayed
    // interval is authoritative; unshown source frames remain recoverable.
    const originalEnd = clamp(originalIn + item.duration_seconds * speed, originalIn + minimumSpan, sourceDuration)
    const trimIn = clamp(originalIn + leftDelta * speed, 0, originalEnd - minimumSpan)
    const sourceEnd = clamp(originalEnd + rightDelta * speed, trimIn + minimumSpan, sourceDuration)
    return {
      ...unchanged,
      duration_seconds: (sourceEnd - trimIn) / speed,
      trim_in_seconds: trimIn,
      trim_out_seconds: sourceDuration - sourceEnd,
    }
  }

  if (kind === 'video') {
    // Still images have a display duration, not a finite video source window.
    return {
      ...unchanged,
      duration_seconds: clamp(item.duration_seconds + rightDelta - leftDelta, MIN_DURATION, MAX_DURATION),
      trim_in_seconds: 0,
      trim_out_seconds: 0,
    }
  }

  const originalEnd = item.start_seconds + item.duration_seconds
  const requestedStart = clamp(item.start_seconds + leftDelta, 0, originalEnd - MIN_DURATION)
  const requestedDuration = originalEnd + rightDelta - requestedStart
  const audio = ['voice', 'music', 'sfx'].includes(kind)
  const sourceSpan = item.duration_seconds * speed
  const minimum = audio ? Math.max(MIN_DURATION, sourceSpan / 8) : MIN_DURATION
  const maximum = audio ? Math.min(MAX_DURATION, sourceSpan / 0.1) : MAX_DURATION
  const duration = clamp(requestedDuration, minimum, leftDelta ? Math.min(maximum, originalEnd) : maximum)
  return {
    ...unchanged,
    start_seconds: leftDelta ? originalEnd - duration : requestedStart,
    duration_seconds: duration,
    speed: audio ? sourceSpan / duration : speed,
  }
}
