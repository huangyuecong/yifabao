import assert from 'node:assert/strict'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import { useVoiceInput } from '../src/views/agent/content/voiceInput.ts'

class FakeRecognition {
  static instances = []
  constructor() {
    FakeRecognition.instances.push(this)
    this.started = false
    this.stopped = false
  }

  start() {
    this.started = true
  }

  stop() {
    this.stopped = true
    this.onend?.()
  }

  abort() {
    this.onend?.()
  }

  say(...parts) {
    // parts: [text, isFinal] pairs, delivered as one result event
    const results = parts.map(([text, isFinal]) => ({ isFinal, 0: { transcript: text } }))
    this.onresult({ resultIndex: 0, results })
  }
}

class FakeRecorder {
  constructor() {
    this.state = 'inactive'
    this.mimeType = 'audio/webm'
  }

  start() {
    this.state = 'recording'
  }

  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['voice'], { type: 'audio/webm' }) })
    return this.onstop?.()
  }
}

function setup(options = {}) {
  // An explicit `recognition: undefined` means a browser without speech recognition.
  const recognition = 'recognition' in options ? options.recognition : FakeRecognition
  const { transcribe, microphone = true } = options
  FakeRecognition.instances = []
  const texts = []
  const notices = []
  const recorders = []
  const voice = useVoiceInput({
    onText: text => texts.push(text),
    transcribe: () => transcribe,
    notify: (level, message) => notices.push([level, message]),
    environment: {
      recognition,
      getUserMedia: async () => {
        if (!microphone) {
          throw new Error('denied')
        }
        return { getTracks: () => [{ stop() {} }] }
      },
      createRecorder: () => {
        const recorder = new FakeRecorder()
        recorders.push(recorder)
        return recorder
      },
    },
  })
  return { voice, texts, notices, recorders }
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0))

test('browser recognition fills the box live and stops on the second click', () => {
  const { voice, texts } = setup()
  voice.toggle()
  const recognition = FakeRecognition.instances[0]
  assert.equal(voice.state.value, 'listening')
  assert.equal(recognition.lang, 'zh-CN')
  assert.ok(recognition.continuous && recognition.interimResults && recognition.started)
  recognition.say(['查看待审核', false])
  recognition.say(['查看待审核的内容。', true], ['再看', false])
  assert.deepEqual(texts, ['查看待审核', '查看待审核的内容。再看'])
  voice.toggle()
  assert.ok(recognition.stopped)
  assert.equal(voice.state.value, 'idle')
  voice.cancel()
})

test('a session that ends without any text says so instead of staying silent', () => {
  const { voice, texts, notices } = setup()
  voice.start()
  const recognition = FakeRecognition.instances[0]
  recognition.say(['', true])
  voice.stop()
  assert.deepEqual(texts, [], 'an empty result does not clear what was typed')
  assert.match(notices[0][1], /没有识别到文字/u)
})

test('an unreachable browser service switches to server transcription when it is configured', async () => {
  const sent = []
  const { voice, texts, notices, recorders } = setup({
    transcribe: async (recording) => {
      sent.push(recording)
      return '服务器识别的文字'
    },
  })
  voice.start()
  const recognition = FakeRecognition.instances[0]
  recognition.onerror({ error: 'network' })
  recognition.onend()
  await settle()
  assert.equal(voice.state.value, 'recording')
  assert.equal(notices[0][0], 'info')
  voice.stop()
  await settle()
  assert.equal(sent.length, 1)
  assert.deepEqual(texts, ['服务器识别的文字'])
  assert.equal(voice.state.value, 'idle')
  assert.equal(recorders.length, 1)
})

test('without server transcription a blocked browser service explains what to do', () => {
  const { voice, notices } = setup()
  voice.start()
  const recognition = FakeRecognition.instances[0]
  recognition.onerror({ error: 'network' })
  recognition.onend()
  assert.equal(voice.state.value, 'idle')
  assert.match(notices[0][1], /Edge/u)

  voice.start()
  FakeRecognition.instances[1].onerror({ error: 'not-allowed' })
  assert.match(notices[1][1], /允许本页面使用麦克风/u)
  FakeRecognition.instances[1].onerror({ error: 'aborted' })
  assert.equal(notices.length, 2, 'a deliberate stop is not an error')
  voice.cancel()
})

test('a browser without recognition records for the server, or reports that voice input is unavailable', async () => {
  const unsupported = setup({ recognition: undefined })
  assert.equal(unsupported.voice.supported.value, false)
  unsupported.voice.start()
  await settle()
  assert.match(unsupported.notices[0][1], /不支持语音输入/u)

  const empty = setup({ recognition: undefined, transcribe: async () => '' })
  assert.equal(empty.voice.supported.value, true)
  empty.voice.start()
  await settle()
  empty.voice.stop()
  await settle()
  assert.deepEqual(empty.texts, [])
  assert.match(empty.notices[0][1], /没有识别到语音/u)

  const denied = setup({ recognition: undefined, transcribe: async () => 'x', microphone: false })
  denied.voice.start()
  await settle()
  assert.equal(denied.voice.state.value, 'idle')
  assert.match(denied.notices[0][1], /麦克风/u)
})

test('cancel discards an unfinished recording without transcribing it', async () => {
  const sent = []
  const { voice } = setup({ recognition: undefined, transcribe: async (recording) => {
    sent.push(recording)
    return '不应出现'
  } })
  voice.start()
  await settle()
  voice.cancel()
  await settle()
  assert.deepEqual(sent, [])
  assert.equal(voice.state.value, 'idle')
})
