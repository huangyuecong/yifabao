import assert from 'node:assert/strict'
import { previewFps } from '../src/views/agent/content/video-editor/mediaJobState.ts'
import {
  addItem,
  createTimeline,
  findItem,
  findTrack,
  normalizeItem,
  normalizeTimeline,
  splitItem,
  timelineDuration,
  trimPatch,
  updateItem,
} from '../src/views/agent/content/video-editor/timelineState.ts'
import {
  overlayPreviewStyle,
  subtitlePreviewStyle,
  videoPreviewTiming,
  visualFitMode,
  visualLayers,
} from '../src/views/agent/content/video-editor/videoPreviewPresentation.ts'

const tests = []
const test = (name, run) => tests.push({ name, run })
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 0.00001, `${actual} != ${expected}`)
const video = (id, duration = 10, metadata = {}) => normalizeItem({
  id,
  duration_seconds: duration,
  metadata: { media_kind: 'video', source_duration_seconds: duration, ...metadata },
}, 'video')

test('入点/出点裁切同步缩短时长并更新后续主画面位置', () => {
  let timeline = addItem(createTimeline(), 'video', video('a'))
  timeline = addItem(timeline, 'video', video('b', 5))
  timeline = updateItem(timeline, 'a', { trim_in_seconds: 2 })
  assert.equal(findItem(timeline, 'a').item.duration_seconds, 8)
  assert.equal(findItem(timeline, 'b').item.start_seconds, 8)
  timeline = updateItem(timeline, 'a', { trim_out_seconds: 3 })
  assert.equal(findItem(timeline, 'a').item.duration_seconds, 5)
  assert.equal(findItem(timeline, 'b').item.start_seconds, 5)
})

test('已知源时长限制裁切和长度，未知源长度不阻止自由设置时长', () => {
  const item = video('a', 5)
  const trimmed = trimPatch(item, { trim_in_seconds: 100 }, 5, 'video')
  close(trimmed.trim_in_seconds, 4.9)
  close(trimmed.duration_seconds, 0.1)
  assert.equal(trimPatch(item, { duration_seconds: 99 }, 5, 'video').duration_seconds, 5)
  const unknown = { ...item, metadata: { media_kind: 'audio' } }
  assert.equal(trimPatch(unknown, { duration_seconds: 14.7 }, undefined, 'music').duration_seconds, 14.7)
})

test('裁切按语速换算，已经包含新时长的拖拽操作不会重复扣减', () => {
  const item = { ...video('a', 10), duration_seconds: 5, speed: 2 }
  assert.equal(trimPatch(item, { trim_in_seconds: 2 }, 10, 'voice').duration_seconds, 4)
  assert.equal(trimPatch(item, { trim_in_seconds: 2, duration_seconds: 4 }, 10, 'voice').duration_seconds, 4)
  const split = splitItem(addItem(createTimeline(), 'voice', item), 'a', 2, 'b')
  assert.equal(findItem(split, 'a').item.trim_out_seconds, 6)
  assert.equal(findItem(split, 'b').item.trim_in_seconds, 4)
})

test('图片与字幕忽略旧裁切数据，拆分图片不生成无意义裁切', () => {
  const image = normalizeItem({ id: 'image', duration_seconds: 5, trim_in_seconds: 2, trim_out_seconds: 1, metadata: { media_kind: 'image' } }, 'video')
  assert.equal(image.trim_in_seconds, 0)
  assert.equal(image.trim_out_seconds, 0)
  assert.deepEqual(trimPatch(image, { trim_in_seconds: 3 }, undefined, 'video'), { trim_in_seconds: 0, trim_out_seconds: 0 })
  const subtitle = normalizeItem({ id: 'subtitle', trim_in_seconds: 2, trim_out_seconds: 3 }, 'subtitle')
  assert.equal(subtitle.trim_in_seconds, 0)
  assert.equal(subtitle.trim_out_seconds, 0)
  const split = splitItem(addItem(createTimeline(), 'video', image), 'image', 2, 'right')
  assert.equal(findItem(split, 'image').item.trim_out_seconds, 0)
  assert.equal(findItem(split, 'right').item.trim_in_seconds, 0)
})

test('进入片段的转场扣除重叠，与服务端相同地限制为相邻片段一半', () => {
  let timeline = addItem(createTimeline(), 'video', video('a', 5))
  timeline = addItem(timeline, 'video', video('b', 5, { transition: 'fade', transition_duration_seconds: 1 }))
  timeline = addItem(timeline, 'video', video('c', 3, { transition: 'fade', transition_duration_seconds: 5 }))
  assert.equal(findItem(timeline, 'b').item.start_seconds, 4)
  assert.equal(findItem(timeline, 'c').item.start_seconds, 7.5)
  assert.equal(timelineDuration(timeline), 10.5)
  const restored = normalizeTimeline(timeline)
  assert.equal(findItem(restored, 'c').item.start_seconds, 7.5)
})

