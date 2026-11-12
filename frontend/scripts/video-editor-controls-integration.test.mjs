import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { computed, createRenderer, defineComponent, h, nextTick, ref } from 'vue'
import { compileTemplate, parse } from 'vue/compiler-sfc'
import { assetMediaKind, projectAssetDurationSeconds } from '../src/views/agent/content/video-editor/assetMedia.ts'
import { isTerminalMediaJob, previewFps } from '../src/views/agent/content/video-editor/mediaJobState.ts'
import { timelineWebVtt } from '../src/views/agent/content/video-editor/previewSubtitles.ts'
import {
  addItem,
  cloneTimeline,
  createTimeline,
  findItem,
  findTrack,
  isImageTimelineItem,
  trimPatch,
  updateItem,
} from '../src/views/agent/content/video-editor/timelineState.ts'
import { workflowStageBaseline } from '../src/views/agent/content/video-editor/workflowFinalization.ts'

const filename = 'VideoEditorWorkspace.vue'
const source = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source, { filename })
assert.deepEqual(errors, [])
const ast = ts.createSourceFile(filename, descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const functionNames = [
  'patchSelected',
  'patchSelectedNumber',
  'updateMetadataField',
  'updateSubtitleStyle',
  'updateSubtitleOutputMode',
  'subtitleStyleValue',
  'updateTransition',
  'updateTransform',
  'updateCanvas',
  'applyCanvasPreset',
  'updateSelectedVoiceSpeed',
  'finalRenderProfile',
  'isFinalWorkflowRender',
  'waitForWorkflowRender',
  'completeWorkflowVideo',
  'submitProjectExport',
  'continuePersistedWorkflowRender',
  'trackMediaJob',
  'acceptMediaJobUpdate',
  'isVideoPreviewItem',
  'projectAssetFor',
  'projectAssetMediaKind',
  'replaceExactPreviewUrl',
]
const variableNames = ['selectedLocated', 'selectedItem', 'selectedTrack', 'selectedCanTrim', 'selectedHasAudio', 'subtitleOutputMode']
const declarations = [
  ...variableNames.map((name) => {
    const statement = ast.statements.find(item => ts.isVariableStatement(item)
      && item.declarationList.declarations.some(declaration => declaration.name.getText(ast) === name))
    assert.ok(statement, `实际变量 ${name} 应存在`)
    return statement.getText(ast)
  }),
  ...functionNames.map((name) => {
    const statement = ast.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === name)
    assert.ok(statement, `实际函数 ${name} 应存在`)
    return statement.getText(ast)
  }),
]
const bindings = [
  'computed',
  'timeline',
  'selectedItemId',
  'findItem',
  'findTrack',
  'isImageTimelineItem',
  'trimPatch',
  'updateItem',
  'cloneTimeline',
  'assetMediaKind',
  'previewVideoElements',
  'previewAudioElements',
  'projectAssetDurationSeconds',
  'commitTimeline',
  'queueOperations',
  'operationId',
  'ElMessage',
  'messageOf',
  'liveMessage',
  'props',
  'project',
  'pendingOperations',
  'taskDirty',
  'manualSave',
  'pausePlayback',
  'videoTrack',
  'workflowFinalizing',
  'resolvedTaskId',
  'taskWorkflow',
  'workflowStageBaseline',
  'exportVideoProfile',
  'previewFps',
  'contentApi',
  'workflowCompletionJob',
  'editorDisposed',
  'emit',
  'isTerminalJob',
  'setTimeout',
  'exporting',
  'exportJob',
  'validateAiBrief',
  'createLocalId',
  'exactPreviewOpen',
  'exactPreviewJob',
  'exactPreviewBusy',
  'exactPreviewUrl',
  'exactPreviewSubtitleUrl',
  'ownedObjectUrls',
  'timelineWebVtt',
  'URL',
  'Blob',
  'prepareExactPreview',
  'downloadMediaJob',
  'scheduleJobPoll',
  'aiVideoJob',
  'videoGenerating',
  'aiVideoSubmissionSequence',
  'notifiedAiVideoFailureId',
  'friendlyContentError',
  'refreshProjectAssets',
]
const { outputText, diagnostics } = ts.transpileModule(`
  export function createHarness(context: any) {
    const { ${bindings.join(', ')} } = context
    ${declarations.join('\n')}
    return { ${[...variableNames, ...functionNames].join(', ')} }
  }
`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext }, reportDiagnostics: true })
assert.deepEqual((diagnostics ?? []).filter(item => item.category === ts.DiagnosticCategory.Error), [])
const dataModule = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`
// eslint-disable-next-line no-unsanitized/method -- Actual local component AST declarations plus explicit test bindings.
const { createHarness } = await import(dataModule(outputText))

// Compile and mount the real right-hand form template; unrelated editor panels are not needed.
const template = descriptor.template.content
const formStart = template.indexOf('<ElForm label-position="top" size="small" class="compact-form">', template.indexOf('class="editor-sidebar editor-right"'))
assert.notEqual(formStart, -1)
const formTemplate = template.slice(formStart, template.indexOf('</ElForm>', formStart) + '</ElForm>'.length)
const compiled = compileTemplate({ source: formTemplate, filename, id: 'real-video-controls' })
assert.deepEqual(compiled.errors, [])
// eslint-disable-next-line no-unsanitized/method -- Vue's compiler generated this render function from the local SFC form.
const { render } = await import(dataModule(compiled.code.replace(/from ["']vue["']/gu, `from '${import.meta.resolve('vue')}'`)))

function hostNode(type, text = '') {
  return { type, text, props: {}, children: [], parent: null }
}
const renderer = createRenderer({
  createElement: type => hostNode(type),
  createText: text => hostNode('#text', text),
  createComment: text => hostNode('#comment', text),
  setText(target, text) {
    target.text = text
  },
  setElementText(target, text) {
    target.text = text
    target.children = []
  },
  patchProp(target, key, previous, next) {
    target.props[key] = next
  },
  parentNode: target => target.parent,
  nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  insert(target, parent, anchor = null) {
    if (target.parent) {
      target.parent.children.splice(target.parent.children.indexOf(target), 1)
    }
    target.parent = parent
    const position = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(position < 0 ? parent.children.length : position, 0, target)
  },
  remove(target) {
    if (target.parent) {
      target.parent.children.splice(target.parent.children.indexOf(target), 1)
    }
    target.parent = null
  },
})
const nodes = target => [target, ...target.children.flatMap(nodes)]

function setup(mode = 'full', options = {}) {
  let initial = addItem(createTimeline(), 'video', { id: 'image', duration_seconds: 5, metadata: { media_kind: 'image' } })
  initial = addItem(initial, 'video', { id: 'video', duration_seconds: 5, metadata: { media_kind: 'video', duration_seconds: 5 } })
  initial = addItem(initial, 'subtitle', { id: 'subtitle', content: '字幕一', metadata: { subtitle_mode: options.subtitle || 'burn' } })
  initial = addItem(initial, 'subtitle', { id: 'subtitle-two', content: '字幕二', metadata: { subtitle_mode: options.subtitle || 'burn' } })
  initial = addItem(initial, 'voice', { id: 'voice', duration_seconds: 14.7, metadata: { media_kind: 'audio' } })
  initial = addItem(initial, 'overlay', { id: 'overlay', metadata: { media_kind: 'image' } })
  initial.canvas.fps = options.fps || 60
  const timeline = ref(initial)
  const events = []
  const requests = []
  const messages = []
  const emissions = []
  const downloads = []
  const polls = []
  const createdBlobs = []
  const revokedUrls = []
  const objectUrlApi = {
    createObjectURL(blob) {
      const url = `blob:video-integration/${createdBlobs.length + 1}`
      createdBlobs.push({ url, blob })
      return url
    },
    revokeObjectURL(url) {
      revokedUrls.push(url)
    },
  }
  const pendingOperations = ref([])
  const project = ref({ id: `project-${mode}`, current_revision: 2, current_revision_id: 'revision-2', assets: [] })
  const taskDirty = ref(false)
  const stage = { key: 'video_preview', version: 4, selected_artifact_ids: {}, confirmed_artifact_ids: {} }
  const completedJob = () => ({ id: 'render-job', kind: 'video_project_render', status: 'succeeded', result: {
    project_id: project.value.id,
    revision_id: 'revision-3',
    profile: 'final-1080p',
    artifact_id: 'final-artifact',
    timeline_composition_version: 2,
    ...options.result,
  }, error: options.jobStatus === 'failed' ? '模拟渲染失败' : undefined })
  const context = {
    computed,
    timeline,
    selectedItemId: ref('image'),
    findItem,
    findTrack,
    isImageTimelineItem,
    trimPatch,
    updateItem,
    cloneTimeline,
    assetMediaKind,
    previewVideoElements: new Map(),
    previewAudioElements: new Map(),
    projectAssetDurationSeconds,
    commitTimeline(next, operation) {
      timeline.value = next
      pendingOperations.value.push(operation)
      return true
    },
    queueOperations(operations) {
      pendingOperations.value.push(...operations)
    },
    operationId: () => 'operation',
    ElMessage: Object.fromEntries(['error', 'warning', 'success'].map(level => [level, message => messages.push({ level, message })])),
    messageOf: error => error.message || String(error),
    liveMessage: ref(''),
    props: { workflowMode: options.workflowMode ?? true, initialTask: { mode } },
    project,
    pendingOperations,
    taskDirty,
    async manualSave() {
      events.push('save')
      if (options.saveFails) {
        return false
      }
      project.value = { ...project.value, current_revision: 3, current_revision_id: 'revision-3' }
      pendingOperations.value = options.unsaved ? [{ op_id: 'still-unsaved' }] : []
      taskDirty.value = Boolean(options.dirty)
      return true
    },
    pausePlayback: () => events.push('pause'),
    videoTrack: computed(() => findTrack(timeline.value, 'video')),
    workflowFinalizing: ref(false),
    resolvedTaskId: ref(`task-${mode}`),
    taskWorkflow: ref(undefined),
    workflowStageBaseline,
    exportVideoProfile: ref('1080p'),
    previewFps,
    workflowCompletionJob: ref(undefined),
    editorDisposed: false,
    emit: (...args) => emissions.push(args),
    isTerminalJob: isTerminalMediaJob,
    setTimeout: callback => queueMicrotask(callback),
    exporting: ref(false),
    exportJob: ref(undefined),
    validateAiBrief: () => true,
    createLocalId: prefix => `${prefix}-fresh`,
    exactPreviewOpen: ref(false),
    exactPreviewJob: ref(undefined),
    exactPreviewBusy: ref(false),
    exactPreviewUrl: ref(''),
    exactPreviewSubtitleUrl: ref(''),
    ownedObjectUrls: new Set(),
    timelineWebVtt,
    URL: objectUrlApi,
    Blob,
    prepareExactPreview() {},
    downloadMediaJob: async (job) => {
      downloads.push(job.id)
      return true
    },
    scheduleJobPoll: id => polls.push(id),
    aiVideoJob: ref(undefined),
    videoGenerating: ref(false),
    aiVideoSubmissionSequence: 0,
    notifiedAiVideoFailureId: ref(''),
    friendlyContentError: value => String(value),
    refreshProjectAssets: async () => {},
    contentApi: {
      getWorkflow: async () => ({ stages: [stage] }),
      async createVideoProjectRender(id, request) {
        events.push('render')
        requests.push({ type: 'render', id, request })
        if (options.createFails) {
          throw new Error('模拟请求失败')
        }
        return { ...completedJob(), status: 'running' }
      },
      async getJob() {
        events.push('poll')
        assert.deepEqual(emissions, [], '任务尚未返回成功前不能进入确认导出')
        if (options.editDuringRender) {
          pendingOperations.value.push({ op_id: 'new-edit' })
        }
        if (options.changeRevision) {
          project.value.current_revision_id = 'revision-4'
        }
        return { ...completedJob(), status: options.jobStatus || 'succeeded' }
      },
      async retryJob() {
        return { ...completedJob(), status: 'running' }
      },
      async createVideoProjectExport(id, request) {
        events.push('export')
        requests.push({ type: 'export', id, request })
        if (options.createFails) {
          throw new Error('模拟请求失败')
        }
        return { id: 'export-job', kind: 'video_project_export', status: 'running', result: {} }
      },
    },
  }
  const actual = createHarness(context)
  const root = hostNode('root')
  const app = renderer.createApp({ setup: () => ({ ...context, ...actual }), render })
  for (const name of ['ElForm', 'ElFormItem', 'ElInputNumber', 'ElInput', 'ElSelect', 'ElOption', 'ElSlider', 'ElSwitch', 'ElColorPicker']) {
    app.component(name, defineComponent({ setup: (props, { attrs, slots }) => () => h(name, attrs, slots.default?.()) }))
  }
  app.mount(root)
  return {
    context,
    actual,
    events,
    requests,
    messages,
    emissions,
    downloads,
    polls,
    createdBlobs,
    revokedUrls,
    options,
    fields: () => nodes(root).filter(node => node.type === 'ElFormItem').map(node => node.props.label),
    field(label) {
      const field = nodes(root).find(node => node.type === 'ElFormItem' && node.props.label === label)
      assert.ok(field, `${label} 控件应显示`)
      return nodes(field).find(node => ['ElInputNumber', 'ElInput', 'ElSelect', 'ElSlider', 'ElSwitch', 'ElColorPicker'].includes(node.type))
    },
    async select(id) {
      context.selectedItemId.value = id
      await nextTick()
    },
    async change(label, value) {
      this.field(label).props['onUpdate:modelValue'](value)
      await nextTick()
    },
    close() {
      app.unmount()
    },
  }
}

const tests = []
const test = (name, run) => tests.push({ name, run })

test('真实属性表单仅展示选中片段支持的字段，主轨开始不可改', async () => {
  const fixture = setup()
  try {
    assert.ok(fixture.fields().includes('画面适配'))
    assert.ok(!fixture.fields().includes('裁掉开头（秒）'))
    assert.ok(!fixture.fields().includes('音量'))
    assert.equal(fixture.field('开始（秒）').props.disabled, true)
    await fixture.select('video')
    assert.ok(fixture.fields().includes('裁掉结尾（秒）'))
    assert.ok(fixture.fields().includes('音量'))
    await fixture.select('subtitle')
    assert.ok(fixture.fields().includes('显示内容'))
    assert.ok(fixture.fields().includes('字幕字号'))
    assert.ok(!fixture.fields().includes('裁掉开头（秒）'))
    assert.ok(!fixture.fields().includes('画面适配'))
    await fixture.select('voice')
    assert.ok(fixture.fields().includes('配音语速'))
    assert.ok(fixture.fields().includes('裁掉开头（秒）'))
    assert.ok(!fixture.fields().includes('字幕字号'))
    await fixture.select('overlay')
    assert.ok(fixture.fields().includes('缩放'))
    assert.ok(!fixture.fields().includes('画面适配'))
    assert.ok(!fixture.fields().includes('显示内容'))
  }
  finally {
    fixture.close()
  }
})

test('真实控件事件写入裁切规范值和字幕样式，全片字幕方式同步所有字幕', async () => {
  const fixture = setup()
  try {
    await fixture.select('video')
    await fixture.change('裁掉开头（秒）', 1)
    assert.equal(fixture.actual.selectedItem.value.duration_seconds, 4)
    assert.equal(fixture.context.pendingOperations.value.at(-1).duration_seconds, 4)
    await fixture.change('画面适配', 'cover')
    assert.equal(fixture.actual.selectedItem.value.metadata.fit_mode, 'cover')
    await fixture.select('subtitle')
    await fixture.change('字幕字号', 48)
    await fixture.change('文字颜色', '#FF0000')
    assert.deepEqual(fixture.actual.selectedItem.value.metadata.subtitle_style, { font_size: 48, primary_color: '#FF0000' })
    await fixture.change('全片字幕方式', 'soft')
    assert.deepEqual(findTrack(fixture.context.timeline.value, 'subtitle').items.map(item => item.metadata.subtitle_mode), ['soft', 'soft'])
    assert.equal(fixture.field('字幕字号').props.disabled, true)
    assert.equal(fixture.field('文字颜色').props.disabled, true)
  }
  finally {
    fixture.close()
  }
})

for (const mode of ['full', 'video']) {
  test(`${mode} 完成先保存最新版本，真实字幕/FPS随请求发送，成功后才进入导出`, async () => {
    const fixture = setup(mode, { subtitle: 'soft', fps: 24 })
    try {
      assert.deepEqual(fixture.emissions, [])
      await fixture.actual.completeWorkflowVideo()
      assert.deepEqual(fixture.events, ['pause', 'save', 'render', 'poll'])
      const { request } = fixture.requests[0]
      assert.equal(request.expected_revision, 3)
      assert.equal(request.subtitle_mode, 'soft')
      assert.equal(request.fps, 24)
      assert.equal(request.include_audio, true)
      assert.match(request.idempotency_key, /^workflow-final-v2:/u)
      assert.deepEqual(fixture.emissions, [['workflowCompleted', { artifactId: 'final-artifact', baseline: { version: 4, selectedArtifactIds: [], confirmedArtifactIds: [] } }]])
      assert.equal(fixture.context.workflowFinalizing.value, false)
    }
    finally {
      fixture.close()
    }
  })

  test(`${mode} 导出请求使用已保存revision和真实字幕/FPS，仅任务成功才下载`, async () => {
    const fixture = setup(mode, { subtitle: 'soft', fps: 60 })
    try {
      await fixture.actual.submitProjectExport()
      assert.deepEqual(fixture.events, ['save', 'export'])
      assert.equal(fixture.requests[0].request.expected_revision, 3)
      assert.equal(fixture.requests[0].request.subtitle_mode, 'soft')
      assert.equal(fixture.requests[0].request.fps, 60)
      assert.deepEqual(fixture.downloads, [])
      assert.deepEqual(fixture.emissions, [])
      await fixture.actual.acceptMediaJobUpdate({ ...fixture.context.exportJob.value, status: 'failed' })
      assert.deepEqual(fixture.downloads, [])
      await fixture.actual.acceptMediaJobUpdate({ ...fixture.context.exportJob.value, status: 'succeeded' })
      assert.deepEqual(fixture.downloads, ['export-job'])
      assert.deepEqual(fixture.emissions, [])
    }
    finally {
      fixture.close()
    }
  })
}

for (const [label, options] of [
  ['保存失败', { saveFails: true }],
  ['保存后仍有待提交操作', { unsaved: true }],
  ['保存后任务仍为未保存', { dirty: true }],
  ['最终请求失败', { createFails: true }],
  ['最终渲染失败', { jobStatus: 'failed' }],
  ['渲染后新增编辑', { editDuringRender: true }],
  ['渲染期间版本变化', { changeRevision: true }],
  ['旧合成版本 v1', { result: { timeline_composition_version: 1 } }],
  ['错误revision', { result: { revision_id: 'revision-1' } }],
]) {
  test(`${label} 不进入确认导出并恢复完成按钮`, async () => {
    const fixture = setup('full', options)
    try {
      await fixture.actual.completeWorkflowVideo()
      assert.deepEqual(fixture.emissions, [])
      assert.equal(fixture.context.workflowFinalizing.value, false)
      assert.ok(fixture.messages.some(message => message.level === 'error'))
    }
    finally {
      fixture.close()
    }
  })
}

test('导出保存失败和请求失败都复位，可重试；未保存尾部编辑不能导出', async () => {
  for (const options of [{ saveFails: true }, { createFails: true }, { unsaved: true }, { dirty: true }]) {
    const fixture = setup('video', options)
    try {
      await fixture.actual.submitProjectExport()
      assert.equal(fixture.context.exporting.value, false)
      assert.deepEqual(fixture.downloads, [])
      if (!options.createFails) {
        assert.equal(fixture.requests.length, 0)
      }
      for (const key of Object.keys(options)) {
        fixture.options[key] = false
      }
      await fixture.actual.submitProjectExport()
      assert.equal(fixture.requests.at(-1).type, 'export')
    }
    finally {
      fixture.close()
    }
  }
})

test('恢复已持久化渲染时拒绝旧合成 v1，不误确认历史成片', async () => {
  const fixture = setup()
  try {
    const old = { id: 'old', kind: 'video_project_render', status: 'succeeded', result: { project_id: fixture.context.project.value.id, revision_id: 'revision-2', profile: 'final-1080p', timeline_composition_version: 1, artifact_id: 'old-artifact' } }
    await fixture.actual.continuePersistedWorkflowRender(old, { version: 1, selectedArtifactIds: [], confirmedArtifactIds: [] })
    assert.deepEqual(fixture.emissions, [])
    assert.ok(fixture.messages.some(message => message.level === 'error'))
  }
  finally {
    fixture.close()
  }
})

test('运行中无合成版本标记的同revision任务可恢复，成功后仍校验 v2 再确认', async () => {
  const fixture = setup()
  try {
    await fixture.context.manualSave()
    const running = { id: 'render-job', kind: 'video_project_render', status: 'running', result: {
      project_id: fixture.context.project.value.id,
      revision_id: 'revision-3',
      profile: 'final-1080p',
    } }
    assert.equal(fixture.actual.isFinalWorkflowRender(running, fixture.context.project.value), true)
    assert.equal(fixture.actual.isFinalWorkflowRender({ ...running, status: 'succeeded' }, fixture.context.project.value), false)
    const baseline = { version: 4, selectedArtifactIds: [], confirmedArtifactIds: [] }
    await fixture.actual.continuePersistedWorkflowRender(running, baseline)
    assert.equal(fixture.events.filter(item => item === 'poll').length, 1)
    assert.deepEqual(fixture.emissions, [['workflowCompleted', { artifactId: 'final-artifact', baseline }]])
  }
  finally {
    fixture.close()
  }
})

test('视频控件依据实际工程素材类型回退，未知媒体不误显示裁切或音量', async () => {
  const fixture = setup()
  try {
    const context = fixture.context
    context.timeline.value = updateItem(context.timeline.value, 'video', { project_asset_id: 'asset', metadata: { media_kind: '' } })
    context.project.value.assets.push({ id: 'asset', role: 'video', metadata: { media_kind: 'video' } })
    await fixture.select('video')
    assert.equal(fixture.actual.selectedCanTrim.value, true)
    assert.ok(fixture.fields().includes('裁掉结尾（秒）'))
    context.project.value.assets[0].metadata.media_kind = 'image'
    await nextTick()
    assert.equal(fixture.actual.selectedCanTrim.value, false)
    assert.ok(!fixture.fields().includes('音量'))
    context.project.value.assets = []
    await nextTick()
    assert.equal(fixture.actual.selectedCanTrim.value, false)
    assert.ok(!fixture.fields().includes('裁掉开头（秒）'))
  }
  finally {
    fixture.close()
  }
})

for (const { name, run } of tests) {
  await run()
  console.info(`PASS ${name}`)
}
console.info(`Video editor controls integration: ${tests.length} tests passed.`)
