/* eslint-disable financial/no-division, financial/no-float-calculation */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import { createPreviewPlaybackController } from '../src/views/agent/content/video-editor/previewPlayback.ts'

function fixture() {
  let clock = 0
  let position = 0
  let rate = 1
  const seeks = []
  const rates = []
  const element = {
    readyState: 4,
    seeking: false,
    paused: true,
    get currentTime() {
      return position
    },
    set currentTime(value) {
      position = value
      seeks.push(value)
    },
    get playbackRate() {
      return rate
    },
    set playbackRate(value) {
      rate = value
      rates.push(value)
    },
    playCalls: 0,
    play() {
      this.paused = false
      this.playCalls += 1
      return Promise.resolve()
    },
    pause() {
      this.paused = true
    },
  }
  const controller = createPreviewPlaybackController(() => clock)
  return {
    element,
    seeks,
    rates,
    controller,
    time(value) {
      clock = value
    },
    decoded(value) {
      position = value
    },
    sync(sourceTime, options = {}) {
      controller.sync(element, { sourceTime, speed: 1, playing: true, revision: 0, ...options })
    },
  }
}

test('20 seconds of normal decoder jitter never repeatedly seeks or resets playbackRate', () => {
  const f = fixture()
  f.sync(1)
  for (let frame = 1; frame <= 1200; frame += 1) {
    const time = frame / 60
    f.time(time * 1000)
    f.decoded(1 + time - 0.12)
    f.sync(1 + time)
  }
  assert.deepEqual(f.seeks, [1])
  assert.deepEqual(f.rates, [])
  assert.equal(f.element.playCalls, 1)
})

test('buffering or seeking does not get interrupted by frame-by-frame corrections', () => {
  const f = fixture()
  f.sync(1)
  f.element.readyState = 2
  for (let frame = 1; frame < 120; frame += 1) {
    f.time(frame * 16)
    f.sync(1 + frame / 60)
  }
  assert.equal(f.seeks.length, 1)
  f.element.readyState = 4
  f.element.seeking = true
  f.time(2500)
  f.sync(3.5)
  assert.equal(f.seeks.length, 1)
  f.element.seeking = false
  f.time(2600)
  f.sync(3.6)
  f.time(3099)
  f.sync(4.099)
  assert.equal(f.seeks.length, 1)
  f.time(3100)
  f.sync(4.1)
  assert.deepEqual(f.seeks, [1, 4.1])
  f.time(3200)
  f.sync(4.9)
  assert.equal(f.seeks.length, 2)
})

test('scrubbing, edits, loop boundaries and resuming align immediately', () => {
  const f = fixture()
  f.sync(1)
  f.element.seeking = true
  f.sync(7, { revision: 1 })
  assert.equal(f.seeks.at(-1), 7)
  f.element.seeking = false
  f.sync(1, { revision: 1, cycle: 1 })
  assert.equal(f.seeks.at(-1), 1)
  f.sync(2, { revision: 1, cycle: 1, playing: false })
  assert.equal(f.element.paused, true)
  f.sync(2.1, { revision: 1, cycle: 1, playing: false })
  assert.equal(f.seeks.at(-1), 2.1)
  f.controller.stop(f.element)
  f.sync(3, { revision: 1, cycle: 1 })
  assert.equal(f.seeks.at(-1), 3)
})

test('double speed uses native playback rate once and stays smooth at normal decoder lag', () => {
  const f = fixture()
  f.sync(1, { speed: 2 })
  for (let frame = 1; frame <= 600; frame += 1) {
    f.time(frame * 1000 / 60)
    f.decoded(1 + frame * 2 / 60 - 0.2)
    f.sync(1 + frame * 2 / 60, { speed: 2 })
  }
  assert.deepEqual(f.seeks, [1])
  assert.deepEqual(f.rates, [2])
})

test('pending browser play request is not submitted on every frame', async () => {
  const f = fixture()
  let finish
  f.element.play = () => {
    f.element.playCalls += 1
    return new Promise((resolve) => {
      finish = resolve
    })
  }
  for (let frame = 0; frame < 100; frame += 1) {
    f.sync(frame / 60)
  }
  assert.equal(f.element.playCalls, 1)
  f.controller.stop(f.element)
  finish()
  await Promise.resolve()
  assert.equal(f.element.paused, true)
})

test('waveform observes native media time instead of seeking on each playhead update', () => {
  const source = readFileSync(new URL('../src/views/agent/content/video-editor/AudioWaveform.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /\.setTime\(|\.seekTo\(|currentTime\s*=/)
  assert.doesNotMatch(source, /watch\([^\n]*props\.playhead/)
  assert.match(source, /media: audioElement\.value/)
})
