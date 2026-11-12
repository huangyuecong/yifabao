import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// Standalone regression script, executed with node without a separate test runner.
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import {
  addItem,
  cloneTimeline,
  createTimeline,
  findItem,
  findTrack,
  moveAudioItemToTrack,
  timelineDuration,
  TRACK_DEFINITIONS,
} from '../src/views/agent/content/video-editor/timelineState.ts'

const source = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
assert.deepEqual(errors, [])
const ast = ts.createSourceFile('Workspace.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const variables = [
  'selectedLocated',
  'selectedItem',
  'selectedTrack',
  'selectedAudioTargetKind',
  'selectedAudioTargetTrack',
  'canMoveSelectedAudioTrack',
  'selectedAudioMoveLabel',
  'selectedAudioMoveHint',
  'visibleTimelineTracks',
  'currentOverlays',
  'musicDurationRange',
  'shortMusicNotice',
]
const functions = [
  'moveSelectedAudioTrack',
  'commitTimeline',
  'videoPositionOperations',
  'timelineTransitionOperations',
  'undo',
  'redo',
  'sanitizeTimelineForActiveProjectAssets',
  'activeItems',
  'trackKindForAssetRole',
  'isSubtitleFile',
  'inferMediaKind',
  'importFiles',
  'timelineTrackForProjectAsset',
  'timelineTrackName',
  'projectAssetAddDisabled',
  'projectAssetAddDisabledReason',
  'generateMusic',
]
const implementation = [...variables, ...functions].map((name) => {
  const statement = ast.statements.find(item => ts.isFunctionDeclaration(item)
    ? item.name?.text === name
    : ts.isVariableStatement(item) && item.declarationList.declarations.some(declaration => declaration.name.getText(ast) === name))
  assert.ok(statement, `${name} must use the actual Workspace implementation`)
  return statement.getText(ast)
}).join('\n')
const compiled = ts.transpileModule(`${implementation}\nglobalThis.editor = { ${[...variables, ...functions].join(', ')} }`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const plain = value => JSON.parse(JSON.stringify(value))

function sample() {
  let timeline = addItem(createTimeline(), 'video', { id: 'video', duration_seconds: 20, metadata: { media_kind: 'image' } })
  timeline = addItem(timeline, 'overlay', { id: 'old-overlay', start_seconds: 2, duration_seconds: 8, metadata: { media_kind: 'image' } })
  timeline = addItem(timeline, 'voice', {
    id: 'audio',
    label: '原始配音',
    project_asset_id: 'source-audio',
    asset_ref_id: 'ref-audio',
    start_seconds: 3.25,
    duration_seconds: 4.8,
    trim_in_seconds: 1.1,
    trim_out_seconds: 2.2,
    speed: 1.25,
    volume: 1.7,
    enabled: true,
    metadata: { media_kind: 'audio', fade_in_seconds: 0.5, fade_out_seconds: 0.7, loop: true, source: 'local', asset_role: 'voice' },
    transform: { opacity: 0.9 },
  })
  timeline = addItem(timeline, 'music', { id: 'music-before', start_seconds: 1, duration_seconds: 2 })
  return addItem(timeline, 'music', { id: 'music-after', start_seconds: 8, duration_seconds: 2 })
}

function fixture(timeline = sample()) {
  const operations = []
  const messages = []
  const imported = []
  let id = 0
  const scope = {
    findItem,
    findTrack,
    cloneTimeline,
    timelineDuration,
    moveAudioItemToTrack,
    TRACK_DEFINITIONS,
    AbortController,
    computed: getter => ({ get value() {
      return getter()
    } }),
    timeline: { value: timeline },
    selectedItemId: { value: 'audio' },
    past: { value: [] },
    future: { value: [] },
    project: { value: null },
    playhead: { value: 3 },
    liveMessage: { value: '' },
    workflowFinalizing: { value: false },
    MAX_PROJECT_DURATION_SECONDS: 600,
    operationId: () => `op-${++id}`,
    queueOperations: batch => operations.push(...plain(batch)),
    ElMessage: Object.assign(value => messages.push(value), {
      success: value => messages.push(value),
      warning: value => messages.push(value),
      error: value => messages.push(value),
    }),
    visualRole: { value: 'intro' },
    importedAssets: { value: [] },
    createLocalId: prefix => `${prefix}-${++id}`,
    ensureTaskAndProject: async () => {},
    importMedia: async (...args) => imported.push(args),
    importSubtitle: async (...args) => imported.push(args),
    messageOf: error => error.message,
    saveState: { value: '' },
    saveMessage: { value: '' },
    projectAssetReferenceCount: () => 0,
    contentCapabilities: { value: { capabilities: [] } },
    musicPrompt: { value: '无人声配乐' },
    musicDuration: { value: 15 },
    musicGenerating: { value: false },
    musicFormat: { value: 'mp3' },
    validateAiBrief: () => true,
    manualSave: async () => true,
    registerGeneratedAssets: () => {},
  }
  vm.createContext(scope)
  vm.runInContext(compiled, scope)
  return { scope, editor: scope.editor, operations, messages, imported }
}

test('音频跨轨完整保留身份、时间、裁剪、速度、音量和淡入淡出，不重排画面', () => {
  const before = sample()
  const original = cloneTimeline(before)
  const moved = moveAudioItemToTrack(before, 'audio', 'music')
  assert.deepEqual(before, original)
  assert.deepEqual(findItem(moved, 'audio').item, findItem(before, 'audio').item)
  assert.deepEqual(findTrack(moved, 'music').items.map(item => item.id), ['music-before', 'audio', 'music-after'])
  assert.deepEqual(findTrack(moved, 'video'), findTrack(before, 'video'))
  assert.deepEqual(findTrack(moved, 'overlay'), findTrack(before, 'overlay'))
  assert.deepEqual(moveAudioItemToTrack(moved, 'audio', 'voice'), before)
})

test('锁定轨道、非音频目标、同轨和未知片段均不改变工程；兼容旧 sfx', () => {
  const timeline = sample()
  for (const target of ['voice', 'video', 'overlay', 'subtitle']) {
    assert.equal(moveAudioItemToTrack(timeline, 'audio', target), timeline)
  }
  assert.equal(moveAudioItemToTrack(timeline, 'video', 'music'), timeline)
  assert.equal(moveAudioItemToTrack(timeline, 'missing', 'music'), timeline)
  findTrack(timeline, 'music').locked = true
  assert.equal(moveAudioItemToTrack(timeline, 'audio', 'music'), timeline)
  findTrack(timeline, 'music').locked = false
  findTrack(timeline, 'voice').locked = true
  assert.equal(moveAudioItemToTrack(timeline, 'audio', 'music'), timeline)
  findTrack(timeline, 'voice').locked = false
  const legacy = moveAudioItemToTrack(timeline, 'audio', 'sfx')
  assert.deepEqual(moveAudioItemToTrack(legacy, 'audio', 'voice'), timeline)
})

test('实际工作区音频动作提交 move 并能撤销/重做，选择和所有音频参数不丢失', () => {
  const { scope, editor, operations } = fixture()
  const original = cloneTimeline(scope.timeline.value)
  assert.equal(editor.selectedAudioMoveLabel.value, '下移到背景音效')
  editor.moveSelectedAudioTrack()
  assert.equal(findItem(scope.timeline.value, 'audio').track.kind, 'music')
  assert.deepEqual(operations, [{ op_id: 'op-1', type: 'move', track_id: 'track-music', item_id: 'audio', index: 1, start_seconds: 3.25 }])
  assert.equal(scope.selectedItemId.value, 'audio')
  assert.equal(editor.selectedAudioMoveLabel.value, '上移到配音')
  assert.equal(scope.past.value.length, 1)
  editor.undo()
  assert.deepEqual(plain(scope.timeline.value), original)
  assert.ok(operations.some(operation => operation.type === 'move' && operation.item_id === 'audio' && operation.track_id === 'track-voice'))
  editor.redo()
  assert.equal(findItem(scope.timeline.value, 'audio').track.kind, 'music')
  assert.deepEqual(plain(findItem(scope.timeline.value, 'audio').item), findItem(original, 'audio').item)
  editor.moveSelectedAudioTrack()
  assert.deepEqual(plain(scope.timeline.value), original)
})

test('实际工作区只在音频选中时提供上下轨操作，锁定或合成中不提交变更', () => {
  const { scope, editor, operations, messages } = fixture()
  findTrack(scope.timeline.value, 'music').locked = true
  assert.equal(editor.canMoveSelectedAudioTrack.value, false)
  editor.moveSelectedAudioTrack()
  assert.match(messages.at(-1), /已锁定/u)
  assert.equal(operations.length, 0)
  findTrack(scope.timeline.value, 'music').locked = false
  scope.workflowFinalizing.value = true
  editor.moveSelectedAudioTrack()
  assert.equal(operations.length, 0)
  scope.workflowFinalizing.value = false
  for (const id of ['video', 'old-overlay', 'missing', '']) {
    scope.selectedItemId.value = id
    assert.equal(editor.selectedAudioTargetKind.value, undefined)
    editor.moveSelectedAudioTrack()
    assert.equal(operations.length, 0)
  }
})

test('本地混合批量导入始终将音频送入配音，图片/视频位置和字幕导入不变', async () => {
  const { editor, imported } = fixture()
  await editor.importFiles([
    { name: '旁白.mp3', type: 'audio/mpeg' },
    { name: '环境声.wav', type: 'audio/wav' },
    { name: '封面.png', type: 'image/png' },
    { name: '片头.mp4', type: 'video/mp4' },
    { name: '字幕.srt', type: '' },
  ])
  assert.deepEqual(imported.slice(0, 4).map(args => [args[0].name, args[3], args[4], args[5]]), [
    ['旁白.mp3', 'voice', 'voice', 1],
    ['环境声.wav', 'voice', 'voice', 2],
    ['封面.png', 'video', 'intro', 0],
    ['片头.mp4', 'video', 'intro', 1],
  ])
  assert.equal(imported[4][0].name, '字幕.srt')
})

test('隐藏旧叠加轨只影响轨道列表，已有叠加层仍预览并保留在完整工程快照', () => {
  const { scope, editor } = fixture()
  const before = plain(scope.timeline.value)
  assert.deepEqual(plain(editor.visibleTimelineTracks.value.map(track => track.kind)), ['video', 'subtitle', 'voice', 'music'])
  assert.deepEqual(plain(editor.currentOverlays.value.map(item => item.id)), ['old-overlay'])
  assert.deepEqual(plain(scope.timeline.value), before)
  for (const role of ['overlay', 'logo']) {
    const asset = { role, metadata: {} }
    assert.equal(editor.projectAssetAddDisabled(asset), true)
    assert.match(editor.projectAssetAddDisabledReason(asset), /已有叠加内容仍会保留并正常导出/u)
  }
  for (const role of ['video', 'voice', 'music', 'subtitle']) {
    assert.equal(editor.projectAssetAddDisabled({ role, metadata: {} }), false)
  }
})

test('实际模板显示明确音频移动标签，不再展示叠加创建、Logo 或音频导入角色选择器', () => {
  const template = descriptor.template.content
  assert.doesNotMatch(template, /<ElOption[^>]+value="(?:overlay|logo)"/u)
  assert.doesNotMatch(template, /v-model="audioRole"|move-selected-visual-track/u)
  assert.doesNotMatch(template, /默认加入配音，可选中后下移到背景音效/u)
  const button = template.match(/<ElButton\s+v-if="selectedItem && selectedAudioTargetKind"[^>]+>[\s\S]*?<\/ElButton>/u)?.[0]
  assert.ok(button)
  assert.match(button, /@click="moveSelectedAudioTrack"/u)
  assert.match(button, /:disabled="!canMoveSelectedAudioTrack"/u)
  assert.match(button, /:aria-label="selectedAudioMoveHint"/u)
  assert.match(button, /\{\{ selectedAudioMoveLabel \}\}/u)
})

test('背景音乐支持一秒起，读取最大时长和生成下限，不默改用户时长', async () => {
  const { scope, editor } = fixture()
  assert.deepEqual(plain(editor.musicDurationRange.value), { min: 1, max: 15, providerMin: 30 })
  scope.contentCapabilities.value.capabilities = [{ capability: 'music-generation', parameters: { duration_seconds: { minimum: 1, maximum: 60, provider_minimum: 45 } } }]
  assert.deepEqual(plain(editor.musicDurationRange.value), { min: 1, max: 15, providerMin: 45 })
  scope.project.value = { id: 'local-fixture', current_revision: 8 }
  const requests = []
  scope.contentApi = { async generateVideoProjectMusic(id, payload) {
    requests.push(plain(payload))
    return {}
  } }
  scope.musicDuration.value = 200
  await editor.generateMusic()
  assert.equal(requests.length, 0)
  assert.equal(scope.musicDuration.value, 200)
  scope.musicDuration.value = 5
  await editor.generateMusic()
  assert.equal(requests[0].duration_seconds, 5)
  assert.equal(scope.musicDuration.value, 5)
  assert.equal(editor.shortMusicNotice.value, '短配乐会先生成 45 秒后截取，模型按实际生成时长计费。')
  scope.musicDuration.value = 1
  await editor.generateMusic()
  assert.equal(requests[1].duration_seconds, 1)
  scope.musicDuration.value = 45
  assert.equal(editor.shortMusicNotice.value, '')
  for (const value of [0, 1.5, Number.NaN]) {
    scope.musicDuration.value = value
    await editor.generateMusic()
    assert.equal(requests.length, 2)
    assert.ok(Object.is(scope.musicDuration.value, value))
  }
})

test('背景音乐失败后结束忙碌状态并可再次生成，配乐用途和导入音效途径可见', async () => {
  const { scope, editor, messages } = fixture()
  scope.project.value = { id: 'local-fixture', current_revision: 8 }
  let calls = 0
  scope.contentApi = { async generateVideoProjectMusic() {
    calls += 1
    if (calls === 1) {
      throw new Error('配乐服务尚未配置，请联系管理员后重试')
    }
    return {}
  } }
  await editor.generateMusic()
  assert.equal(scope.musicGenerating.value, false)
  assert.match(messages.at(-1), /配乐服务尚未配置/u)
  await editor.generateMusic()
  assert.equal(calls, 2)
  assert.equal(scope.musicGenerating.value, false)
  assert.match(descriptor.template.content, /AI\s*生成背景音乐（无人声）/u)
  assert.match(descriptor.template.content, /环境音、碰杯等音效可导入音频后移到背景音效轨/u)
  const button = descriptor.template.content.match(/<ElButton[^>]+@click="generateMusic"[^>]*>/u)?.[0]
  assert.ok(button)
  assert.doesNotMatch(button, /:disabled/u)
  assert.match(descriptor.template.content, /:min="musicDurationRange.min"/u)
  assert.match(descriptor.template.content, /:max="musicDurationRange.max"/u)
})

test('其他音乐模型未声明先生成再截取时，不显示三十秒计费提示', () => {
  const { scope, editor } = fixture()
  scope.contentCapabilities.value.capabilities = [{ capability: 'music-generation', provider_id: 'other-music-provider', parameters: { duration_seconds: { minimum: 1, maximum: 120 } } }]
  scope.musicDuration.value = 5
  assert.deepEqual(plain(editor.musicDurationRange.value), { min: 1, max: 15, providerMin: 1 })
  assert.equal(editor.shortMusicNotice.value, '')
})

test('保存及生成等待期间连续点击背景音乐，只保存一次且只提交一次生成', async () => {
  const { scope, editor } = fixture()
  scope.project.value = { id: 'local-fixture', current_revision: 8 }
  let releaseSave
  let releaseGeneration
  let signalGenerationStarted
  const generationStarted = new Promise((resolve) => {
    signalGenerationStarted = resolve
  })
  let saves = 0
  let submissions = 0
  scope.manualSave = () => {
    saves += 1
    return new Promise((resolve) => {
      releaseSave = resolve
    })
  }
  scope.contentApi = { generateVideoProjectMusic() {
    submissions += 1
    signalGenerationStarted()
    return new Promise((resolve) => {
      releaseGeneration = resolve
    })
  } }
  const first = editor.generateMusic()
  assert.equal(scope.musicGenerating.value, true)
  await editor.generateMusic()
  await editor.generateMusic()
  assert.equal(saves, 1)
  assert.equal(submissions, 0)
  releaseSave(true)
  await generationStarted
  assert.equal(submissions, 1)
  await editor.generateMusic()
  assert.equal(saves, 1)
  assert.equal(submissions, 1)
  releaseGeneration({})
  await first
  assert.equal(scope.musicGenerating.value, false)
})

test('保存失败、抛错或工程未创建时恢复按钮，不提交生成并允许修复后重试', async () => {
  for (const failure of ['false', 'throw', 'missing-project']) {
    const { scope, editor, messages } = fixture()
    scope.project.value = failure === 'missing-project' ? null : { id: 'local-fixture', current_revision: 8 }
    let submissions = 0
    scope.contentApi = { async generateVideoProjectMusic() {
      submissions += 1
      return {}
    } }
    scope.manualSave = async () => {
      if (failure === 'throw') {
        throw new Error('网络中断，保存失败')
      }
      return failure !== 'false'
    }
    await editor.generateMusic()
    assert.equal(scope.musicGenerating.value, false, failure)
    assert.equal(submissions, 0, failure)
    assert.match(messages.at(-1), /保存/u)
    scope.project.value = { id: 'local-fixture', current_revision: 8 }
    scope.manualSave = async () => true
    await editor.generateMusic()
    assert.equal(submissions, 1, failure)
    assert.equal(scope.musicGenerating.value, false, failure)
  }
})
