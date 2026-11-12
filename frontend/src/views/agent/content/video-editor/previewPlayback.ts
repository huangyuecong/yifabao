/* eslint-disable financial/no-float-calculation */
interface PlaybackTarget {
  sourceTime: number
  speed: number
  playing: boolean
  revision: number
  cycle?: number
}

interface PlaybackState {
  revision: number
  cycle: number
  playing: boolean
  lastSeek: number
  driftSince?: number
  pendingPlay?: Promise<void>
}

/** Let media decode continuously; only seek on user edits, loop boundaries or sustained drift. */
export function createPreviewPlaybackController(now = () => performance.now()) {
  const states = new WeakMap<HTMLMediaElement, PlaybackState>()

  function sync(element: HTMLMediaElement, target: PlaybackTarget) {
    const time = now()
    const previous = states.get(element)
    const cycle = target.cycle ?? 0
    const state = previous ?? {
      revision: target.revision,
      cycle,
      playing: false,
      lastSeek: Number.NEGATIVE_INFINITY,
    }
    states.set(element, state)
    const changed = !previous || state.revision !== target.revision || state.cycle !== cycle
      || state.playing !== target.playing || element.playbackRate !== target.speed
    if (element.playbackRate !== target.speed) {
      element.playbackRate = target.speed
    }
    const drift = Math.abs(element.currentTime - target.sourceTime)
    const ready = !element.seeking && element.readyState >= 3
    // Small startup/decoder jitter must not flush the decoder every animation frame.
    // Compare in timeline seconds so faster clips do not trigger more corrections.
    const outOfSync = drift > 0.25 * Math.max(1, target.speed)
    if (target.playing && !changed && ready && outOfSync) {
      state.driftSince ??= time
    }
    else {
      state.driftSince = undefined
    }
    const sustainedDrift = state.driftSince !== undefined && time - state.driftSince >= 500
      && time - state.lastSeek >= 1000
    if (drift > 0.001 && (changed || !target.playing || sustainedDrift)) {
      // Explicit scrubbing may replace an in-flight seek; periodic correction may not.
      if (changed || !element.seeking) {
        element.currentTime = target.sourceTime
        state.lastSeek = time
        state.driftSince = undefined
      }
    }
    state.revision = target.revision
    state.cycle = cycle
    state.playing = target.playing
    if (!target.playing) {
      if (!element.paused) {
        element.pause()
      }
    }
    else if (element.paused && !state.pendingPlay) {
      state.pendingPlay = element.play().then(() => {
        if (!state.playing) {
          element.pause()
        }
      }).catch(() => undefined).finally(() => {
        state.pendingPlay = undefined
      })
    }
  }

  function stop(element: HTMLMediaElement) {
    if (!element.paused) {
      element.pause()
    }
    const state = states.get(element)
    if (state) {
      state.playing = false
      state.driftSince = undefined
    }
  }

  return { sync, stop }
}
