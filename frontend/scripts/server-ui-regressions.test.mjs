import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { parse } from 'vue/compiler-sfc'

function component(path) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  const ast = ts.createSourceFile(path, descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  return { source, descriptor, ast }
}

const uploadScope = {}
vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../src/views/agent/content/libraryUpload.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText, { exports: uploadScope })
const library = component('../src/views/agent/content/ContentLibraryView.vue')
const workspace = component('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue')
const taskView = component('../src/views/agent/content/ContentTaskView.vue')

test('first export prompts for confirmation and reserves the outdated warning for stale exports', () => {
  const scope = {
    computed,
    exportStageConfirmed: ref(false),
    archivedReadOnly: ref(false),
    currentStage: ref({ state: 'not_started' }),
    exportPresentation: ref({ title: '导出最终视频', description: '下载成片' }),
  }
  const f = executeDeclarations(taskView, ['exportResultTitle', 'exportResultDescription'], scope)
  assert.equal(f.exportResultTitle.value, '待确认导出')
  assert.doesNotMatch(f.exportResultDescription.value, /失效|变化/)
  scope.currentStage.value.state = 'stale'
  assert.equal(f.exportResultTitle.value, '导出内容需要更新')
  scope.exportStageConfirmed.value = true
  assert.equal(f.exportResultTitle.value, '导出最终视频')
})

function executeDeclarations(component, names, scope) {
  const statements = component.ast.statements.filter((statement) => {
    if (ts.isFunctionDeclaration(statement)) {
      return names.includes(statement.name?.text)
    }
    if (ts.isVariableStatement(statement)) {
      return statement.declarationList.declarations.some(item => names.includes(item.name.getText()))
    }
    return false
  })
  const code = statements.map(item => item.getText(component.ast).replace(/^export\s+/, '')).join('\n')
  const compiled = ts.transpileModule(`${code}\nglobalThis.result = { ${names.join(', ')} }`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  }).outputText
  vm.runInNewContext(compiled, scope)
  return scope.result
}

function libraryFixture() {
  const messages = []
  const writes = []
  const scope = {
    computed,
    ...uploadScope,
    versionFile: ref(),
    versionKindBeforeReplacement: ref('document'),
    versionForm: reactive({ name: '资料', category: 'brand', resource_kind: 'document', text_content: '原文本', tags: [] }),
    editingResource: ref({ id: 'existing', version: 1, resource_kind: 'document', metadata: {}, facts: [] }),
    versionDialogOpen: ref(true),
    categories: ref([]),
    resources: ref([]),
    busy: ref(''),
    resourceForm: reactive({ name: '', category: '', resource_kind: 'document', text_content: '资料正文', notes: '', tags: [] }),
    selectedFile: ref(),
    createDrawerOpen: ref(true),
    ElMessage: { warning: value => messages.push(value), success() {}, error: value => messages.push(value) },
    messageOf: String,
    contentApi: {
      createLibrary: async (value) => {
        writes.push(value)
        return { id: 'saved', ...value }
      },
      uploadLibrary: async (value) => {
        writes.push(value)
        return { id: 'saved', ...value }
      },
      createLibraryVersion: async (id, value) => {
        writes.push(value)
        return { id, ...value }
      },
      uploadLibraryVersion: async (id, value) => {
        writes.push(value)
        return { id, ...value }
      },
      listLibrary: async () => writes.map(value => ({ id: 'saved', ...value })),
      listLibraryCategories: async () => [],
    },
  }
  return { scope, messages, writes, ...executeDeclarations(library, ['categoryLabels', 'categoryLabel', 'categoryOptions', 'chooseFile', 'chooseVersionFile', 'changeResourceKind', 'cancelVersionFile', 'createVersion', 'finishResourceSave', 'saveResource', 'saveTextResource', 'uploadResource'], scope) }
}

test('empty library offers named categories and saves the chosen category on first entry', async () => {
  const f = libraryFixture()
  const option = f.categoryOptions.value.find(item => item.value === 'brand_logo')
  assert.equal(option.label, '品牌标识')
  f.scope.resourceForm.name = '企业 Logo'
  await f.saveTextResource()
  assert.equal(f.writes.length, 0)
  assert.match(f.messages[0], /请选择资料分类/)
  assert.doesNotMatch(f.messages[0], /资料名称/)
  f.scope.resourceForm.category = option.value
  await f.saveTextResource()
  assert.equal(f.writes[0].name, '企业 Logo')
  assert.equal(f.writes[0].category, 'brand_logo')
  assert.equal(f.scope.createDrawerOpen.value, false)
})

