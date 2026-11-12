import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { audioPreviewState } from '../src/views/agent/content/video-editor/audioPreview.ts'
import { createPreviewAudioGainController } from '../src/views/agent/content/video-editor/previewAudioGain.ts'

const base = {
  playhead: 3,
  clipStart: 3,
  clipDuration: 12,
  trimIn: 1,
  trimOut: 1,
  speed: 1,
  volume: 0.8,
  sourceDuration: 6,
  loop: false,
  fadeIn: 0,
  fadeOut: 0,
}

const tests = []
const test = (name, callback) => tests.push({ name, callback })

test('开始/片段结束/裁出终点均有明确停止状态', () => {
  assert.equal(audioPreviewState({ ...base, playhead: 2.9 }).shouldPlay, false)
  assert.equal(audioPreviewState(base).shouldPlay, true)
  const beforeEnd = audioPreviewState({ ...base, playhead: 6.9 })
  assert.equal(beforeEnd.shouldPlay, true)
  assert.ok(beforeEnd.sourceTime < 5)
  for (const playhead of [7, 9, 15]) {
    const state = audioPreviewState({ ...base, playhead })
    assert.equal(state.shouldPlay, false)
    assert.equal(state.sourceTime, 5)
    assert.equal(state.volume, 0)
  }
})

test('循环只在裁入到裁出之间，片段终点仍停止', () => {
  for (const playhead of [3, 6.99, 7, 7.1, 11, 14.99]) {
    const state = audioPreviewState({ ...base, playhead, loop: true })
    assert.equal(state.shouldPlay, true)
    assert.ok(state.sourceTime >= 1 && state.sourceTime < 5)
  }
  assert.equal(audioPreviewState({ ...base, playhead: 7, loop: true }).sourceTime, 1)
  assert.equal(audioPreviewState({ ...base, playhead: 15, loop: true }).shouldPlay, false)
})

test('无效裁切/未知源时长不能播放或产生非法定位', () => {
  for (const patch of [
    { trimIn: 4, trimOut: 4 },
    { trimIn: 10 },
    { sourceDuration: 0 },
    { sourceDuration: Number.NaN },
    { clipDuration: 0 },
    { playhead: Number.NaN },
    { clipDuration: Number.POSITIVE_INFINITY },
  ]) {
    const state = audioPreviewState({ ...base, ...patch, loop: true })
    assert.equal(state.shouldPlay, false)
    assert.ok(Number.isFinite(state.sourceTime))
    assert.ok(state.sourceTime >= 0 && state.sourceTime <= 6)
  }
})

test('语速、裁入以及淡入淡出共同生效，保留 200% 增益', () => {
  const accelerated = audioPreviewState({ ...base, playhead: 4, speed: 2 })
  assert.equal(accelerated.playbackRate, 2)
  assert.equal(accelerated.sourceTime, 3)
  assert.equal(audioPreviewState({ ...base, playhead: 5, speed: 2 }).shouldPlay, false)
  assert.equal(audioPreviewState({ ...base, playhead: 4, fadeIn: 2 }).volume, 0.4)
  assert.equal(audioPreviewState({ ...base, playhead: 6, fadeOut: 2 }).volume, 0.4)
  assert.equal(audioPreviewState({ ...base, playhead: 14, loop: true, fadeOut: 2 }).volume, 0.4)
  assert.equal(audioPreviewState({ ...base, volume: 2 }).volume, 2)
  assert.equal(audioPreviewState({ ...base, playhead: 4, volume: 2, fadeIn: 2 }).volume, 1)
})

function mockContext() {
  const sources = []
  const gains = []
  const seenElements = new WeakSet()
  const context = {
    state: 'suspended',
    destination: { name: 'speakers' },
    resumeCalls: 0,
    suspendCalls: 0,
    createGain() {
      const gain = {
        gain: { value: 1 },
        connections: [],
        connect(destination) {
          this.connections.push(destination)
        },
        disconnect() {
          this.connections = []
        },
      }
      gains.push(gain)
      return gain
    },
    createMediaElementSource(element) {
      assert.equal(seenElements.has(element), false, '同一 HTMLMediaElement 禁止重复创建 source')
      seenElements.add(element)
      const source = {
        element,
        connections: [],
        connect(destination) {
          this.connections.push(destination)
        },
        disconnect() {
          this.connections = []
        },
      }
      sources.push(source)
      return source
    },
    async resume() {
      this.resumeCalls += 1
      this.state = 'running'
    },
    async suspend() {
      this.suspendCalls += 1
      this.state = 'suspended'
    },
  }
  return { context, sources, gains }
}

