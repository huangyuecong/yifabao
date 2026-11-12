interface MediaGainBinding {
  context: AudioContext
  source: MediaElementAudioSourceNode
  gain: GainNode
  owners: Set<symbol>
  connected: boolean
  bypassGain: boolean
}

export interface PreviewAudioGainResult {
  mode: 'web-audio' | 'native'
  gain: number
  limited: boolean
}

export interface PreviewAudioGainOptions {
  onWarning?: (message: string) => void
  /** Injectable for tests; the browser context is shared lazily in production. */
  createContext?: () => AudioContext | undefined
}

// A media element may only be associated with one MediaElementAudioSourceNode
// for its lifetime, even after disconnecting it. Retain this identity in a WeakMap.
const bindings = new WeakMap<HTMLMediaElement, MediaGainBinding>()
const activeBindings = new Map<AudioContext, Set<MediaGainBinding>>()
const pendingResumes = new WeakMap<AudioContext, Promise<boolean>>()
let sharedContext: AudioContext | undefined

function browserContext() {
  if (typeof AudioContext === 'undefined') {
    return undefined
  }
  if (!sharedContext || sharedContext.state === 'closed') {
    sharedContext = new AudioContext()
  }
  return sharedContext
}

function resumeContext(context: AudioContext) {
  if (context.state === 'running') {
    return Promise.resolve(true)
  }
  const pending = pendingResumes.get(context)
  if (pending) {
    return pending
  }
  let request: Promise<void>
  try {
    request = context.resume()
  }
  catch {
    return Promise.resolve(false)
  }
  const next = request
    .then(() => context.state === 'running')
    .catch(() => false)
    .finally(() => pendingResumes.delete(context))
  pendingResumes.set(context, next)
  return next
}

/** Apply timeline gain without changing media files or creating duplicate sources. */
export function createPreviewAudioGainController(options: PreviewAudioGainOptions = {}) {
  const owner = Symbol('video-preview-audio')
  const owned = new Map<HTMLMediaElement, MediaGainBinding>()
  const warnings = new Set<string>()
  const failedElements = new WeakSet<HTMLMediaElement>()
  const resumedContexts = new Set<AudioContext>()
  let disposed = false
  let resumeRequested = false

  function warn(message: string) {
    if (!warnings.has(message)) {
      warnings.add(message)
      options.onWarning?.(message)
    }
  }

  async function ensureRunning(context: AudioContext) {
    const running = await resumeContext(context)
    if (!running && !disposed) {
      warn('浏览器暂未允许音频播放，请再次点击播放按钮。')
    }
    return running
  }

  function register(element: HTMLMediaElement, binding: MediaGainBinding) {
    binding.owners.add(owner)
    owned.set(element, binding)
    const connected = activeBindings.get(binding.context) || new Set<MediaGainBinding>()
    connected.add(binding)
    activeBindings.set(binding.context, connected)
    if (resumeRequested && !resumedContexts.has(binding.context)) {
      resumedContexts.add(binding.context)
      void ensureRunning(binding.context)
    }
  }

  function fallback(element: HTMLMediaElement, gain: number): PreviewAudioGainResult {
    const actualGain = Math.min(1, gain)
    if (element.volume !== actualGain) {
      element.volume = actualGain
    }
    if (gain > 1) {
      warn('当前浏览器不支持音量增强，实时预览最高为 100%；可继续调整其他参数。')
    }
    return { mode: 'native', gain: actualGain, limited: gain > 1 }
  }

  function apply(element: HTMLMediaElement, value: number): PreviewAudioGainResult {
    const gain = Math.max(0, Math.min(2, Number.isFinite(value) ? value : 1))
    if (disposed || (failedElements.has(element) && !bindings.has(element))) {
      return fallback(element, gain)
    }
    let binding = bindings.get(element)
    try {
      if (!binding) {
        const context = (options.createContext || browserContext)()
        if (!context) {
          failedElements.add(element)
          return fallback(element, gain)
        }
        // Create the ordinary node first: if it fails, the element still uses
        // its native audio path and remains safe to preview without amplification.
        const gainNode = context.createGain()
        const source = context.createMediaElementSource(element)
        binding = { context, source, gain: gainNode, owners: new Set(), connected: false, bypassGain: false }
        bindings.set(element, binding)
      }
      if (!binding.connected) {
        if (binding.bypassGain) {
          binding.source.connect(binding.context.destination)
        }
        else {
          binding.source.connect(binding.gain)
          binding.gain.connect(binding.context.destination)
        }
        binding.connected = true
      }
      register(element, binding)
      if (binding.bypassGain) {
        return fallback(element, gain)
      }
      if (element.volume !== 1) {
        element.volume = 1
      }
      if (binding.gain.gain.value !== gain) {
        binding.gain.gain.value = gain
      }
      return { mode: 'web-audio', gain, limited: false }
    }
    catch {
      failedElements.add(element)
      if (binding) {
        // Once a source exists, native output is routed through Web Audio. Keep
        // a unity path connected if gain setup failed instead of silencing it.
        try {
          binding.source.disconnect()
          binding.gain.disconnect()
          binding.source.connect(binding.context.destination)
          binding.bypassGain = true
          binding.connected = true
          register(element, binding)
        }
        catch {
          warn('当前音频预览暂不可用，请重新打开编辑器后再试。')
        }
      }
      return fallback(element, gain)
    }
  }

  async function resume() {
    if (disposed) {
      return false
    }
    // Called in the user's play gesture; new elements applied during the same
    // playback also resume their shared context when it becomes available.
    resumeRequested = true
    resumedContexts.clear()
    const contexts = new Set([...owned.values()].map(binding => binding.context))
    contexts.forEach(context => resumedContexts.add(context))
    const results = await Promise.all([...contexts].map(ensureRunning))
    return results.every(Boolean)
  }

  function release(element: HTMLMediaElement) {
    const binding = owned.get(element)
    if (!binding) {
      return
    }
    owned.delete(element)
    binding.owners.delete(owner)
    if (binding.owners.size) {
      return
    }
    binding.source.disconnect()
    binding.gain.disconnect()
    binding.connected = false
    element.volume = 1
    const connected = activeBindings.get(binding.context)
    connected?.delete(binding)
    if (!connected?.size) {
      activeBindings.delete(binding.context)
      resumedContexts.delete(binding.context)
      // Keep the shared context reusable: closing it permanently invalidates
      // cached media sources, including an element reused after a component swap.
      void binding.context.suspend().catch(() => undefined)
    }
  }

  function dispose() {
    if (disposed) {
      return
    }
    disposed = true
    for (const element of [...owned.keys()]) {
      release(element)
    }
  }

  return { apply, resume, release, dispose }
}
