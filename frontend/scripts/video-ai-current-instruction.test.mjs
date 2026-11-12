import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// Local-only request regression: no browser projects or paid generation calls.
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, reactive, ref } from 'vue'
import { parse } from 'vue/compiler-sfc'
import { formatContentGenerationError } from '../src/api/agent/contentGenerationErrors.ts'

const source = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
assert.deepEqual(errors, [])
const ast = ts.createSourceFile('Workspace.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const functions = ['generateVideoCandidate', 'acceptMediaJobUpdate', 'restorePersistedVideoJobs', 'mediaJobBelongsToProject']
const declarations = functions.map((name) => {
  const declaration = ast.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === name)
  assert.ok(declaration, `exercise actual ${name}`)
  return declaration.getText(ast)
})
const candidate = ast.statements.find(statement => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some(value => value.name.getText(ast) === 'aiVideoCandidateAsset'))
assert.ok(candidate)
const compiled = ts.transpileModule(`${declarations.join('\n')}\n${candidate.getText(ast)}\nglobalThis.generate = generateVideoCandidate; globalThis.candidate = aiVideoCandidateAsset`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const plain = value => JSON.parse(JSON.stringify(value))

function deferred() {
  let resolve
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve }
}

async function until(predicate) {
  for (let attempt = 0; attempt < 20 && !predicate(); attempt += 1) {
    await Promise.resolve()
  }
  assert.ok(predicate(), 'expected the local asynchronous handler to reach this phase')
}

function fixture(mode) {
  const requests = []
  const warnings = []
  const notices = []
  let requestNumber = 0
  const previousJob = { id: 'previous-failed-job', status: 'failed', result: { prompt: '旧提示词', reference_video_asset_ids: ['old-video'] } }
  const scope = {
    props: { workflowMode: mode === 'full' },
    computed,
    project: ref({ id: `project-${mode}`, current_revision: 4, assets: [{ id: 'old-asset', label: '旧带字幕视频' }] }),
    editorDisposed: false,
    aiVideoSubmitting: ref(false),
    aiVideoSubmissionSequence: 0,
    aiVideoForm: reactive({
      prompt: '  这个视频不要有字幕  ',
      referenceImageAssetIds: ['product-image', 'scene-image'],
      referenceVideoAssetIds: ['current-video'],
      durationSeconds: 7,
      ratio: '16:9',
      resolution: '480p',
      generateAudio: true,
      returnLastFrame: false,
    }),
    aiVideoDurationError: ref(''),
    aiVideoRatios: ref(['16:9', '9:16']),
    aiVideoResolutions: ref(['480p', '720p']),
    videoGenerating: ref(false),
    aiVideoJob: ref(previousJob),
    exactPreviewJob: ref(undefined),
    exportJob: ref(undefined),
    notifiedAiVideoFailureId: ref(''),
    liveMessage: ref(''),
    taskWorkflow: ref(undefined),
    isTerminalJob: job => ['succeeded', 'failed', 'cancelled'].includes(job.status),
    isFinalWorkflowRender: () => false,
    scheduleJobPoll: () => {},
    refreshProjectAssets: async () => {},
    friendlyContentError: value => value,
    formatContentGenerationError,
    ensureEditorAiService: async () => true,
    validateAiVideoReferences: () => true,
    validateAiBrief: () => true,
    manualSave: async () => true,
    syncAiVideoCapabilityDefaults: () => assert.fail('valid current options must not be replaced'),
    createLocalId: prefix => `${prefix}-${++requestNumber}`,
    trackMediaJob: async () => {
      scope.videoGenerating.value = false
    },
    messageOf: error => error.message,
    ElMessage: {
      info: message => notices.push(message),
      warning: message => warnings.push(message),
      error: message => warnings.push(message),
      success: message => notices.push(message),
    },
    contentApi: {
      async generateVideoProjectVideo(projectId, payload) {
        requests.push({ projectId, ...plain(payload) })
        return { id: `new-job-${requests.length}`, status: 'queued', result: {} }
      },
      async retryJob() {
        assert.fail('AI video retry must not submit the previous job parameters')
      },
    },
  }
  vm.createContext(scope)
  vm.runInContext(compiled, scope)
  return { scope, requests, warnings, notices, previousJob, generate: scope.generate }
}

