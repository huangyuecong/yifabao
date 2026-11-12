import { computed, ref } from 'vue'

/*
 * Voice input for the chat box. The browser's own speech recognition (Edge, Chrome)
 * needs no model configuration: text appears while you speak. When the browser has
 * none, or its speech service is unreachable, a recording can be transcribed by the
 * content service instead, but only if an administrator configured a recognition model.
 */

type Level = 'info' | 'warning' | 'error'

interface RecognitionResult { isFinal: boolean, 0: { transcript: string } }
interface RecognitionEvent { resultIndex: number, results: ArrayLike<RecognitionResult> }
interface Recognition {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((event: RecognitionEvent) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
  abort: () => void
}
type RecognitionConstructor = new () => Recognition

export interface VoiceEnvironment {
  recognition?: RecognitionConstructor
  getUserMedia?: (constraints: { audio: true }) => Promise<MediaStream>
  createRecorder?: (stream: MediaStream) => MediaRecorder
}

export interface VoiceOptions {
  /** Called with the text recognized so far (final + still-changing words). */
  onText: (text: string) => void
  /** Server transcription, only when the content service has a recognition model. */
  transcribe: () => ((recording: Blob) => Promise<string>) | undefined
  notify: (level: Level, message: string) => void
  maxSeconds?: number
  environment?: VoiceEnvironment
}

export function browserVoiceEnvironment(): VoiceEnvironment {
  const scope = globalThis as unknown as {
    SpeechRecognition?: RecognitionConstructor
    webkitSpeechRecognition?: RecognitionConstructor
    navigator?: Navigator
    MediaRecorder?: typeof MediaRecorder
  }
  const media = scope.navigator?.mediaDevices
  return {
    recognition: scope.SpeechRecognition ?? scope.webkitSpeechRecognition,
    getUserMedia: media?.getUserMedia ? constraints => media.getUserMedia(constraints) : undefined,
    createRecorder: scope.MediaRecorder ? stream => new scope.MediaRecorder!(stream) : undefined,
  }
}

export function useVoiceInput(options: VoiceOptions) {
  const environment = options.environment ?? browserVoiceEnvironment()
  const maxSeconds = options.maxSeconds ?? 60
  const state = ref<'idle' | 'listening' | 'recording' | 'transcribing'>('idle')
  const seconds = ref(0)
  let recognition: Recognition | undefined
  let recorder: MediaRecorder | undefined
  let stream: MediaStream | undefined
  let timer: ReturnType<typeof setInterval> | undefined
  let finalText = ''
  let switching = false
  let heard = false
  let reported = false

  const active = computed(() => state.value !== 'idle')
  const supported = computed(() => Boolean(environment.recognition
    || (environment.getUserMedia && environment.createRecorder && options.transcribe())))

  function startClock() {
    seconds.value = 0
    stopClock()
    timer = setInterval(() => {
      seconds.value += 1
      if (seconds.value >= maxSeconds) {
        stop()
      }
    }, 1000)
  }

  function stopClock() {
    if (timer) {
      clearInterval(timer)
      timer = undefined
    }
  }

  function finish() {
    stopClock()
    stream?.getTracks().forEach(track => track.stop())
    stream = undefined
    recognition = undefined
    recorder = undefined
    state.value = 'idle'
  }

  function startBrowser(Recognition: RecognitionConstructor) {
    const current = new Recognition()
    recognition = current
    finalText = ''
    heard = false
    reported = false
    current.lang = 'zh-CN'
    current.continuous = true
    current.interimResults = true
    current.onresult = (event) => {
      let interim = ''
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index]
        if (result.isFinal) {
          finalText += result[0].transcript
        }
        else {
          interim += result[0].transcript
        }
      }
      if ((finalText + interim).trim()) {
        heard = true
        options.onText(finalText + interim)
      }
    }
    current.onerror = (event) => {
      if (event.error === 'aborted') {
        return
      }
      if ((event.error === 'network' || event.error === 'service-not-allowed') && options.transcribe()) {
        // The browser's speech service is unreachable: record and transcribe on the server instead.
        switching = true
        options.notify('info', '浏览器语音服务连不上，已改用服务器识别，请继续说话')
        return
      }
      const messages: Record<string, string> = {
        'not-allowed': '无法使用麦克风：请在浏览器地址栏允许本页面使用麦克风',
        'service-not-allowed': '浏览器不允许使用语音识别，请改用 Edge 浏览器',
        'network': '浏览器的语音识别服务连不上，请改用 Edge 浏览器后重试',
        'no-speech': '没有听到声音，请靠近麦克风再试一次',
        'audio-capture': '没有找到可用的麦克风',
      }
      reported = true
      options.notify(event.error === 'no-speech' ? 'info' : 'warning', messages[event.error] ?? '语音识别中断，请重试')
    }
    current.onend = () => {
      if (recognition !== current) {
        return
      }
      if (switching) {
        switching = false
        recognition = undefined
        void startRecorder()
        return
      }
      if (!heard && !reported) {
        // Some browsers end silently when their speech service gives no text.
        options.notify('info', '没有识别到文字。如果一直这样，可能是浏览器的语音服务不可用，建议用 Edge 浏览器')
      }
      finish()
    }
    current.start()
    state.value = 'listening'
    startClock()
  }

  async function startRecorder() {
    const transcribe = options.transcribe()
    if (!environment.getUserMedia || !environment.createRecorder || !transcribe) {
      options.notify('warning', '当前浏览器不支持语音输入，请使用 Edge 或 Chrome')
      finish()
      return
    }
    try {
      stream = await environment.getUserMedia({ audio: true })
    }
    catch {
      options.notify('warning', '无法使用麦克风：请在浏览器地址栏允许本页面使用麦克风')
      finish()
      return
    }
    const chunks: Blob[] = []
    const current = environment.createRecorder(stream)
    recorder = current
    current.ondataavailable = event => event.data.size && chunks.push(event.data)
    current.onstop = async () => {
      stopClock()
      stream?.getTracks().forEach(track => track.stop())
      state.value = 'transcribing'
      try {
        const text = await transcribe(new Blob(chunks, { type: current.mimeType || 'audio/webm' }))
        if (text) {
          options.onText(text)
        }
        else {
          options.notify('info', '没有识别到语音，请靠近麦克风再试一次')
        }
      }
      catch (error) {
        options.notify('error', error instanceof Error ? error.message : '语音识别失败，请重试或直接输入文字')
      }
      finally {
        finish()
      }
    }
    current.start()
    state.value = 'recording'
    startClock()
  }

  function start() {
    if (active.value) {
      return
    }
    if (environment.recognition) {
      startBrowser(environment.recognition)
    }
    else {
      void startRecorder()
    }
  }

  /** Stop listening and keep what was recognized. */
  function stop() {
    if (state.value === 'listening') {
      recognition?.stop()
    }
    else if (state.value === 'recording' && recorder?.state === 'recording') {
      recorder.stop()
    }
  }

  /** Discard an unfinished session, e.g. when leaving the page. */
  function cancel() {
    switching = false
    recognition?.abort()
    if (recorder?.state === 'recording') {
      recorder.onstop = null
      recorder.stop()
    }
    finish()
  }

  return { state, seconds, active, supported, start, stop, cancel, toggle: () => (active.value ? stop() : start()) }
}
