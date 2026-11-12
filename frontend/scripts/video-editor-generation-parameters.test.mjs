import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// Standalone local regression script, no browser or paid API requests.
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, createRenderer, defineComponent, h, nextTick, reactive, ref } from 'vue'
import { compileTemplate, parse } from 'vue/compiler-sfc'
import { ContentGenerationError } from '../src/api/agent/contentGenerationErrors.ts'
import { imageCandidateParameters } from '../src/views/agent/content/video-editor/imageCandidateParameters.ts'
import { loadVideoGeneratorDraft, saveVideoGeneratorDraft } from '../src/views/agent/content/video-editor/videoGeneratorDraft.ts'

const source = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
assert.deepEqual(errors, [])
const ast = ts.createSourceFile('Workspace.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const variables = [
  'voiceForm',
  'subtitleForm',
  'imageForm',
  'aiVideoForm',
  'musicPrompt',
  'musicDuration',
  'musicFormat',
  'musicGenerating',
  'musicDurationRange',
  'imageGenerationUncertain',
  'imageResultRefreshing',
  'imageCandidateCapability',
  'imageCandidateSettings',
  'aiVideoDurations',
  'aiVideoDurationError',
]
const functions = [
  'refreshImageCandidateCapability',
  'generateImageCandidates',
  'refreshImageGenerationResult',
  'startNewImageBatch',
  'videoGeneratorDraftSnapshot',
  'restoreVideoGeneratorDraft',
  'syncAiVideoCapabilityDefaults',
  'generateVideoCandidate',
  'generateMusic',
]
const statements = [...variables, ...functions].map((name) => {
  const statement = ast.statements.find(item => ts.isFunctionDeclaration(item)
    ? item.name?.text === name
    : ts.isVariableStatement(item) && item.declarationList.declarations.some(declaration => declaration.name.getText(ast) === name))
  assert.ok(statement, `use actual production ${name}`)
  return statement.getText(ast)
})
const compiled = ts.transpileModule(`${statements.join('\n')}\nglobalThis.editor = { ${[...variables, ...functions].join(', ')} }`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const plain = value => JSON.parse(JSON.stringify(value))
const capability = (parameters = {}) => ({ capability: 'image-generation', enabled: true, mode: 'api', parameters: {
  ratios: ['adaptive', '1:1', '9:16'],
  sizes: ['2K', '3K', '4K'],
  custom_ratio: false,
  custom_size: false,
  defaults: { ratio: 'adaptive', size: '2K' },
  ...parameters,
} })

const formStart = descriptor.template.content.indexOf('<ElForm class="two-fields generator-field" label-position="top">')
assert.ok(formStart > 0)
const formSource = descriptor.template.content.slice(formStart, descriptor.template.content.indexOf('</ElForm>', formStart) + '</ElForm>'.length)
const template = compileTemplate({ source: formSource, filename: 'Workspace.vue', id: 'image-candidate-options' })
assert.deepEqual(template.errors, [])
const dataModule = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`
// eslint-disable-next-line no-unsanitized/method -- Compile only the real local SFC form.
const { render } = await import(dataModule(template.code.replace(/from ["']vue["']/gu, `from '${import.meta.resolve('vue')}'`)))

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

function fixture(mode = 'full', initialCapability = capability()) {
  const requests = []
  const messages = []
  const stored = new Map()
  const scope = {
    computed,
    reactive,
    ref,
    ContentGenerationError,
    imageCandidateParameters,
    loadVideoGeneratorDraft,
    props: { workflowMode: mode === 'full' },
    resolvedTaskId: ref(`task-${mode}`),
    project: ref({ id: `project-${mode}`, current_revision: 8 }),
    contentCapabilities: ref({ capabilities: initialCapability ? [initialCapability] : [] }),
    imageCapabilityLoading: ref(false),
    imageCapabilityError: ref(''),
    imageGenerating: ref(false),
    videoGenerating: ref(false),
    aiVideoSubmitting: ref(false),
    aiVideoSubmissionSequence: 0,
    editorDisposed: false,
    aiVideoJob: ref(undefined),
    MIN_AI_VIDEO_DURATION_SECONDS: 5,
    aiVideoCapabilityParameters: ref({ durations: [4, 5, 10, 15] }),
    aiVideoRatios: ref(['9:16']),
    aiVideoResolutions: ref(['720p']),
    aiVideoGenerateAudioSupported: ref(false),
    aiVideoReturnLastFrameSupported: ref(false),
    validateAiBrief: () => true,
    validateAiVideoReferences: () => true,
    manualSave: async () => true,
    ensureEditorAiService: async () => true,
    trackMediaJob: async () => {},
    createLocalId: prefix => `${prefix}-fixture`,
    registerGeneratedAssets: () => {},
    refreshProjectAssets: async () => {},
    messageOf: error => error.message,
    ElMessage: {
      warning: value => messages.push(value),
      error: value => messages.push(value),
      success: value => messages.push(value),
      info: value => messages.push(value),
    },
    videoGeneratorDraftHydrated: false,
    sessionStorage: { getItem: key => stored.get(key) || null, setItem: (key, value) => stored.set(key, value) },
    contentApi: {
      async capabilities() {
        return { capabilities: [capability()] }
      },
      async generateVideoProjectImages(id, payload) {
        requests.push({ id, ...plain(payload) })
        return []
      },
      async generateVideoProjectVideo(id, payload) {
        requests.push({ id, ...plain(payload) })
        return { id: 'local-job' }
      },
      async generateVideoProjectMusic(id, payload) {
        requests.push({ id, ...plain(payload) })
        return { id: 'local-music' }
      },
    },
  }
  vm.createContext(scope)
  vm.runInContext(compiled, scope)
  const editor = scope.editor
  editor.imageForm.prompt = '产品静物'
  const root = hostNode('root')
  const app = renderer.createApp({ setup: () => ({ ...scope, ...editor }), render })
  for (const name of ['ElForm', 'ElFormItem', 'ElSelect', 'ElOption']) {
    app.component(name, defineComponent({ setup: (props, { attrs, slots }) => () => h(name, attrs, slots.default?.()) }))
  }
  app.mount(root)
  return {
    scope,
    editor,
    requests,
    messages,
    unmount: () => app.unmount(),
    field(label) {
      const field = nodes(root).find(node => node.type === 'ElFormItem' && node.props.label === label)
      assert.ok(field, `${label} must be labeled in the actual SFC`)
      const select = nodes(field).find(node => node.type === 'ElSelect')
      assert.ok(select)
      return select
    },
    async change(label, value) {
      this.field(label).props['onUpdate:modelValue'](value)
      await nextTick()
    },
  }
}

test('图片选项严格来自当前模型，支持相同 nested custom 约定和模型默认', () => {
  const result = imageCandidateParameters(capability({ sizes: ['1024x1024'], ratios: { options: ['1:1'], allow_custom: true } }), { ratio: '7:3', size: '' })
  assert.equal(result.error, '')
  assert.equal(result.allowCustomRatio, true)
  assert.equal(result.allowCustomSize, false)
  assert.deepEqual(result.sizeOptions, [{ label: '1024×1024 px', value: '1024x1024' }])
  assert.equal(result.sizeOptions.some(option => option.value === '4K'), false)
  assert.equal(result.sizeDefaultLabel, '模型默认（2K）')
  assert.match(imageCandidateParameters(undefined, { ratio: '', size: '' }).error, /尚未读取/u)
  assert.match(imageCandidateParameters({ parameters: {} }, { ratio: '', size: '' }).error, /未声明完整/u)
})

test('自定义只有显式支持才有效，且校验格式、像素和宽高范围', () => {
  const model = capability({ custom_ratio: true, custom_size: true, minimum_pixels: 10000, maximum_pixels: 1000000, aspect_ratio_range: { min: 0.5, max: 2 } })
  assert.equal(imageCandidateParameters(model, { ratio: '2:1', size: '400x200' }).error, '')
  for (const selection of [{ ratio: '错误', size: '' }, { ratio: '1.5:1', size: '' }, { ratio: '01:1', size: '' }, { ratio: '3:1', size: '' }, { ratio: '', size: '5K' }, { ratio: '', size: '50x50' }, { ratio: '', size: '2000x2000' }]) {
    assert.notEqual(imageCandidateParameters(model, selection).error, '')
  }
  assert.match(imageCandidateParameters(model, { ratio: '1:1', size: '400x200' }).error, /尺寸与所选画面比例不一致/u)
  assert.equal(imageCandidateParameters(model, { ratio: '', size: '400x200' }).error, '')
  const result = imageCandidateParameters(capability({ sizes: ['2K'] }), { ratio: '', size: '4K' })
  assert.match(result.error, /不在当前图片模型支持范围/u)
  assert.equal(result.sizeOptions[0].disabled, true)
})

for (const mode of ['full', 'video']) {
  test(`${mode} 图片候选默认一张，保留用户主动选择的数量`, async () => {
    const item = fixture(mode)
    assert.equal(item.editor.imageForm.count, 1)
    await item.editor.generateImageCandidates()
    assert.equal(item.requests[0].count, 1)
    item.editor.imageForm.count = 2
    await item.editor.generateImageCandidates()
    assert.equal(item.requests[1].count, 2)
    item.unmount()
  })

  test(`${mode} 背景音乐默认十五秒，上限取十五秒与模型能力较小值`, async () => {
    const item = fixture(mode)
    assert.equal(item.editor.musicDuration.value, 15)
    assert.equal(item.editor.musicDurationRange.value.max, 15)
    item.editor.musicPrompt.value = '温暖的纯音乐'
    for (const maximum of [600, 30, 15]) {
      item.scope.contentCapabilities.value.capabilities = [{ capability: 'music-generation', parameters: { duration_seconds: { minimum: 1, maximum } } }]
      assert.equal(item.editor.musicDurationRange.value.max, 15)
    }
    for (const duration of [1, 15]) {
      item.editor.musicDuration.value = duration
      await item.editor.generateMusic()
      assert.equal(item.requests.at(-1).duration_seconds, duration)
    }
    for (const duration of [0, 15.1, 16, 30, 600]) {
      item.editor.musicDuration.value = duration
      await item.editor.generateMusic()
      assert.equal(item.requests.length, 2)
    }
    item.scope.contentCapabilities.value.capabilities = [{ capability: 'music-generation', parameters: { duration_seconds: { minimum: 1, maximum: 10 } } }]
    assert.equal(item.editor.musicDurationRange.value.max, 10)
    item.editor.musicDuration.value = 15
    await item.editor.generateMusic()
    assert.equal(item.requests.length, 2)
    item.unmount()
  })

  test(`${mode} 旧音乐草稿三十秒恢复为十五秒，不改变其他生成设置`, () => {
    const item = fixture(mode)
    item.editor.musicDuration.value = 30
    item.editor.imageForm.count = 3
    item.editor.aiVideoForm.durationSeconds = 10
    saveVideoGeneratorDraft(item.scope.sessionStorage, `task-${mode}`, item.editor.videoGeneratorDraftSnapshot())
    item.editor.restoreVideoGeneratorDraft()
    assert.equal(item.editor.musicDuration.value, 15)
    assert.equal(item.editor.imageForm.count, 3)
    assert.equal(item.editor.aiVideoForm.durationSeconds, 10)
    item.unmount()
  })

  test(`${mode} 共用工作区的实际下拉选择进入 API，清空使用模型默认`, async () => {
    const item = fixture(mode)
    assert.equal(item.editor.imageForm.size, '')
    assert.equal(item.editor.imageForm.ratio, '')
    assert.equal(item.field('输出清晰度').props['allow-create'], false)
    await item.change('输出清晰度', '3K')
    await item.change('画面比例', '9:16')
    await item.editor.generateImageCandidates()
    assert.equal(item.requests[0].id, `project-${mode}`)
    assert.equal(item.requests[0].size, '3K')
    assert.equal(item.requests[0].ratio, '9:16')
    await item.change('输出清晰度', undefined)
    await item.change('画面比例', undefined)
    await item.editor.generateImageCandidates()
    assert.equal(item.requests[1].size, undefined)
    assert.equal(item.requests[1].ratio, undefined)
    item.unmount()
  })

  test(`${mode} 草稿恢复保留图片参数和短音乐时长，模型切换后不降档且挡住不支持请求`, async () => {
    const item = fixture(mode)
    await item.change('输出清晰度', '4K')
    await item.change('画面比例', '9:16')
    item.editor.musicDuration.value = 1
    saveVideoGeneratorDraft(item.scope.sessionStorage, `task-${mode}`, item.editor.videoGeneratorDraftSnapshot())
    item.editor.imageForm.size = ''
    item.editor.imageForm.ratio = ''
    item.editor.musicDuration.value = 30
    item.scope.contentCapabilities.value = { capabilities: [capability({ sizes: ['2K'], ratios: ['1:1'] })] }
    item.editor.restoreVideoGeneratorDraft()
    assert.equal(item.editor.imageForm.size, '4K')
    assert.equal(item.editor.imageForm.ratio, '9:16')
    assert.equal(item.editor.musicDuration.value, 1)
    await item.editor.generateImageCandidates()
    assert.equal(item.requests.length, 0)
    assert.match(item.messages.at(-1), /不在当前图片模型支持范围/u)
    assert.equal(item.editor.imageForm.size, '4K')
    item.unmount()
  })
}

test('只移除圈定的三处说明，产品参考图和导入操作仍保留', () => {
  assert.doesNotMatch(descriptor.template.content, /默认加入配音，可选中后下移到背景音效|点击“浏览企业知识库”选择素材；确认后会直接加入对应轨道。|任务产品资料关联的产品图会由服务端自动作为参考图片|张并计入上方参考图片总上限，无需在工程素材中重复选择。/u)
  assert.match(descriptor.template.content, /aria-label="AI 视频产品资料关联参考图片"/u)
  assert.match(descriptor.template.content, /v-for="item in aiVideoProductVisualReferences"/u)
  assert.match(descriptor.template.content, /@click="openFileDialog"/u)
  assert.match(descriptor.template.content, /:max="musicDurationRange.max"/u)
})

test('图片能力未读取时先加载，读取失败不生成且可重新尝试', async () => {
  const item = fixture('full', undefined)
  item.scope.contentCapabilities.value = { capabilities: [] }
  let reads = 0
  item.scope.contentApi.capabilities = async () => {
    reads += 1
    if (reads === 1) {
      throw new Error('网络连接中断')
    }
    return { capabilities: [capability()] }
  }
  await item.editor.generateImageCandidates()
  assert.equal(item.requests.length, 0)
  assert.equal(item.scope.imageGenerating.value, false)
  assert.match(item.messages.at(-1), /参数读取失败/u)
  await item.editor.generateImageCandidates()
  assert.equal(reads, 2)
  assert.equal(item.requests.length, 1)
  item.unmount()
})

test('保存等待期间模型参数变化会再次校验，不发送已经不支持的图片参数', async () => {
  const item = fixture()
  item.editor.imageForm.size = '4K'
  item.scope.manualSave = async () => {
    item.scope.contentCapabilities.value = { capabilities: [capability({ sizes: ['2K'] })] }
    return true
  }
  await item.editor.generateImageCandidates()
  assert.equal(item.requests.length, 0)
  assert.equal(item.editor.imageForm.size, '4K')
  item.unmount()
})

test('多图等待中重复点击只提交一次，断线后刷新结果不会重新生成', async () => {
  const item = fixture()
  let rejectRequest
  let submissions = 0
  let refreshes = 0
  item.editor.imageForm.count = 8
  item.scope.contentApi.generateVideoProjectImages = async () => {
    submissions += 1
    return new Promise((resolve, reject) => {
      rejectRequest = reject
    })
  }
  item.scope.refreshProjectAssets = async () => {
    refreshes += 1
  }
  const pending = item.editor.generateImageCandidates()
  await nextTick()
  await item.editor.generateImageCandidates()
  assert.equal(submissions, 1)
  assert.equal(item.scope.imageGenerating.value, true)
  rejectRequest(new ContentGenerationError({ code: 'ECONNABORTED', message: 'timeout of 2460000ms exceeded' }))
  await pending
  assert.equal(item.scope.imageGenerating.value, false)
  assert.equal(item.editor.imageGenerationUncertain.value, true)
  assert.match(item.messages.at(-1), /结果尚未确认/u)
  await item.editor.refreshImageGenerationResult()
  assert.equal(refreshes, 1)
  assert.equal(submissions, 1)
  await item.editor.generateImageCandidates()
  assert.equal(refreshes, 2, '原生成按钮在结果不确定时仅刷新，不重新提交')
  assert.equal(submissions, 1)
  item.unmount()
})

for (const mode of ['full', 'video']) {
  test(`${mode} 新AI视频只开放真实能力中五秒起的选项，旧四秒草稿保留但不能提交`, async () => {
    const item = fixture(mode)
    assert.deepEqual(plain(item.editor.aiVideoDurations.value), [5, 10, 15])
    Object.assign(item.editor.aiVideoForm, { prompt: '产品视频', durationSeconds: 4, ratio: '9:16', resolution: '720p' })
    item.editor.syncAiVideoCapabilityDefaults()
    assert.equal(item.editor.aiVideoForm.durationSeconds, 4)
    await item.editor.generateVideoCandidate()
    assert.equal(item.requests.length, 0)
    assert.match(item.messages.at(-1), /至少 5 秒/u)
    item.editor.aiVideoForm.durationSeconds = 5
    await item.editor.generateVideoCandidate()
    assert.equal(item.requests[0].duration_seconds, 5)
    item.scope.aiVideoCapabilityParameters.value = { durations: [6, 9] }
    item.editor.aiVideoForm.durationSeconds = 5
    await item.editor.generateVideoCandidate()
    assert.equal(item.requests.length, 1, '五秒也必须确实在当前模型能力列表中')
    assert.deepEqual(plain(item.editor.aiVideoDurations.value), [6, 9])
    item.unmount()
  })
}
