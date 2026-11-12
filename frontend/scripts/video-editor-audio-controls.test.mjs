import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
import { audioPreviewState } from '../src/views/agent/content/video-editor/audioPreview.ts'
import { createPreviewAudioGainController } from '../src/views/agent/content/video-editor/previewAudioGain.ts'
import { createPreviewPlaybackController } from '../src/views/agent/content/video-editor/previewPlayback.ts'
import { videoPreviewTiming } from '../src/views/agent/content/video-editor/videoPreviewPresentation.ts'

const source = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/u)?.[1]
assert.ok(script)
const ast = ts.createSourceFile('VideoEditorWorkspace.ts', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const functionNames = [
  'activeItems',
  'setPreviewAudioElement',
  'syncAudioPreviews',
  'setPreviewVideoElement',
  'syncVideoPreview',
  'pausePlayback',
  'togglePlayback',
  'playbackTick',
]
const implementation = functionNames.map((name) => {
  const statement = ast.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === name)
  assert.ok(statement, `${name} must be extracted from the actual Workspace`)
  return statement.getText(ast)
}).join('\n')
const compiled = ts.transpileModule(implementation, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText

class MediaElement {
  duration = 6
  currentTime = 0
  volume = 1
  playbackRate = 1
  paused = true
  readyState = 4
  seeking = false
  pauseCalls = 0
  playCalls = 0

  pause() {
    this.paused = true
    this.pauseCalls += 1
  }

  async play() {
    this.paused = false
    this.playCalls += 1
  }
}

class AudioElement extends MediaElement {}
class VideoElement extends MediaElement {}

function audioItem(id, patch = {}) {
  return {
    id,
    start_seconds: 3,
    duration_seconds: 12,
    trim_in_seconds: 1,
    trim_out_seconds: 1,
    speed: 1,
    volume: 0.8,
    enabled: true,
    metadata: {},
    ...patch,
  }
}

function fixture() {
  const nodes = new Map()
  const context = {
    state: 'suspended',
    destination: { name: 'output' },
    createGain() {
      return {
        gain: { value: 1 },
        destination: null,
        connect(value) {
          this.destination = value
        },
        disconnect() {
          this.destination = null
        },
      }
    },
    createMediaElementSource(element) {
      assert.equal(nodes.has(element), false, '同一媒体元素只能创建一次 source')
      const node = {
        destination: null,
        connect(value) {
          this.destination = value
        },
        disconnect() {
          this.destination = null
        },
      }
      nodes.set(element, node)
      return node
    },
    async resume() {
      this.state = 'running'
    },
    async suspend() {
      this.state = 'suspended'
    },
  }
  const warnings = []
  const gain = createPreviewAudioGainController({ createContext: () => context, onWarning: text => warnings.push(text) })
  const voice = audioItem('voice', { volume: 1.8 })
  const music = audioItem('music', { volume: 0.4, metadata: { loop: true } })
  const scope = {
    timeline: { value: { canvas: { fps: 25 }, tracks: [
      { id: 'voice-track', kind: 'voice', items: [voice] },
      { id: 'music-track', kind: 'music', items: [music] },
    ] } },
    playhead: { value: 4 },
    duration: { value: 15 },
    playing: { value: false },
    previewAudioElements: new Map(),
    previewVideoElements: new Map(),
    previewGain: gain,
    previewPlayback: createPreviewPlaybackController(),
    playbackRevision: 0,
    currentVisualLayers: { value: [] },
    playbackTimestamp: 0,
    playbackFrame: 0,
    requestAnimationFrame: () => 10,
    cancelAnimationFrame: () => undefined,
    playbackTick: () => undefined,
    nextTick: () => Promise.resolve(),
    ensureItemPreview: () => Promise.resolve(),
    findTrack: (value, kind) => value.tracks.find(track => track.kind === kind),
    HTMLAudioElement: AudioElement,
    HTMLVideoElement: VideoElement,
    audioPreviewState,
    videoPreviewTiming,
    ElMessage: { warning: text => warnings.push(text) },
  }
  scope.currentAudio = { get value() {
    return ['voice', 'music'].flatMap(kind => scope.activeItems(kind))
  } }
  vm.createContext(scope)
  vm.runInContext(compiled, scope)
  const first = new AudioElement()
  const second = new AudioElement()
  scope.setPreviewAudioElement('voice', first)
  scope.setPreviewAudioElement('music', second)
  return {
    scope,
    context,
    nodes,
    voice,
    music,
    first,
    second,
    close: () => {
      scope.pausePlayback()
      gain.dispose()
    },
  }
}

const tests = []
const test = (name, callback) => tests.push({ name, callback })

test('真实播放入口恢复 AudioContext，两路音轨独立混音，180% 不再被截成 100%', async (f) => {
  await f.scope.togglePlayback()
  assert.equal(f.context.state, 'running')
  assert.equal(f.first.paused, false)
  assert.equal(f.second.paused, false)
  assert.equal(f.nodes.size, 2)
  assert.equal(f.nodes.get(f.first).destination.gain.value, 1.8)
  assert.equal(f.nodes.get(f.second).destination.gain.value, 0.4)
  assert.equal(f.first.volume, 1)
  assert.equal(f.second.volume, 1)
  assert.equal(f.nodes.get(f.first).destination.destination, f.context.destination)
  assert.equal(f.nodes.get(f.second).destination.destination, f.context.destination)
})

test('裁出后配音真正停止，循环背景音仍回到裁入点播放', (f) => {
  f.scope.playing.value = true
  f.scope.playhead.value = 7
  f.first.currentTime = 5.1
  f.first.paused = false
  f.second.currentTime = 5.1
  f.second.paused = false
  f.scope.syncAudioPreviews()
  assert.equal(f.first.currentTime, 5)
  assert.equal(f.first.paused, true)
  assert.equal(f.nodes.get(f.first).destination.gain.value, 0)
  assert.equal(f.second.currentTime, 1)
  assert.equal(f.second.paused, false)
  f.scope.playhead.value = 15
  f.scope.syncAudioPreviews()
  assert.equal(f.second.paused, true)
})

test('暂停时微调裁入、语速、音量、淡入立即同步；再次播放保持新参数', async (f) => {
  f.scope.togglePlayback()
  f.scope.pausePlayback()
  f.voice.trim_in_seconds = 1.1
  f.voice.speed = 2
  f.voice.volume = 2
  f.voice.metadata.fade_in_seconds = 2
  f.scope.syncAudioPreviews()
  assert.equal(f.first.paused, true)
  assert.equal(f.first.currentTime, 3.1)
  assert.equal(f.first.playbackRate, 2)
  assert.equal(f.nodes.get(f.first).destination.gain.value, 1)
  f.voice.metadata.fade_in_seconds = 0
  f.scope.syncAudioPreviews()
  assert.equal(f.nodes.get(f.first).destination.gain.value, 2)
  await f.scope.togglePlayback()
  assert.equal(f.first.paused, false)
  assert.equal(f.first.playbackRate, 2)
  assert.equal(f.nodes.get(f.first).destination.gain.value, 2)
})

test('选中音轨播放器替换释放旧图，不打断另一音轨，重复 ref 不重复绑定', (f) => {
  f.scope.playing.value = true
  f.scope.syncAudioPreviews()
  const replacement = new AudioElement()
  f.scope.setPreviewAudioElement('voice', replacement)
  assert.equal(f.first.paused, true)
  assert.equal(f.nodes.get(f.first).destination, null)
  assert.notEqual(f.nodes.get(f.second).destination, null)
  f.scope.syncAudioPreviews()
  assert.equal(replacement.paused, false)
  f.scope.setPreviewAudioElement('voice', replacement)
  f.scope.syncAudioPreviews()
  assert.equal(f.nodes.size, 3)
  f.scope.setPreviewAudioElement('voice', null)
  assert.equal(replacement.paused, true)
  assert.equal(f.nodes.get(replacement).destination, null)
  assert.equal(f.scope.previewAudioElements.has('voice'), false)
})

test('主视频使用同一增益控制器，禁用源声/尾帧保持不会继续出声', async (f) => {
  const item = audioItem('video', { start_seconds: 0, duration_seconds: 12, volume: 2 })
  const video = new VideoElement()
  f.scope.currentVisualLayers.value = [{ item, opacity: 1 }]
  f.scope.setPreviewVideoElement(item.id, video)
  f.scope.playing.value = true
  f.scope.playhead.value = 1
  await f.scope.syncVideoPreview()
  assert.equal(f.nodes.get(video).destination.gain.value, 2)
  assert.equal(video.volume, 1)
  assert.equal(video.paused, false)
  item.metadata.source_audio = 'mute'
  await f.scope.syncVideoPreview()
  assert.equal(f.nodes.get(video).destination.gain.value, 0)
  item.metadata.source_audio = 'keep'
  f.scope.playhead.value = 10
  await f.scope.syncVideoPreview()
  assert.equal(video.paused, true)
  assert.equal(f.nodes.get(video).destination.gain.value, 0)
  f.scope.setPreviewVideoElement(item.id, null)
  assert.equal(f.nodes.get(video).destination, null)
})

test('主视频倍速使用浏览器原生速度，不靠逐帧跳转追赶时间线', async (f) => {
  const item = audioItem('fast-video', { start_seconds: 0, duration_seconds: 2, speed: 2 })
  const video = new VideoElement()
  f.scope.currentVisualLayers.value = [{ item, opacity: 1 }]
  f.scope.setPreviewVideoElement(item.id, video)
  f.scope.playing.value = true
  f.scope.playhead.value = 0.5
  await f.scope.syncVideoPreview()
  assert.equal(video.playbackRate, 2)
  assert.equal(video.currentTime, 2)
  video.currentTime = 2.1
  f.scope.playhead.value = 0.65
  await f.scope.syncVideoPreview()
  assert.equal(video.currentTime, 2.1, '正常解码延迟不应触发 seek')
})

test('主线程掉帧后时间线按真实经过时间推进，不把连续声音往回拉', (f) => {
  f.scope.playing.value = true
  f.scope.playhead.value = 0
  f.scope.playbackTimestamp = 1000
  f.scope.playbackTick(2000)
  assert.equal(f.scope.playhead.value, 1)
})

test('真实模板与生命周期：波形/隐藏音频 CORS 一致，参数监听和卸载清理已接线', () => {
  const waveform = readFileSync(new URL('../src/views/agent/content/video-editor/AudioWaveform.vue', import.meta.url), 'utf8')
  assert.match(waveform, /<audio[^>]*crossorigin="anonymous"/u)
  assert.match(source, /class="preview-audio-element"\s+crossorigin="anonymous"/u)
  assert.match(source, /watch\(timeline,[\s\S]*?nextTick\(syncAudioPreviews\)[\s\S]*?deep: true/u)
  assert.match(source, /pausePlayback\(\)\s+previewGain\.dispose\(\)/u)
  assert.doesNotMatch(waveform, /audioElement\.value\.volume\s*=/u)
})

for (const { name, callback } of tests) {
  const f = fixture()
  try {
    await callback(f)
    console.info(`PASS ${name}`)
  }
  finally {
    f.close()
  }
}
console.info(`Video editor audio controls: ${tests.length} tests passed.`)