test('GainNode 实际处理 0–200%，原生音量保持 1，重复应用不重复创建 source', async () => {
  const fixture = mockContext()
  const element = { volume: 0.5 }
  const controller = createPreviewAudioGainController({ createContext: () => fixture.context })
  for (const gain of [0, 0.5, 1, 1.5, 2]) {
    assert.deepEqual(controller.apply(element, gain), { mode: 'web-audio', gain, limited: false })
    assert.equal(fixture.gains[0].gain.value, gain)
    assert.equal(element.volume, 1)
  }
  assert.equal(fixture.sources.length, 1)
  assert.equal(fixture.sources[0].connections[0], fixture.gains[0])
  assert.equal(fixture.gains[0].connections[0], fixture.context.destination)
  assert.equal(await controller.resume(), true)
  assert.equal(fixture.context.state, 'running')
  controller.dispose()
  assert.deepEqual(fixture.sources[0].connections, [])
  assert.deepEqual(fixture.gains[0].connections, [])
  assert.equal(fixture.context.suspendCalls, 1)
})

test('元素移除/组件销毁后复用既有 source，多个控件共享时不提前断开', () => {
  const fixture = mockContext()
  const element = { volume: 1 }
  const first = createPreviewAudioGainController({ createContext: () => fixture.context })
  const second = createPreviewAudioGainController({ createContext: () => {
    throw new Error('Should reuse existing binding')
  } })
  first.apply(element, 1.2)
  second.apply(element, 1.5)
  first.dispose()
  assert.equal(fixture.sources[0].connections.length, 1)
  assert.equal(fixture.context.suspendCalls, 0)
  second.release(element)
  assert.equal(fixture.sources[0].connections.length, 0)
  second.apply(element, 2)
  assert.equal(fixture.sources.length, 1)
  assert.equal(fixture.gains[0].gain.value, 2)
  second.dispose()
  second.dispose()
  const third = createPreviewAudioGainController()
  third.apply(element, 0.8)
  assert.equal(fixture.sources.length, 1)
  assert.equal(fixture.gains[0].gain.value, 0.8)
  third.dispose()
})

test('播放手势先发生时，稍后加入的播放器也会恢复 AudioContext', async () => {
  const fixture = mockContext()
  const controller = createPreviewAudioGainController({ createContext: () => fixture.context })
  assert.equal(await controller.resume(), true)
  controller.apply({ volume: 1 }, 1.5)
  await Promise.resolve()
  assert.equal(fixture.context.state, 'running')
  controller.dispose()
})

test('浏览器不支持或创建失败时原生预览最高 100%，中文提示不反复弹', () => {
  for (const createContext of [() => undefined, () => {
    throw new Error('Not supported')
  }]) {
    const warnings = []
    const controller = createPreviewAudioGainController({ createContext, onWarning: text => warnings.push(text) })
    const element = { volume: 1 }
    assert.deepEqual(controller.apply(element, 0.5), { mode: 'native', gain: 0.5, limited: false })
    assert.deepEqual(controller.apply(element, 2), { mode: 'native', gain: 1, limited: true })
    controller.apply(element, 1.5)
    assert.equal(element.volume, 1)
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /不支持音量增强.*最高为 100%/u)
    controller.dispose()
  }
})

test('播放许可被阻止时明确提示，后续可继续点击恢复', async () => {
  const fixture = mockContext()
  const warnings = []
  let blocked = true
  fixture.context.resume = async () => {
    if (blocked) {
      throw new Error('NotAllowedError')
    }
    fixture.context.state = 'running'
  }
  const controller = createPreviewAudioGainController({ createContext: () => fixture.context, onWarning: text => warnings.push(text) })
  controller.apply({ volume: 1 }, 1)
  assert.equal(await controller.resume(), false)
  assert.equal(warnings.length, 1)
  assert.match(warnings[0], /再次点击播放按钮/u)
  blocked = false
  assert.equal(await controller.resume(), true)
  controller.dispose()
})

test('增益节点连接失败仍保留直通声音，释放后重用不重复创建 source', () => {
  const fixture = mockContext()
  const createGain = fixture.context.createGain
  fixture.context.createGain = () => {
    const gain = createGain()
    gain.connect = () => {
      throw new Error('Gain connection unavailable')
    }
    return gain
  }
  const controller = createPreviewAudioGainController({ createContext: () => fixture.context })
  const element = { volume: 1 }
  assert.deepEqual(controller.apply(element, 2), { mode: 'native', gain: 1, limited: true })
  assert.equal(fixture.sources[0].connections[0], fixture.context.destination)
  controller.release(element)
  assert.deepEqual(fixture.sources[0].connections, [])
  controller.apply(element, 0.5)
  assert.equal(fixture.sources[0].connections[0], fixture.context.destination)
  assert.equal(element.volume, 0.5)
  assert.equal(fixture.sources.length, 1)
  controller.dispose()
})

test('波形不覆盖统一音量或播放位置，由主播放器统一同步', () => {
  const source = readFileSync(new URL('../src/views/agent/content/video-editor/AudioWaveform.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /audioElement\.value\.volume\s*=/u)
  assert.doesNotMatch(source, /\.setTime\(|\.seekTo\(/u)
  assert.match(source, /media: audioElement\.value/u)
})

for (const { name, callback } of tests) {
  await callback()
  console.info(`PASS ${name}`)
}
console.info(`Video audio preview: ${tests.length} tests passed.`)