test('AI 视频失败入口明确使用当前设置重新生成，精确预览和导出仍保留原重试', () => {
  const buttons = [...descriptor.template.content.matchAll(/<ElButton\b[\s\S]*?<\/ElButton>/gu)].map(match => match[0])
  const retry = buttons.find(button => button.includes('按当前设置重新生成'))
  assert.ok(retry, 'explain that changed settings are used')
  assert.match(retry, /aiVideoJob\.status === 'failed'/u)
  assert.match(retry, /@click="generateVideoCandidate"/u)
  assert.match(retry, /:loading="videoGenerating \|\| aiVideoSubmitting"/u)
  assert.doesNotMatch(descriptor.template.content, /retryMediaJob\(aiVideoJob\)/u)
  assert.match(descriptor.template.content, /retryMediaJob\(exactPreviewJob\)/u)
  assert.match(descriptor.template.content, /retryMediaJob\(exportJob\)/u)
  assert.match(descriptor.template.content, /v-if="!aiVideoSubmitting && aiVideoJob\?\.status === 'succeeded'"/u)
})

for (const mode of ['full', 'video']) {
  test(`${mode}：每次发送当前原始提示词和所选图片/视频，不混入旧文案或旧任务参数`, async () => {
    const f = fixture(mode)
    const oldJob = plain(f.previousJob)
    await f.generate()
    assert.equal(f.requests.length, 1)
    const first = f.requests[0]
    assert.equal(first.projectId, `project-${mode}`)
    assert.equal(first.prompt, '这个视频不要有字幕')
    assert.deepEqual(first.reference_image_asset_ids, ['product-image', 'scene-image'])
    assert.deepEqual(first.reference_video_asset_ids, ['current-video'])
    assert.equal(first.source_copy_artifact_id, null, 'the server resolves current copy separately')
    assert.equal(first.duration_seconds, 7)
    assert.equal(first.generate_audio, true)

    f.scope.aiVideoForm.prompt = '仅更换背景为室外，保留产品；不要字幕，也不要画外音'
    f.scope.aiVideoForm.referenceImageAssetIds.splice(0, 2, 'new-product-image')
    f.scope.aiVideoForm.referenceVideoAssetIds.splice(0, 1, 'new-reference-video')
    f.scope.aiVideoForm.generateAudio = false
    f.scope.project.value.current_revision = 5
    await f.generate()
    assert.equal(f.requests.length, 2)
    const second = f.requests[1]
    assert.equal(second.prompt, f.scope.aiVideoForm.prompt)
    assert.deepEqual(second.reference_image_asset_ids, ['new-product-image'])
    assert.deepEqual(second.reference_video_asset_ids, ['new-reference-video'])
    assert.equal(second.expected_revision, 5)
    assert.equal(second.generate_audio, false)
    assert.notEqual(first.idempotency_key, second.idempotency_key, 'new instructions create a distinct request')
    assert.deepEqual(first.reference_image_asset_ids, ['product-image', 'scene-image'])
    assert.deepEqual(first.reference_video_asset_ids, ['current-video'])
    assert.deepEqual(f.previousJob, oldJob, 'the failed job remains unchanged')
    assert.equal(f.scope.aiVideoJob.value.id, 'new-job-2')
    assert.deepEqual(f.warnings, [])
  })

  test(`${mode}：清空参考视频后重新生成不偷偷引用原视频`, async () => {
    const f = fixture(mode)
    f.scope.aiVideoForm.referenceVideoAssetIds = []
    await f.generate()
    assert.deepEqual(f.requests[0].reference_video_asset_ids, [])
    assert.equal(f.requests[0].prompt, '这个视频不要有字幕')
  })

  test(`${mode}：修改后仍执行原有参考素材校验，错误时不提交也不回退重试旧任务`, async () => {
    const f = fixture(mode)
    f.scope.validateAiVideoReferences = () => false
    await f.generate()
    assert.equal(f.requests.length, 0)
    assert.equal(f.scope.aiVideoJob.value.id, 'previous-failed-job')
    assert.equal(f.scope.aiVideoSubmitting.value, false)
  })

  test(`${mode}：保存和创建请求尚未返回时不重复提交，旧视频不冒充本次结果`, async () => {
    const f = fixture(mode)
    const save = deferred()
    const response = deferred()
    const oldAssets = plain(f.scope.project.value.assets)
    f.scope.aiVideoJob.value = { id: 'old-success', status: 'succeeded', result: { project_asset_id: 'old-asset' } }
    f.scope.manualSave = () => save.promise
    f.scope.contentApi.generateVideoProjectVideo = async (projectId, payload) => {
      f.requests.push({ projectId, ...plain(payload) })
      return response.promise
    }
    const first = f.generate()
    assert.equal(f.scope.aiVideoSubmitting.value, true)
    await f.generate()
    assert.equal(f.requests.length, 0)
    save.resolve(true)
    await until(() => f.requests.length === 1)
    assert.equal(f.scope.aiVideoJob.value, undefined, 'hide old result before awaiting creation')
    assert.equal(f.scope.candidate.value, undefined)
    await f.generate()
    assert.equal(f.requests.length, 1)
    await f.scope.acceptMediaJobUpdate({ id: 'old-success', status: 'succeeded', result: { project_asset_id: 'old-asset' } })
    assert.equal(f.scope.aiVideoJob.value, undefined, 'old poll cannot reclaim the panel')
    response.resolve({ id: 'current-job', status: 'queued', result: {} })
    await first
    assert.equal(f.scope.aiVideoJob.value.id, 'current-job')
    assert.deepEqual(plain(f.scope.project.value.assets), oldAssets, 'history assets are never removed')
    assert.equal(f.scope.aiVideoSubmitting.value, false)
  })

  test(`${mode}：历史任务列表晚返回不能覆盖已提交的新任务`, async () => {
    const f = fixture(mode)
    const history = deferred()
    f.scope.contentApi.listVideoProjectJobs = () => history.promise
    const restored = f.scope.restorePersistedVideoJobs(f.scope.project.value)
    await f.generate()
    history.resolve([{
      id: 'old-success',
      kind: 'video_project_ai_generation',
      status: 'succeeded',
      result: { project_id: `project-${mode}`, project_asset_id: 'old-asset' },
    }])
    await restored
    assert.equal(f.scope.aiVideoJob.value.id, 'new-job-1')
    assert.equal(f.scope.candidate.value, undefined)
    assert.equal(f.scope.liveMessage.value, '')
  })

  test(`${mode}：迟到的旧任务失败或成功不改变新任务状态和候选`, async () => {
    const f = fixture(mode)
    f.scope.aiVideoJob.value = { id: 'current-job', status: 'running', result: {} }
    f.scope.videoGenerating.value = true
    for (const status of ['failed', 'succeeded']) {
      await f.scope.acceptMediaJobUpdate({ id: 'old-job', status, error: '旧失败', result: { project_asset_id: 'old-asset' } })
    }
    assert.equal(f.scope.aiVideoJob.value.id, 'current-job')
    assert.equal(f.scope.aiVideoJob.value.status, 'running')
    assert.equal(f.scope.videoGenerating.value, true)
    assert.equal(f.scope.candidate.value, undefined)
    assert.deepEqual(f.warnings, [])
    assert.deepEqual(f.notices, [])
  })

  test(`${mode}：旧成功任务的素材刷新晚完成时不提示新任务已经完成`, async () => {
    const f = fixture(mode)
    const refresh = deferred()
    f.scope.aiVideoJob.value = { id: 'old-job', status: 'running', result: {} }
    f.scope.refreshProjectAssets = () => refresh.promise
    const update = f.scope.acceptMediaJobUpdate({ id: 'old-job', status: 'succeeded', result: { project_asset_id: 'old-asset' } })
    await f.generate()
    refresh.resolve()
    await update
    assert.equal(f.scope.aiVideoJob.value.id, 'new-job-1')
    assert.equal(f.scope.liveMessage.value, '')
    assert.deepEqual(f.notices, [])
  })

  test(`${mode}：当前任务缺少结果素材时不能回退显示旧视频`, () => {
    const f = fixture(mode)
    f.scope.aiVideoJob.value = { id: 'current-job', status: 'succeeded', result: { project_asset_id: 'current-asset' } }
    assert.equal(f.scope.candidate.value, undefined)
    f.scope.project.value.assets.push({ id: 'current-asset', label: '本次视频' })
    assert.equal(f.scope.candidate.value.id, 'current-asset')
  })

  test(`${mode}：创建请求在离开编辑器后返回，不更新已销毁页面`, async () => {
    const f = fixture(mode)
    const response = deferred()
    f.scope.contentApi.generateVideoProjectVideo = () => response.promise
    const creating = f.generate()
    await until(() => f.scope.videoGenerating.value)
    f.scope.editorDisposed = true
    response.resolve({ id: 'late-job', status: 'queued', result: {} })
    await creating
    assert.equal(f.scope.aiVideoJob.value, undefined)
    assert.equal(f.scope.aiVideoSubmitting.value, false)
  })
}
