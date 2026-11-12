/* eslint-disable financial/no-division */
import { describe, expect, it } from 'vitest'
import {
  fitCanvasInViewport,
  timelinePixelsPerSecond,
  timelineRulerStep,
} from './editorGeometry.ts'

describe('video editor geometry', () => {
  it('changes timeline scale across the complete slider range without a dead zone', () => {
    expect(timelinePixelsPerSecond(0.5)).toBe(21)
    expect(timelinePixelsPerSecond(1)).toBe(42)
    expect(timelinePixelsPerSecond(2.5)).toBe(105)
    expect(timelineRulerStep(21)).toBe(5)
    expect(timelineRulerStep(42)).toBe(2)
    expect(timelineRulerStep(105)).toBe(1)
  })

  it.each([
    [1920, 1080, 16 / 9],
    [1080, 1920, 9 / 16],
    [1080, 1080, 1],
    [1440, 1080, 4 / 3],
  ])('fits a %sx%s canvas while preserving its visible ratio', (width, height, ratio) => {
    const fitted = fitCanvasInViewport(width, height, 900, 360)
    expect(fitted.width).toBeLessThanOrEqual(880)
    expect(fitted.height).toBeLessThanOrEqual(340)
    expect(fitted.width / fitted.height).toBeCloseTo(ratio, 2)
  })
})
