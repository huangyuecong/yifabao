import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./AudioWaveform.vue', import.meta.url), 'utf8')

describe('audio waveform loading contract', () => {
  it('lets WaveSurfer auto-load its external media exactly once', () => {
    expect(source).toContain('media: audioElement.value')
    expect(source).toContain('url: props.url')
    expect(source).not.toMatch(/\b(?:instance|waveSurfer)\.load\s*\(/)
  })

  it('scopes load errors to the current instance and ignores cancellations', () => {
    expect(source).toContain('error.name === \'AbortError\'')
    expect(source).toContain('instance.on(\'error\', error => fail(error, generation, instance))')
    expect(source).toContain('generation === sourceGeneration')
    expect(source).toContain('waveSurfer === instance')
  })

  it('keeps the HTML audio fallback registered with the editor', () => {
    expect(source).toContain('emit(\'mediaElement\', props.itemId, audioElement.value)')
    expect(source).toContain('emit(\'mediaElement\', props.itemId, null)')
    expect(source).toContain('<audio')
  })
})