test('upload validation reports the actual missing field, then keeps file and category', async () => {
  const f = libraryFixture()
  await f.uploadResource()
  assert.equal(f.messages.at(-1), '请填写资料名称')
  f.scope.resourceForm.name = '参考图片'
  f.scope.resourceForm.category = '自定义资料分类'
  await f.uploadResource()
  assert.equal(f.messages.at(-1), '请选择要上传的文件')
  const file = { name: 'reference.png', size: 42 }
  f.scope.selectedFile.value = file
  await f.uploadResource()
  assert.equal(f.writes[0].category, '自定义资料分类')
  assert.equal(f.writes[0].file.name, 'reference.png')
})

test('one save action uploads media, preserves Logo, and never calls text creation', async () => {
  for (const [name, requested, expected] of [
    ['reference.JPG', 'product', 'image'],
    ['logo.png', 'logo', 'logo'],
    ['clip.mp4', 'document', 'video'],
    ['voice.mp3', 'spec', 'audio'],
    ['manual.txt', 'product', 'product'],
  ]) {
    const f = libraryFixture()
    f.scope.resourceForm.resource_kind = requested
    const input = { files: [{ name, size: 42 }], value: 'selected' }
    f.chooseFile({ target: input })
    assert.equal(input.value, '')
    assert.equal(f.scope.resourceForm.resource_kind, expected)
    assert.equal(f.scope.resourceForm.name, name.replace(/\.[^.]+$/, ''))
    f.scope.resourceForm.category = '自定义分类'
    f.scope.contentApi.createLibrary = () => {
      throw new Error('must upload file')
    }
    await f.saveResource()
    assert.equal(f.writes.length, 1)
    assert.equal(f.writes[0].file.name, name)
    assert.equal(f.writes[0].resource_kind, expected)
    assert.equal(f.writes[0].text_content, '资料正文')
    assert.equal(f.scope.resources.value[0].id, 'saved')
    assert.equal(f.scope.selectedFile.value, undefined)
  }
})

test('text is required for every type, while a file is always optional', async () => {
  const f = libraryFixture()
  Object.assign(f.scope.resourceForm, { name: '资料', category: 'brand', text_content: ' ' })
  await f.saveResource()
  assert.equal(f.messages.at(-1), '请填写文本内容')
  f.scope.resourceForm.resource_kind = 'logo'
  await f.saveResource()
  assert.equal(f.messages.at(-1), '请填写文本内容')
  await f.saveTextResource()
  assert.equal(f.messages.at(-1), '请填写文本内容')
  assert.equal(f.writes.length, 0)
  f.scope.resourceForm.text_content = '有效正文'
  await f.saveResource()
  assert.equal(f.writes[0].text_content, '有效正文')
  assert.equal(f.writes[0].file, undefined)
  assert.equal(f.writes[0].resource_kind, 'logo')
})

test('selecting and cancelling a replacement preserves text and restores the original type', async () => {
  const f = libraryFixture()
  f.chooseVersionFile({ target: { files: [{ name: 'replacement.png', size: 30 }] } })
  assert.equal(f.scope.versionForm.resource_kind, 'image')
  assert.equal(f.scope.versionForm.text_content, '原文本')
  f.scope.versionForm.text_content = '保留编辑后的文本'
  f.cancelVersionFile()
  assert.equal(f.scope.versionFile.value, undefined)
  assert.equal(f.scope.versionForm.resource_kind, 'document')
  assert.equal(f.scope.versionForm.text_content, '保留编辑后的文本')
  await f.createVersion()
  assert.equal(f.writes[0].file, undefined)
  assert.equal(f.writes[0].text_content, '保留编辑后的文本')
})

test('replacement upload sends text and blocks a blank description without losing the selection', async () => {
  const f = libraryFixture()
  f.chooseVersionFile({ target: { files: [{ name: 'replacement.mp4', size: 30 }] } })
  f.scope.versionForm.text_content = ' '
  await f.createVersion()
  assert.equal(f.writes.length, 0)
  assert.equal(f.messages.at(-1), '请填写文本内容')
  assert.equal(f.scope.versionFile.value.name, 'replacement.mp4')
  f.scope.versionForm.text_content = '新版视频说明'
  await f.createVersion()
  assert.equal(f.writes[0].file.name, 'replacement.mp4')
  assert.equal(f.writes[0].text_content, '新版视频说明')
})

