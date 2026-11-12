import { describe, expect, it } from 'vitest'
import { audioPreviewState } from './audioPreview.ts'

const base = {
  playhead: 0,
  clipStart: 0,
  clipDuration: 12,
  trimIn: 1,
  trimOut: 1,
  speed: 1,
  volume: 0.8,
  sourceDuration: 6,
  loop: false,
  fadeIn: 0,
  fadeOut: 0,
}

describe('audio preview state', () => {
  it('loops a short source inside its trimmed playback window', () => {
    const state = audioPreviewState({ ...base, playhead: 9, loop: true })
    expect(state.sourceTime).toBe(2)
  })

  it('applies fade-in and fade-out envelopes to instantaneous volume', () => {
    expect(audioPreviewState({ ...base, playhead: 1, fadeIn: 2 }).volume).toBeCloseTo(0.4)
    expect(audioPreviewState({ ...base, playhead: 11, sourceDuration: 14, fadeOut: 2 }).volume).toBeCloseTo(0.4)
  })

  it('stops at the non-looped source end instead of replaying a trimmed tail', () => {
    const state = audioPreviewState({ ...base, playhead: 11, volume: 2 })
    expect(state.sourceTime).toBe(5)
    expect(state.shouldPlay).toBe(false)
    expect(state.volume).toBe(0)
  })

  it('retains gain up to 200 percent for the Web Audio output controller', () => {
    const state = audioPreviewState({ ...base, playhead: 1, volume: 2 })
    expect(state.shouldPlay).toBe(true)
    expect(state.volume).toBe(2)
  })

  it('does not play empty or invalid trimmed windows', () => {
    expect(audioPreviewState({ ...base, trimIn: 4, trimOut: 4 }).shouldPlay).toBe(false)
    expect(audioPreviewState({ ...base, sourceDuration: 0, loop: true }).shouldPlay).toBe(false)
  })
})
