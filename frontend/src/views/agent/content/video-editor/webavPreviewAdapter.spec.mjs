import { describe, expect, it } from 'vitest'
import { webAvEligibility } from './webavPreviewAdapter.ts'

const eligible = {
  featureEnabled: true,
  userAgent: 'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36',
  hasWebCodecs: true,
  sourceBytes: 10 * 1024 * 1024,
  canvasPixels: 1920 * 1080,
  deviceMemory: 8,
  hardwareConcurrency: 8,
}

describe('webAV preview eligibility', () => {
  it('allows a capable Chrome session', () => {
    expect(webAvEligibility(eligible).eligible).toBe(true)
  })

  it('requires the feature flag and capacity thresholds', () => {
    expect(webAvEligibility({ ...eligible, featureEnabled: false }).eligible).toBe(false)
    expect(webAvEligibility({ ...eligible, deviceMemory: 2 }).eligible).toBe(false)
    expect(webAvEligibility({ ...eligible, sourceBytes: 300 * 1024 * 1024 }).eligible).toBe(false)
  })

  it('rejects browsers outside Chrome and Edge', () => {
    expect(webAvEligibility({ ...eligible, userAgent: 'Mozilla/5.0 Firefox/142.0' }).eligible).toBe(false)
  })
})