test('file upload also requires text before creating a resource', async () => {
  const f = libraryFixture()
  f.chooseFile({ target: { files: [{ name: 'reference.png', size: 30 }] } })
  Object.assign(f.scope.resourceForm, { category: 'brand', text_content: ' ' })
  await f.saveResource()
  assert.equal(f.writes.length, 0)
  assert.equal(f.messages.at(-1), '请填写文本内容')
})

test('both upload clients send authored text in multipart body, not in the URL', async () => {
  const requests = []
  const source = readFileSync(new URL('../src/api/agent/content.ts', import.meta.url), 'utf8')
  const ast = ts.createSourceFile('content.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const f = executeDeclarations({ ast }, ['contentApi'], {
    FormData,
    request: async value => requests.push(value),
  })
  const payload = { category: 'brand', name: '资料', resource_kind: 'image', expected_version: 1, text_content: '说明\n包含完整文本', file: new File(['file'], 'reference.png') }
  await f.contentApi.uploadLibrary(payload)
  await f.contentApi.uploadLibraryVersion('existing', payload)
  for (const request of requests) {
    assert.equal(request.data.get('text_content'), payload.text_content)
    assert.equal(request.data.get('file').name, 'reference.png')
    assert.equal(request.params.text_content, undefined)
  }
})

test('file validation rejects empty, unsupported and over-limit files, keeping valid selection', () => {
  const f = libraryFixture()
  f.chooseFile({ target: { files: [{ name: 'valid.png', size: 30 }] } })
  for (const file of [{ name: 'empty.png', size: 0 }, { name: 'bad.exe', size: 1 }, { name: 'large.png', size: 101 * 1024 * 1024 }]) {
    f.chooseFile({ target: { files: [file] } })
    assert.equal(f.scope.selectedFile.value.name, 'valid.png')
  }
  assert.equal(f.messages.length, 3)
  f.scope.resourceForm.resource_kind = 'video'
  f.changeResourceKind()
  assert.equal(f.scope.selectedFile.value, undefined)
})

test('failed upload retains the file and form, while double submit creates only one request', async () => {
  const f = libraryFixture()
  f.chooseFile({ target: { files: [{ name: 'voice.wav', size: 30 }] } })
  f.scope.resourceForm.category = 'audio'
  let rejectUpload
  let calls = 0
  f.scope.contentApi.uploadLibrary = () => {
    calls++
    return new Promise((resolve, reject) => {
      rejectUpload = reject
    })
  }
  const saving = f.saveResource()
  await f.saveResource()
  assert.equal(calls, 1)
  rejectUpload(new Error('上传失败'))
  await saving
  assert.equal(f.scope.busy.value, '')
  assert.equal(f.scope.selectedFile.value.name, 'voice.wav')
  assert.equal(f.scope.resourceForm.name, 'voice')
  assert.equal(f.scope.createDrawerOpen.value, true)
})

test('successful write stays visible when refresh fails and cannot be mistaken for a failed save', async () => {
  const f = libraryFixture()
  f.chooseFile({ target: { files: [{ name: 'logo.png', size: 30 }] } })
  f.scope.resourceForm.category = 'logo'
  f.scope.contentApi.listLibrary = async () => {
    throw new Error('offline')
  }
  f.scope.contentApi.listLibraryCategories = async () => {
    throw new Error('offline')
  }
  await f.saveResource()
  assert.equal(f.scope.resources.value[0].id, 'saved')
  assert.equal(f.scope.createDrawerOpen.value, false)
  assert.match(f.messages.at(-1), /无需重复保存/)
  assert.equal(f.writes.length, 1)
})

