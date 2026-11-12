import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, ref } from 'vue'
import { assetMediaKind } from '../src/views/agent/content/video-editor/assetMedia.ts'

const source = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/u)?.[1]
assert.ok(script)
const ast = ts.createSourceFile('VideoEditorWorkspace.ts', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const names = [
  'projectAssetFor',
  'projectAssetMediaKind',
  'isVideoPreviewItem',
  'replaceExactPreviewUrl',
  'prepareExactPreview',
  'acceptMediaJobUpdate',
]
const implementation = names.map((name) => {
  const statement = ast.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === name)
  assert.ok(statement, `真实 Workspace 中应存在 ${name}`)
  return statement.getText(ast)
})
for (const name of ['selectedCanTrim', 'selectedHasAudio']) {
  const declaration = ast.statements.filter(ts.isVariableStatement)
    .flatMap(statement => [...statement.declarationList.declarations])
    .find(item => ts.isIdentifier(item.name) && item.name.text === name)
  assert.ok(declaration?.initializer)
  implementation.push(`globalThis.${name} = ${declaration.initializer.getText(ast)}`)
}
const compiled = ts.transpileModule(implementation.join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText

function fixture() {
  const downloads = []
  const created = []
  const revoked = []
  const errors = []
  const scope = {
    computed,
    assetMediaKind,
    project: ref({ assets: [] }),
    selectedItem: ref(undefined),
    selectedTrack: ref({ kind: 'video' }),
    selectedCanTrim: undefined,
    selectedHasAudio: undefined,
    exactPreviewUrl: ref(''),
    exactPreviewSubtitleUrl: ref('blob:vtt-old'),
    exactPreviewJob: ref({ id: 'old', status: 'running' }),
    exactPreviewBusy: ref(true),
    editorDisposed: false,
    ownedObjectUrls: new Set(),
    liveMessage: ref(''),
    exportJob: ref(undefined),
    aiVideoJob: ref(undefined),
    isTerminalJob: job => ['succeeded', 'failed', 'cancelled'].includes(job.status),
    contentApi: {
      downloadJob: id => new Promise((resolve, reject) => {
        downloads.push({ id, resolve: () => resolve({ blob: { id } }), reject })
      }),
    },
    URL: {
      createObjectURL(blob) {
        const url = `blob:preview-${blob.id}-${created.length + 1}`
        created.push(url)
        return url
      },
      revokeObjectURL: url => revoked.push(url),
    },
    ElMessage: { error: text => errors.push(text) },
    messageOf: error => error.message,
  }
  vm.createContext(scope)
  vm.runInContext(compiled, scope)
  return { scope, downloads, created, revoked, errors }
}

function item(metadata = {}, projectAssetId) {
  return { id: 'main-item', metadata, project_asset_id: projectAssetId }
}

const tests = []
const test = (name, callback) => tests.push({ name, callback })

test('旧图片缺媒体标记、Logo 和显式图片均保持图片预览，不显示视频裁切/音量', (f) => {
  for (const metadata of [{}, { media_kind: 'logo' }, { media_kind: 'image' }, { mime_type: 'image/png' }]) {
    const current = item(metadata)
    assert.equal(f.scope.isVideoPreviewItem(current), false)
    f.scope.selectedItem.value = current
    assert.equal(f.scope.selectedCanTrim.value, false)
    assert.equal(f.scope.selectedHasAudio.value, false)
  }
})

test('明确的视频标记或视频 MIME 才启用视频预览；音频轨仍可裁切', (f) => {
  for (const metadata of [{ media_kind: 'video' }, { resource_kind: 'video' }, { mime_type: 'video/mp4' }, { media_type: 'video/webm' }]) {
    const current = item(metadata)
    assert.equal(f.scope.isVideoPreviewItem(current), true)
    f.scope.selectedItem.value = current
    assert.equal(f.scope.selectedCanTrim.value, true)
    assert.equal(f.scope.selectedHasAudio.value, true)
  }
  f.scope.selectedTrack.value = { kind: 'voice' }
  f.scope.selectedItem.value = item()
  assert.equal(f.scope.selectedCanTrim.value, true)
})

test('工程素材真实类型优先于片段残留元数据', (f) => {
  f.scope.project.value.assets = [
    { id: 'image', role: 'video', metadata: { media_kind: 'image' } },
    { id: 'video', role: 'video', metadata: { media_type: 'video/mp4' } },
  ]
  assert.equal(f.scope.isVideoPreviewItem(item({ media_kind: 'video' }, 'image')), false)
  assert.equal(f.scope.isVideoPreviewItem(item({}, 'video')), true)
})

test('渲染成功后下载阶段继续 busy，当前任务下载完成才显示画面并释放 busy', async (f) => {
  const pending = f.scope.acceptMediaJobUpdate({ id: 'old', status: 'succeeded' })
  assert.equal(f.scope.exactPreviewBusy.value, true)
  assert.equal(f.scope.exactPreviewUrl.value, '')
  assert.equal(f.downloads.length, 1)
  f.downloads[0].resolve()
  await pending
  assert.equal(f.scope.exactPreviewBusy.value, false)
  assert.equal(f.scope.exactPreviewUrl.value, 'blob:preview-old-1')
  assert.equal(f.scope.exactPreviewSubtitleUrl.value, 'blob:vtt-old')
  assert.ok(f.scope.ownedObjectUrls.has('blob:preview-old-1'))
})

test('旧下载晚返回不覆盖新任务画面或软字幕，也不释放新任务 busy', async (f) => {
  const pending = f.scope.acceptMediaJobUpdate({ id: 'old', status: 'succeeded' })
  f.scope.exactPreviewJob.value = { id: 'new', status: 'running' }
  f.scope.exactPreviewUrl.value = 'blob:current-new'
  f.scope.exactPreviewSubtitleUrl.value = 'blob:vtt-new'
  f.scope.exactPreviewBusy.value = true
  f.downloads[0].resolve()
  await pending
  assert.equal(f.scope.exactPreviewUrl.value, 'blob:current-new')
  assert.equal(f.scope.exactPreviewSubtitleUrl.value, 'blob:vtt-new')
  assert.equal(f.scope.exactPreviewBusy.value, true)
  assert.deepEqual(f.created, [])
  assert.deepEqual(f.revoked, [])
})

test('旧任务失败不弹当前任务错误，当前下载失败会提示并允许重试', async (f) => {
  const stale = f.scope.acceptMediaJobUpdate({ id: 'old', status: 'succeeded' })
  f.scope.exactPreviewJob.value = { id: 'new', status: 'running' }
  f.downloads[0].reject(new Error('旧下载失败'))
  await stale
  assert.deepEqual(f.errors, [])
  assert.equal(f.scope.exactPreviewBusy.value, true)
  const current = f.scope.acceptMediaJobUpdate({ id: 'new', status: 'succeeded' })
  f.downloads[1].reject(new Error('网络中断'))
  await current
  assert.equal(f.scope.exactPreviewBusy.value, false)
  assert.equal(f.errors.length, 1)
  assert.match(f.errors[0], /精确预览文件加载失败.*网络中断/u)
  const retry = f.scope.prepareExactPreview({ id: 'new', status: 'succeeded' })
  f.downloads[2].resolve()
  await retry
  assert.equal(f.scope.exactPreviewUrl.value, 'blob:preview-new-1')
})

test('组件卸载期间完成或失败的下载不创建 URL、不显示错误', async () => {
  for (const failure of [false, true]) {
    const f = fixture()
    const pending = f.scope.acceptMediaJobUpdate({ id: 'old', status: 'succeeded' })
    f.scope.editorDisposed = true
    if (failure) {
      f.downloads[0].reject(new Error('卸载后失败'))
    }
    else {
      f.downloads[0].resolve()
    }
    await pending
    assert.deepEqual(f.created, [])
    assert.deepEqual(f.errors, [])
    assert.equal(f.scope.exactPreviewUrl.value, '')
  }
})

test('不属于当前预览的任务更新不会触发下载；终止任务正常释放 busy', async (f) => {
  await f.scope.acceptMediaJobUpdate({ id: 'other', status: 'succeeded' })
  assert.equal(f.downloads.length, 0)
  assert.equal(f.scope.exactPreviewJob.value.id, 'old')
  await f.scope.acceptMediaJobUpdate({ id: 'old', status: 'cancelled' })
  assert.equal(f.scope.exactPreviewBusy.value, false)
  assert.equal(f.downloads.length, 0)
})

for (const { name, callback } of tests) {
  await callback(fixture())
  console.info(`PASS ${name}`)
}
console.info(`Video editor preview lifecycle: ${tests.length} tests passed.`)
