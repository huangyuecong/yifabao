import assert from 'node:assert/strict'
import process from 'node:process'

import {
  loadVideoGeneratorDraft,
  saveVideoGeneratorDraft,
  videoGeneratorDraftKey,
} from '../src/views/agent/content/video-editor/videoGeneratorDraft.ts'

class MemoryStorage {
  values = new Map()

  getItem(key) {
    return this.values.get(key) ?? null
  }

  setItem(key, value) {
    this.values.set(key, value)
  }
}

function defaults() {
  return {
    voice: {
      text: '',
      label: 'AI 配音候选',
      voice: '',
      speed: 1,
      volume: 1,
      audioFormat: 'mp3',
      instruction: '',
      generateSubtitles: true,
    },
    subtitle: {
      sourceMode: 'voice',
      sourceProjectAssetId: '',
      sourceText: '',
      text: '',
      label: 'AI 字幕',
      maxCharsPerLine: 24,
      maxLines: 2,
    },
    image: {
      prompt: '',
      label: 'AI 画面候选',
      role: 'video',
      count: 1,
      size: '2K',
      ratio: '9:16',
      watermark: false,
    },
    aiVideo: {
      prompt: '',
      referenceImageAssetIds: [],
      referenceVideoAssetIds: [],
      durationSeconds: 5,
      ratio: '16:9',
      resolution: '720p',
      generateAudio: false,
      returnLastFrame: false,
    },
    music: {
      prompt: '',
      duration: 15,
      format: 'mp3',
    },
  }
}

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

test('返回或刷新视频工作台后恢复配音、字幕、图片、AI 视频和音效参数', () => {
  const storage = new MemoryStorage()
  const draft = defaults()
  draft.voice = {
    ...draft.voice,
    text: '臻品文化，源于长期坚持。',
    voice: 'zh_male_dayi_saturn_bigtts',
    speed: 1.2,
  }
  draft.subtitle = {
    ...draft.subtitle,
    sourceProjectAssetId: 'voice-asset-1',
    maxCharsPerLine: 18,
  }
  draft.image = {
    ...draft.image,
    prompt: '暖色餐桌上的产品主视觉',
    role: 'overlay',
    count: 4,
    size: '3K',
    ratio: '16:9',
    watermark: true,
  }
  draft.aiVideo = {
    ...draft.aiVideo,
    prompt: '清溪河谷晨雾中的产品镜头',
    referenceImageAssetIds: ['image-2', 'image-1'],
    referenceVideoAssetIds: ['video-1'],
    durationSeconds: 10,
    ratio: '9:16',
    generateAudio: true,
  }
  draft.music = { prompt: '克制、温暖的品牌音乐', duration: 15, format: 'wav' }

  assert.equal(saveVideoGeneratorDraft(storage, 'task-a', draft), true)
  assert.deepEqual(loadVideoGeneratorDraft(storage, 'task-a', defaults()), {
    version: 1,
    ...draft,
  })
})

test('不同任务使用不同缓存键且空任务不会写入', () => {
  const storage = new MemoryStorage()
  const draft = defaults()
  draft.aiVideo.prompt = '任务 A 的提示词'
  assert.equal(saveVideoGeneratorDraft(storage, 'task-a', draft), true)
  assert.equal(loadVideoGeneratorDraft(storage, 'task-b', defaults()), undefined)
  assert.equal(saveVideoGeneratorDraft(storage, '', draft), false)
  assert.equal(videoGeneratorDraftKey(''), '')
  assert.notEqual(videoGeneratorDraftKey('task-a'), videoGeneratorDraftKey('task-b'))
})

test('损坏或旧版缓存被忽略，字段类型与枚举按白名单归一化', () => {
  const storage = new MemoryStorage()
  storage.setItem(videoGeneratorDraftKey('broken-json'), '{not-json')
  storage.setItem(videoGeneratorDraftKey('old-version'), JSON.stringify({ version: 0 }))
  assert.equal(loadVideoGeneratorDraft(storage, 'broken-json', defaults()), undefined)
  assert.equal(loadVideoGeneratorDraft(storage, 'old-version', defaults()), undefined)

  storage.setItem(videoGeneratorDraftKey('invalid-fields'), JSON.stringify({
    version: 1,
    voice: {
      text: '保留的文字',
      speed: '很快',
      audioFormat: 'exe',
      generateSubtitles: 'yes',
      unexpected: { polluted: true },
    },
    subtitle: { sourceMode: 'file', maxCharsPerLine: 999, maxLines: -3 },
    image: {
      prompt: '图片提示词',
      role: 'avatar',
      count: 99,
      size: 2048,
      ratio: null,
      watermark: 'yes',
    },
    aiVideo: {
      prompt: '视频提示词',
      sourceCopyArtifactId: '不应从旧缓存恢复的文案版本',
      referenceImageAssetIds: [' image-1 ', 'image-1', 12, ''],
      durationSeconds: Number.POSITIVE_INFINITY,
    },
    music: { duration: 0, format: 'flac' },
    arbitrary: { injected: true },
  }))
  const loaded = loadVideoGeneratorDraft(storage, 'invalid-fields', defaults())
  assert.ok(loaded)
  assert.equal(loaded.voice.text, '保留的文字')
  assert.equal(loaded.voice.speed, 1)
  assert.equal(loaded.voice.audioFormat, 'mp3')
  assert.equal(loaded.voice.generateSubtitles, true)
  assert.equal(loaded.subtitle.sourceMode, 'voice')
  assert.equal(loaded.subtitle.maxCharsPerLine, 100)
  assert.equal(loaded.subtitle.maxLines, 1)
  assert.equal(loaded.image.prompt, '图片提示词')
  assert.equal(loaded.image.role, 'video')
  assert.equal(loaded.image.count, 8)
  assert.equal(loaded.image.size, '2K')
  assert.equal(loaded.image.ratio, '9:16')
  assert.equal(loaded.image.watermark, false)
  assert.deepEqual(loaded.aiVideo.referenceImageAssetIds, ['image-1'])
  assert.equal('sourceCopyArtifactId' in loaded.aiVideo, false)
  assert.equal(loaded.aiVideo.durationSeconds, 5)
  assert.equal(loaded.music.duration, 1)
  assert.equal(loaded.music.format, 'mp3')
  assert.equal('arbitrary' in loaded, false)
  assert.equal('unexpected' in loaded.voice, false)
})

if (failures.length) {
  process.exitCode = 1
}
else {
  console.info('Video generator draft tests passed: 3 checks passed.')
}
