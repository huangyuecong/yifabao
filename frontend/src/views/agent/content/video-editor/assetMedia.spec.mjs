import { describe, expect, it } from 'vitest'
import { selectProjectAssetPreviewUrl } from './assetMedia.ts'

function asset(overrides = {}) {
  return {
    id: 'asset-1',
    source_type: 'artifact',
    source_id: 'source-1',
    role: 'voice',
    label: '素材',
    stream_url: '/api/audio/stream',
    original_url: '/api/audio/download',
    metadata: { media_kind: 'audio', media_type: 'audio/mpeg' },
    active: true,
    derivatives: [],
    created_at: '2026-08-28T00:00:00Z',
    updated_at: '2026-08-28T00:00:00Z',
    ...overrides,
  }
}

describe('project asset preview URL selection', () => {
  it('never treats an audio waveform image as the audio source after reload', () => {
    const value = asset({
      derivatives: [{ kind: 'waveform', media_type: 'image/png', download_url: '/api/waveform.png' }],
    })
    expect(selectProjectAssetPreviewUrl(value)).toBe('/api/audio/stream')
  })

  it('recognizes generated music by its track role when legacy metadata omits media kind', () => {
    const value = asset({
      role: 'music',
      metadata: { source: 'ai-generated', prompt: '东方氛围' },
      derivatives: [{ kind: 'waveform', media_type: 'image/png', download_url: '/api/waveform.png' }],
    })
    expect(selectProjectAssetPreviewUrl(value)).toBe('/api/audio/stream')
  })

  it('uses a playable video proxy before the source stream', () => {
    const value = asset({
      role: 'video',
      metadata: { media_kind: 'video', media_type: 'video/mp4' },
      derivatives: [
        { kind: 'thumbnail', media_type: 'image/jpeg', download_url: '/api/thumb.jpg' },
        { kind: 'proxy', media_type: 'video/mp4', download_url: '/api/proxy.mp4' },
      ],
    })
    expect(selectProjectAssetPreviewUrl(value)).toBe('/api/proxy.mp4')
  })

  it('uses an image thumbnail without affecting original downloads', () => {
    const value = asset({
      role: 'overlay',
      metadata: { media_kind: 'image', media_type: 'image/png' },
      derivatives: [{ kind: 'thumbnail', media_type: 'image/jpeg', download_url: '/api/thumb.jpg' }],
    })
    expect(selectProjectAssetPreviewUrl(value)).toBe('/api/thumb.jpg')
  })
})
