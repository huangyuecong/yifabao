/* eslint-disable financial/no-division */
import type { VideoCanvasState } from './timelineState'

export const WEBAV_MAX_SOURCE_BYTES = 256 * 1024 * 1024
export const WEBAV_MAX_CANVAS_PIXELS = 4096 * 2160

export type WebAvPreviewState = 'disabled' | 'checking' | 'ready' | 'failed'

export interface WebAvPreviewStatus {
  state: WebAvPreviewState
  reason: string
}

export interface WebAvPreviewProbe {
  duration_seconds: number
  width: number
  height: number
  adapter: 'webav-proxy'
}

export interface WebAvEligibilityInput {
  featureEnabled: boolean
  userAgent: string
  hasWebCodecs: boolean
  sourceBytes: number
  canvasPixels: number
  deviceMemory?: number
  hardwareConcurrency?: number
}

export function webAvEligibility(input: WebAvEligibilityInput) {
  if (!input.featureEnabled) {
    return { eligible: false, reason: 'WebAV 预览特性未开启' }
  }
  if (!/(?:Chrome|Edg)\//i.test(input.userAgent) || /(?:OPR|Firefox)\//i.test(input.userAgent)) {
    return { eligible: false, reason: '仅在 Chrome 或 Edge 启用 WebAV 预检' }
  }
  if (!input.hasWebCodecs) {
    return { eligible: false, reason: '浏览器不支持完整 WebCodecs' }
  }
  if (input.sourceBytes > WEBAV_MAX_SOURCE_BYTES) {
    return { eligible: false, reason: '素材超过 WebAV 预检容量门槛' }
  }
  if (input.canvasPixels > WEBAV_MAX_CANVAS_PIXELS) {
    return { eligible: false, reason: '画布超过 WebAV 预检分辨率门槛' }
  }
  if (input.deviceMemory != null && input.deviceMemory < 4) {
    return { eligible: false, reason: '设备内存不足 4 GiB' }
  }
  if (input.hardwareConcurrency != null && input.hardwareConcurrency < 4) {
    return { eligible: false, reason: '设备可用处理器核心不足' }
  }
  return { eligible: true, reason: '满足 WebAV 预检条件' }
}

type WebAvModule = typeof import('@webav/av-cliper')

function withTimeout<T>(promise: Promise<T>, milliseconds: number) {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('WebAV 预检超时')), milliseconds)
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      (error) => {
        clearTimeout(timer)
        reject(error)
      },
    )
  })
}

export class WebAvPreviewAdapter {
  private module?: WebAvModule
  private disabledForSession = false
  private status: WebAvPreviewStatus = {
    state: 'disabled',
    reason: '尚未执行 WebAV 预检',
  }

  constructor(private readonly onStatus?: (status: WebAvPreviewStatus) => void) {}

  getStatus() {
    return { ...this.status }
  }

  async probe(file: File, canvas: VideoCanvasState): Promise<WebAvPreviewProbe | undefined> {
    if (this.disabledForSession || !/^video\/mp4$/i.test(file.type)) {
      return undefined
    }
    const deviceMemory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory
    const result = webAvEligibility({
      featureEnabled: (import.meta.env as unknown as Record<string, string | undefined>)
        .VITE_VIDEO_WEBAV_PREVIEW === 'true',
      userAgent: navigator.userAgent,
      hasWebCodecs: typeof VideoDecoder !== 'undefined'
        && typeof VideoEncoder !== 'undefined'
        && typeof OffscreenCanvas !== 'undefined',
      sourceBytes: file.size,
      canvasPixels: canvas.width * canvas.height,
      deviceMemory,
      hardwareConcurrency: navigator.hardwareConcurrency || undefined,
    })
    if (!result.eligible) {
      this.update({ state: 'disabled', reason: `${result.reason}，使用 HTML5/服务端预览` })
      return undefined
    }

    this.update({ state: 'checking', reason: '正在动态加载 WebAV 并预检素材' })
    let clip: InstanceType<WebAvModule['MP4Clip']> | undefined
    try {
      this.module ||= await import('@webav/av-cliper')
      const supported = await this.module.Combinator.isSupported({
        width: canvas.width,
        height: canvas.height,
      })
      if (!supported) {
        this.update({ state: 'disabled', reason: '当前编码能力不满足要求，使用 HTML5/服务端预览' })
        return undefined
      }
      clip = new this.module.MP4Clip(file.stream())
      const metadata = await withTimeout(clip.ready, 12_000)
      const durationSeconds = Number(metadata.duration) / 1_000_000
      if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
        throw new Error('WebAV 未返回有效视频时长')
      }
      this.update({ state: 'ready', reason: 'WebAV 仅用于素材预检；播放仍由 HTML5/服务端承担' })
      return {
        duration_seconds: durationSeconds,
        width: metadata.width,
        height: metadata.height,
        adapter: 'webav-proxy',
      }
    }
    catch (error) {
      this.disabledForSession = true
      const reason = error instanceof Error ? error.message : '未知错误'
      this.update({ state: 'failed', reason: `WebAV 预检失败：${reason}；本会话已回退 HTML5/服务端` })
      return undefined
    }
    finally {
      clip?.destroy()
    }
  }

  private update(status: WebAvPreviewStatus) {
    this.status = status
    this.onStatus?.({ ...status })
  }
}
