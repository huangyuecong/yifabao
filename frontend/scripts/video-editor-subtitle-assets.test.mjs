import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// Standalone regression script, executed with node without a separate test runner.
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { assetMediaKind, projectAssetDurationSeconds, selectProjectAssetPreviewUrl } from '../src/views/agent/content/video-editor/assetMedia.ts'
import { addItem, cloneTimeline, createTimeline, findTrack, normalizeItem, parseSubtitleText, timelineDuration, TRACK_DEFINITIONS } from '../src/views/agent/content/video-editor/timelineState.ts'

const source = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
assert.deepEqual(errors, [])
const ast = ts.createSourceFile('Workspace.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const functions = [
  'subtitleSegmentsFromAsset',
  'loadProjectSubtitleCues',
  'addSubtitleCuesToTimeline',
  'importSubtitle',
  'projectAssetReferenceCount',
  'projectAssetMediaKind',
  'timelineTrackForProjectAsset',
  'timelineTrackName',
  'subtitleTimelineOffset',
  'projectAssetAddDisabled',
  'projectAssetAddDisabledReason',
  'addProjectAssetToTimeline',
  'importAllWorkflowProjectAssets',
  'timelineTransitionOperations',
]
const implementation = functions.map((name) => {
  const statement = ast.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === name)
  assert.ok(statement, `${name} must use the actual Workspace implementation`)
  return statement.getText(ast)
}).join('\n')
const compiled = ts.transpileModule(`${implementation}\nglobalThis.editor = { ${functions.join(', ')} }`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const plain = value => JSON.parse(JSON.stringify(value))
const srt = '1\r\n00:00:01,250 --> 00:00:03,000\r\n第一句字幕\r\n第二行\r\n\r\n2\r\n00:00:04,000 --> 00:00:06,500\r\n第二句字幕\r\n'
const sampleAsset = (overrides = {}) => ({
  id: 'subtitle-file',
  role: 'subtitle',
  label: '本地字幕.srt',
  active: true,
  source_type: 'upload',
  original_url: '/api/v1/video-projects/project/assets/subtitle-file/download',
  stream_url: '',
  derivatives: [],
  metadata: { filename: '本地字幕.srt' },
  ...overrides,
})
const downloadResult = (text = srt, fileName = '原始字幕.srt') => ({ blob: new Blob([text]), fileName })

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function fixture(assets = [sampleAsset()], download = async () => downloadResult()) {
  const operations = []
  const messages = []
  const downloads = []
  const saves = []
  let id = 0
  const scope = {
    assetMediaKind,
    projectAssetDurationSeconds,
    selectProjectAssetPreviewUrl,
    cloneTimeline,
    findTrack,
    normalizeItem,
    parseSubtitleText,
    timelineDuration,
    TRACK_DEFINITIONS,
    timeline: { value: createTimeline() },
    project: { value: { id: 'project', assets } },
    past: { value: [] },
    future: { value: [] },
    playhead: { value: 0 },
    subtitleOutputMode: { value: 'burn' },
    selectedItemId: { value: '' },
    liveMessage: { value: '' },
    workflowProjectAssetsToImport: { value: assets },
    workflowBulkImporting: { value: false },
    workflowFinalizing: { value: false },
    editorDisposed: false,
    MAX_PROJECT_DURATION_SECONDS: 600,
    operationId: () => `op-${++id}`,
    createLocalId: prefix => `${prefix}-${++id}`,
    queueOperations: batch => operations.push(...plain(batch)),
    messageOf: error => error.message,
    ElMessage: {
      success: value => messages.push(value),
      warning: value => messages.push(value),
      error: value => messages.push(value),
    },
    contentApi: {
      downloadVideoProjectAssetUrl: async (url) => {
        downloads.push(url)
        return download(url)
      },
    },
    manualSave: async () => {
      saves.push(plain(scope.timeline.value))
      return true
    },
    uploadImportedFile: async () => assets[0],
    addTimelineItem: () => {
      assert.fail('Subtitle assets must become parsed cues, never a generic file/title placeholder')
    },
  }
  vm.createContext(scope)
  vm.runInContext(compiled, scope)
  return { scope, editor: scope.editor, operations, messages, downloads, saves, cues: () => findTrack(scope.timeline.value, 'subtitle').items }
}

test('工程素材加入真实 SRT，正文与时间码完整且每个字幕只有内联来源', async () => {
  const asset = sampleAsset()
  const { editor, cues, operations, downloads, saves } = fixture([asset])
  assert.equal(await editor.addProjectAssetToTimeline(asset), true)
  assert.deepEqual(downloads, [asset.original_url])
  assert.deepEqual(cues().map(item => [item.start_seconds, item.duration_seconds, item.content]), [
    [1.25, 1.75, '第一句字幕\n第二行'],
    [4, 2.5, '第二句字幕'],
  ])
  assert.equal(saves.length, 1)
  for (const item of cues()) {
    assert.equal(item.project_asset_id, null)
    assert.equal(item.metadata.source_project_asset_id, asset.id)
    assert.notEqual(item.content, asset.label)
  }
  assert.equal(operations.length, 2)
  for (const operation of operations) {
    assert.equal(operation.type, 'add')
    assert.equal(operation.track_kind, 'subtitle')
    assert.equal(operation.item.project_asset_id, null)
    assert.ok(operation.item.content)
  }
})

test('本地字幕初次导入后移出时间线，再从工程素材加入仍恢复原文', async () => {
  const asset = sampleAsset()
  const { scope, editor, cues, downloads } = fixture([asset])
  const view = { status: 'uploading' }
  await editor.importSubtitle({ name: '本地字幕.srt', text: async () => srt }, view)
  const imported = plain(cues()).map(({ start_seconds, duration_seconds, content }) => ({ start_seconds, duration_seconds, content }))
  assert.equal(view.status, 'ready')
  assert.equal(downloads.length, 0)
  findTrack(scope.timeline.value, 'subtitle').items = []
  assert.equal(await editor.addProjectAssetToTimeline(asset), true)
  assert.deepEqual(plain(cues()).map(({ start_seconds, duration_seconds, content }) => ({ start_seconds, duration_seconds, content })), imported)
})

test('服务端已提供有效分句时直接使用分句，不下载文件', async () => {
  const asset = sampleAsset({
    original_url: '',
    metadata: { segments: [
      { start_seconds: 4, end_seconds: 7, text: '第二句', timing_source: 'asr' },
      { start_seconds: 1, end_seconds: 3, text: '第一句', timing_source: 'asr' },
    ] },
  })
  const { editor, cues, downloads } = fixture([asset])
  assert.equal(await editor.addProjectAssetToTimeline(asset), true)
  assert.equal(downloads.length, 0)
  assert.deepEqual(cues().map(item => item.content), ['第一句', '第二句'])
  assert.ok(cues().every(item => item.metadata.timing_source === 'asr'))
})

test('VTT 时间码和 TXT 原文按下载文件名解析，不能使用素材标题作正文', async () => {
  for (const [fileName, text, expected] of [
    ['原始字幕.VTT', 'WEBVTT\n\n00:01.000 --> 00:03.500 align:center\nVTT 正文\n', [[1, 2.5, 'VTT 正文']]],
    ['原始字幕.TXT', '第一行正文\n\n第二行正文', [[0, 2.8, '第一行正文'], [3, 2.8, '第二行正文']]],
  ]) {
    const asset = sampleAsset({ label: '仅用于显示的素材标题', metadata: {} })
    const { editor, cues } = fixture([asset], async () => downloadResult(text, fileName))
    assert.equal(await editor.addProjectAssetToTimeline(asset), true)
    assert.deepEqual(cues().map(item => [item.start_seconds, item.duration_seconds, item.content]), expected)
  }
})

test('下载失败、文件为空、文件无时间码或没有原文件时不添加占位字幕', async () => {
  for (const failure of ['network', 'empty', 'invalid', 'missing']) {
    const asset = sampleAsset(failure === 'missing' ? { original_url: '' } : {})
    const { editor, cues, operations, saves, messages } = fixture([asset], async () => {
      if (failure === 'network') {
        throw new Error('网络下载失败')
      }
      return downloadResult(failure === 'empty' ? '' : '这是没有时间码的 SRT')
    })
    assert.equal(await editor.addProjectAssetToTimeline(asset), false, failure)
    assert.equal(cues().length, 0, failure)
    assert.equal(operations.length, 0, failure)
    assert.equal(saves.length, 0, failure)
    assert.match(messages.at(-1), /字幕加入失败/u)
  }
})

test('连续双击和已加入后的重复点击不产生重复分句', async () => {
  const pending = deferred()
  const asset = sampleAsset()
  const { editor, cues, operations, downloads, saves } = fixture([asset], () => pending.promise)
  const first = editor.addProjectAssetToTimeline(asset)
  const second = editor.addProjectAssetToTimeline(asset)
  pending.resolve(downloadResult())
  assert.deepEqual(await Promise.all([first, second]), [true, false])
  assert.equal(cues().length, 2)
  assert.equal(operations.length, 2)
  assert.equal(saves.length, 1)
  const previousDownloads = downloads.length
  assert.equal(await editor.addProjectAssetToTimeline(asset), false)
  assert.equal(downloads.length, previousDownloads)
})

test('下载期间播放位置改变也保持点击时的非零插入位置', async () => {
  const pending = deferred()
  const asset = sampleAsset()
  const { scope, editor, cues } = fixture([asset], () => pending.promise)
  scope.playhead.value = 12.5
  const adding = editor.addProjectAssetToTimeline(asset)
  scope.playhead.value = 80
  pending.resolve(downloadResult())
  assert.equal(await adding, true)
  assert.deepEqual(cues().map(item => item.start_seconds), [13.75, 16.5])
})

test('配音关联字幕对齐配音片段开始位置', async () => {
  const asset = sampleAsset({ metadata: { source_project_asset_id: 'voice-source' } })
  const { scope, editor, cues } = fixture([asset])
  scope.timeline.value = addItem(scope.timeline.value, 'voice', { id: 'voice-item', project_asset_id: 'voice-source', start_seconds: 10, duration_seconds: 8 })
  scope.playhead.value = 90
  assert.equal(await editor.addProjectAssetToTimeline(asset), true)
  assert.deepEqual(cues().map(item => item.start_seconds), [11.25, 14])
})

test('下载期间切换工程、关闭编辑器、合成、移除素材或锁轨都不会误加字幕', async () => {
  for (const change of ['project', 'disposed', 'finalizing', 'removed', 'inactive', 'locked']) {
    const pending = deferred()
    const asset = sampleAsset()
    const { scope, editor, cues, operations, saves } = fixture([asset], () => pending.promise)
    const adding = editor.addProjectAssetToTimeline(asset)
    if (change === 'project') {
      scope.project.value = { id: 'other-project', assets: [asset] }
    }
    else if (change === 'disposed') {
      scope.editorDisposed = true
    }
    else if (change === 'finalizing') {
      scope.workflowFinalizing.value = true
    }
    else if (change === 'removed') {
      scope.project.value.assets = []
    }
    else if (change === 'inactive') {
      asset.active = false
    }
    else {
      findTrack(scope.timeline.value, 'subtitle').locked = true
    }
    pending.resolve(downloadResult())
    assert.equal(await adding, false, change)
    assert.equal(cues().length, 0, change)
    assert.equal(operations.length, 0, change)
    assert.equal(saves.length, 0, change)
  }
})

test('批量加入等待字幕下载解析后统一保存，不能提前按 Promise 计成功', async () => {
  const pending = deferred()
  const first = sampleAsset()
  const second = sampleAsset({ id: 'second-subtitle', metadata: { segments: [{ start_seconds: 8, end_seconds: 9, text: '第三句字幕' }] } })
  const { scope, editor, cues, saves, messages } = fixture([first, second], () => pending.promise)
  const importing = editor.importAllWorkflowProjectAssets()
  assert.equal(scope.workflowBulkImporting.value, true)
  assert.equal(saves.length, 0)
  assert.equal(cues().length, 0)
  pending.resolve(downloadResult())
  await importing
  assert.equal(scope.workflowBulkImporting.value, false)
  assert.equal(saves.length, 1)
  assert.equal(findTrack(saves[0], 'subtitle').items.length, 3)
  assert.match(messages.at(-1), /已全部导入 2 项/u)
})

test('批量加入下载失败的字幕仅计为未加入，并保存实际成功的素材', async () => {
  const failed = sampleAsset()
  const valid = sampleAsset({ id: 'valid-subtitle', metadata: { segments: [{ start_seconds: 0, end_seconds: 2, text: '有效正文' }] } })
  const { scope, editor, cues, saves, messages } = fixture([failed, valid], async () => {
    throw new Error('读取失败')
  })
  await editor.importAllWorkflowProjectAssets()
  assert.equal(scope.workflowBulkImporting.value, false)
  assert.equal(saves.length, 1)
  assert.deepEqual(cues().map(item => item.content), ['有效正文'])
  assert.match(messages.at(-1), /导入 1 项前序素材，另有 1 项未加入/u)
})

test('原始文件字幕的属性面板不再允许混入内联正文或修改不生效的开始位置', () => {
  const elements = []
  function visit(node) {
    if (node.type === 1) {
      elements.push(node)
    }
    for (const child of node.children || []) {
      visit(child)
    }
  }
  visit(descriptor.template.ast)
  const contentField = elements.find(node => node.tag === 'ElFormItem' && node.props.some(prop => prop.name === 'label' && prop.value?.content === '显示内容'))
  const visibleExpression = contentField?.props.find(prop => prop.name === 'if')?.exp?.content
  assert.ok(visibleExpression)
  const startField = elements.find(node => node.tag === 'ElInputNumber' && node.props.some(prop => prop.arg?.content === 'model-value' && prop.exp?.content === 'selectedItem.start_seconds'))
  const disabledExpression = startField?.props.find(prop => prop.arg?.content === 'disabled')?.exp?.content
  assert.ok(disabledExpression)
  for (const [projectAssetId, visible, disabled] of [['subtitle-file', false, true], [null, true, false]]) {
    const context = { selectedTrack: { kind: 'subtitle' }, selectedItem: { project_asset_id: projectAssetId } }
    assert.equal(vm.runInNewContext(visibleExpression, context), visible)
    assert.equal(vm.runInNewContext(disabledExpression, context), disabled)
  }
})
