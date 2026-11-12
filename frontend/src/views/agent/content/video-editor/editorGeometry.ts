/* eslint-disable financial/no-division */

export interface FittedCanvasSize {
  width: number
  height: number
}

export function timelinePixelsPerSecond(zoom: number, base = 42) {
  const normalized = Number.isFinite(zoom) ? Math.min(2.5, Math.max(0.5, zoom)) : 1
  return base * normalized
}

export function timelineRulerStep(pixelsPerSecond: number) {
  if (pixelsPerSecond < 36) {
    return 5
  }
  if (pixelsPerSecond < 84) {
    return 2
  }
  return 1
}

export function fitCanvasInViewport(
  canvasWidth: number,
  canvasHeight: number,
  viewportWidth: number,
  viewportHeight: number,
  padding = 20,
): FittedCanvasSize {
  const sourceWidth = Math.max(1, Number(canvasWidth) || 1)
  const sourceHeight = Math.max(1, Number(canvasHeight) || 1)
  const availableWidth = Math.max(0, (Number(viewportWidth) || 0) - padding)
  const availableHeight = Math.max(0, (Number(viewportHeight) || 0) - padding)
  if (!availableWidth || !availableHeight) {
    return { width: 0, height: 0 }
  }
  const scale = Math.min(availableWidth / sourceWidth, availableHeight / sourceHeight)
  return {
    width: Math.max(1, Math.round(sourceWidth * scale)),
    height: Math.max(1, Math.round(sourceHeight * scale)),
  }
}