test('library creation has one save action and no unnecessary metadata inputs', () => {
  const template = library.descriptor.template.content
  assert.doesNotMatch(template, /保存文本资料|上传素材|动态标签|上级资料|关键事实|资料来源链接/)
  assert.match(template, /@click="saveResource"/)
  assert.match(template, /label="资料文件（可选）"/)
  assert.doesNotMatch(template, /添加方式|手动填写|选择文件后点击底部|v-if="!usesFile"/)
  assert.equal((template.match(/label="文本内容" required/g) || []).length, 2)
  assert.match(template, /#footer/)
  assert.match(library.source, /facts: editingResource\.value\.facts \?\? \[\]/)
})

test('editing basic details preserves hidden metadata including structured facts', async () => {
  const writes = []
  const original = {
    id: 'old',
    version: 3,
    resource_kind: 'document',
    category: 'brand',
    name: '资料',
    text_content: '正文',
    data_level: 'internal',
    tags: ['品牌'],
    parent_id: 'parent',
    source_url: 'https://example.com/source',
    crawled_at: '2026-09-23T00:00:00Z',
    freshness: '长期有效',
    facts: [{ statement: '已审核', verified: true, source: { page: 2 } }],
    notes: '备注',
    metadata: { source_type: 'official' },
  }
  const scope = {
    editingResource: ref(),
    versionFile: ref(),
    versionKindBeforeReplacement: ref('document'),
    versionForm: reactive({}),
    versionDialogOpen: ref(false),
    busy: ref(''),
    resources: ref([]),
    ElMessage: { warning() {}, success() {}, error(value) {
      throw new Error(value)
    } },
    messageOf: String,
    contentApi: { createLibraryVersion: async (id, value) => writes.push({ id, value }), listLibrary: async () => [] },
  }
  const f = executeDeclarations(library, ['openVersionEditor', 'createVersion'], scope)
  f.openVersionEditor(original)
  scope.versionForm.name = '修改名称'
  await f.createVersion()
  assert.equal(writes[0].id, 'old')
  assert.equal(writes[0].value.name, '修改名称')
  for (const field of ['tags', 'parent_id', 'source_url', 'crawled_at', 'freshness', 'facts', 'metadata']) {
    assert.equal(JSON.stringify(writes[0].value[field]), JSON.stringify(original[field]))
  }
})

test('subtitle source falls back to text without voices and keeps a selectable real voice', async () => {
  const scope = { reactive, ref, computed, watch, project: ref({ assets: [] }), projectAssetMediaKind: asset => asset.media_type.startsWith('audio/') ? 'audio' : 'video', compareContentDateTimeDescending: () => 0 }
  const f = executeDeclarations(workspace, ['subtitleForm', 'voiceProjectAssets', 'selectedSubtitleVoiceAsset'], scope)
  Object.assign(scope, f)
  const watcher = workspace.ast.statements.find(statement => /^watch\(\s*\[voiceProjectAssets,/.test(statement.getText()))
  assert.ok(watcher)
  vm.runInNewContext(ts.transpileModule(watcher.getText(), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, scope)
  assert.equal(f.subtitleForm.sourceMode, 'text')
  f.subtitleForm.sourceMode = 'voice'
  await nextTick()
  assert.equal(f.subtitleForm.sourceMode, 'text', 'old draft cannot trap an empty project in voice mode')
  scope.project.value.assets = [{ id: 'voice-1', active: true, role: 'voice', media_type: 'audio/mpeg', metadata: { text: '字幕文稿' } }]
  await nextTick()
  f.subtitleForm.sourceMode = 'voice'
  await nextTick()
  assert.equal(f.selectedSubtitleVoiceAsset.value.id, 'voice-1')
  assert.equal(f.subtitleForm.sourceMode, 'voice')
})

test('new video generation enables sound and keeps an explicit user opt-out', () => {
  const scope = { reactive, aiVideoRatios: ref(['16:9']), aiVideoResolutions: ref(['720p']), aiVideoGenerateAudioSupported: ref(true), aiVideoReturnLastFrameSupported: ref(true) }
  const f = executeDeclarations(workspace, ['aiVideoForm', 'syncAiVideoCapabilityDefaults'], scope)
  f.syncAiVideoCapabilityDefaults()
  assert.equal(f.aiVideoForm.generateAudio, true)
  f.aiVideoForm.generateAudio = false
  f.syncAiVideoCapabilityDefaults()
  assert.equal(f.aiVideoForm.generateAudio, false)
  f.aiVideoForm.generateAudio = true
  scope.aiVideoGenerateAudioSupported.value = false
  f.syncAiVideoCapabilityDefaults()
  assert.equal(f.aiVideoForm.generateAudio, false)
})

test('both video routes expose home, omit exact-preview action and include music in accordion', () => {
  const template = workspace.descriptor.template.content
  assert.doesNotMatch(template, /@click="createExactPreview"/)
  assert.match(template, /<ElButton\s+@click="requestEditorHome"/)
  const musicIndex = template.indexOf('<ElCollapseItem name="music">')
  const musicEnd = template.indexOf('</ElCollapseItem>', musicIndex)
  assert.ok(musicIndex > 0)
  assert.match(template.slice(musicIndex, musicEnd), /@click="generateMusic"/)
  for (const name of ['任务名称', '主题', '目标人群']) {
    assert.ok(template.includes(`<ElFormItem label="${name}" required>`))
  }
})
