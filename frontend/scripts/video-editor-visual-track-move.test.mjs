import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import process from 'node:process'

import {
  addItem,
  createTimeline,
  findTrack,
  moveVisualItemToTrack,
} from '../src/views/agent/content/video-editor/timelineState.ts'

const componentSource = readFileSync(
  new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url),
  'utf8',
)
const failures = []

function test(name, callback) {
  try {
    callback()
    console.info(`PASS ${name}`)
  }
  catch (error) {
    failures.push({ name, error })
    console.error(`FAIL ${name}`)
    console.error(error)
  }
}

function visual(id, duration, mediaKind = 'image', startSeconds = 0) {
  return {
    id,
    label: id,
    start_seconds: startSeconds,
    duration_seconds: duration,
    metadata: { media_kind: mediaKind },
  }
}

test('主画面图片移入叠加轨时保留绝对时间并压紧主画面', () => {
  let timeline = addItem(createTimeline(), 'video', visual('a', 2))
  timeline = addItem(timeline, 'video', visual('b', 3))
  timeline = addItem(timeline, 'video', visual('c', 4))

  const moved = moveVisualItemToTrack(timeline, 'b', 'overlay')

  assert.deepEqual(findTrack(moved, 'video').items.map(item => [item.id, item.start_seconds]), [
    ['a', 0],
    ['c', 2],
  ])
  assert.deepEqual(findTrack(moved, 'overlay').items.map(item => [item.id, item.start_seconds]), [
    ['b', 2],
  ])
})

test('叠加图片按时间位置移回主画面并保持连续排序', () => {
  let timeline = addItem(createTimeline(), 'video', visual('a', 2))
  timeline = addItem(timeline, 'video', visual('c', 4))
  timeline = addItem(timeline, 'overlay', visual('b', 3, 'image', 2))

  const moved = moveVisualItemToTrack(timeline, 'b', 'video')

  assert.deepEqual(findTrack(moved, 'video').items.map(item => [item.id, item.start_seconds]), [
    ['a', 0],
    ['b', 2],
    ['c', 5],
  ])
  assert.deepEqual(findTrack(moved, 'overlay').items, [])
})

test('视频和锁定轨道不能移入叠加轨', () => {
  let timeline = addItem(createTimeline(), 'video', visual('video', 5, 'video'))
  assert.equal(moveVisualItemToTrack(timeline, 'video', 'overlay'), timeline)

  timeline = addItem(timeline, 'video', visual('image', 3))
  findTrack(timeline, 'overlay').locked = true
  assert.equal(moveVisualItemToTrack(timeline, 'image', 'overlay'), timeline)
})

test('暂时隐藏叠加轨道及移入入口，但旧工程叠加层继续预览', () => {
  assert.doesNotMatch(componentSource, /data-testid="move-selected-visual-track"/u)
  assert.doesNotMatch(componentSource, /<ElOption[^>]+value="(?:overlay|logo)"/u)
  assert.match(componentSource, /tracks\.filter\(track => track\.kind !== 'sfx' && track\.kind !== 'overlay'\)/u)
  assert.match(componentSource, /currentOverlays = computed\(\(\) => activeItems\('overlay'\)\)/u)
  assert.match(componentSource, /v-for="item in currentOverlays"/u)
})

if (failures.length) {
  process.exitCode = 1
}
else {
  console.info('Video editor visual track move tests passed: 4 checks passed.')
}