test('禁用主片不占时间，静音轨道不延长有效成片时长', () => {
  let timeline = addItem(createTimeline(), 'video', video('a', 5))
  timeline = addItem(timeline, 'video', { ...video('disabled', 20), enabled: false })
  timeline = addItem(timeline, 'video', video('b', 5))
  assert.equal(findItem(timeline, 'b').item.start_seconds, 5)
  timeline = addItem(timeline, 'voice', { id: 'voice', duration_seconds: 14.7 })
  assert.equal(timelineDuration(timeline), 14.7)
  findTrack(timeline, 'voice').muted = true
  assert.equal(timelineDuration(timeline), 10)
})

test('淡入淡出实时计算两幅画面和对应源时间', () => {
  let timeline = addItem(createTimeline(), 'video', video('a', 5))
  timeline = addItem(timeline, 'video', video('b', 5, { transition: 'fade', transition_duration_seconds: 1 }))
  const layers = visualLayers(timeline, 4.5)
  assert.equal(layers.length, 2)
  assert.equal(layers[0].item.id, 'a')
  assert.equal(layers[0].opacity, 1)
  assert.equal(layers[1].item.id, 'b')
  assert.equal(layers[1].opacity, 0.5)
  assert.equal(layers[1].sourceTime, 0.5)
})

test('淡入黑场和白场以相应背景过渡', () => {
  for (const [transition, color] of [['fadeblack', '#000000'], ['fadewhite', '#FFFFFF']]) {
    let timeline = addItem(createTimeline(), 'video', video('a', 5))
    timeline = addItem(timeline, 'video', video('b', 5, { transition, transition_duration_seconds: 1 }))
    const layers = visualLayers(timeline, 4.5)
    assert.deepEqual(layers.map(layer => layer.opacity), [0, 0])
    assert.deepEqual(layers.map(layer => layer.backgroundColor), [color, color])
  }
})

test('5 秒视频搭配 14.7 秒配音保留完整长度，末视频冻结最后一帧', () => {
  let timeline = addItem(createTimeline(), 'video', video('a', 5))
  timeline = addItem(timeline, 'voice', { id: 'voice', duration_seconds: 14.7 })
  assert.equal(timelineDuration(timeline), 14.7)
  const [layer] = visualLayers(timeline, 12)
  assert.equal(layer.hold, true)
  close(layer.sourceTime, 4.96)
  const timing = videoPreviewTiming({ ...video('clip', 30), duration_seconds: 5, trim_in_seconds: 2 }, 20, 30, 25)
  assert.equal(timing.hold, true)
  close(timing.sourceTime, 6.96)
  findTrack(timeline, 'video').muted = true
  assert.deepEqual(visualLayers(timeline, 2), [])
})

test('视频源末尾与出点共同限制预览时间，精细入点不丢失', () => {
  const item = { ...video('a', 10), trim_in_seconds: 2.1, trim_out_seconds: 3 }
  close(videoPreviewTiming(item, 0, 10).sourceTime, 2.1)
  const end = videoPreviewTiming(item, 8, 10)
  assert.equal(end.hold, true)
  close(end.sourceTime, 6.96)
})

test('字幕字号、颜色和描边按画布等比缩放，支持每条字幕独立样式', () => {
  const item = normalizeItem({ metadata: { subtitle_style: { font_size: 50, primary_color: '#FF8800', outline_color: '#003366', outline_width: 3 } } }, 'subtitle')
  const style = subtitlePreviewStyle(item, createTimeline().canvas, 540)
  assert.equal(style.fontSize, '25px')
  assert.equal(style.color, '#FF8800')
  assert.equal(style.WebkitTextStroke, '1.5px #003366')
  assert.equal(style.bottom, '18px')
  assert.equal(style.textAlign, 'center')
  assert.equal(subtitlePreviewStyle(normalizeItem({}, 'subtitle'), createTimeline().canvas, 540).fontSize, '14px')
})

test('叠加按画布基准 160px、中心位置和缩放透明度计算，fit 正确区分', () => {
  const item = normalizeItem({ metadata: { media_kind: 'image', fit_mode: 'cover' }, transform: { x: 25, y: 75, scale: 2, opacity: 0.3 } }, 'overlay')
  const style = overlayPreviewStyle(item, createTimeline().canvas, 540)
  assert.equal(style.width, '160px')
  assert.equal(style.left, '25%')
  assert.equal(style.top, '75%')
  assert.equal(style.opacity, 0.3)
  assert.equal(style.transform, 'translate(-50%, -50%)')
  assert.equal(style.objectFit, 'cover')
  assert.equal(visualFitMode(item), 'cover')
  assert.equal(visualFitMode(video('a')), 'contain')
})

test('选择的 1–60 帧率原样用于精确预览和导出，不再强制 25/30', () => {
  for (const fps of [1, 24, 25, 26, 30, 50, 60]) {
    assert.equal(previewFps(fps), fps)
  }
  assert.equal(previewFps(Number.NaN), 25)
  assert.equal(previewFps(0), 1)
  assert.equal(previewFps(120), 60)
})

for (const { name, run } of tests) {
  run()
  console.info(`PASS ${name}`)
}
console.info(`Video preview presentation: ${tests.length} tests passed.`)
