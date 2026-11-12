import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// Isolated lifecycle regressions; no user projects, uploads, or AI calls.
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { nextTick, reactive, ref } from 'vue'
import { parse } from 'vue/compiler-sfc'
import { createTimeline, normalizeTimeline } from '../src/views/agent/content/video-editor/timelineState.ts'

const source = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
assert.deepEqual(errors, [])
const ast = ts.createSourceFile('Workspace.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const names = ['applyTask', 'acknowledgeTaskSave', 'taskPayload', 'applyProject', 'ensureTaskAndProject', 'saveTaskInformation', 'initialize']
const declarations = names.map((name) => {
  const statement = ast.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === name)
  assert.ok(statement, `test actual ${name}`)
  return statement.getText(ast)
})
const initialTimeline = ast.statements.find(item => ts.isVariableStatement(item)
  && item.declarationList.declarations.some(value => value.name.getText(ast) === 'timeline'))
assert.ok(initialTimeline)
const taskWatcher = ast.statements.find(item => ts.isExpressionStatement(item)
  && ts.isCallExpression(item.expression) && item.expression.expression.getText(ast) === 'watch'
  && item.expression.arguments[0]?.getText(ast).includes('props.initialTask'))
assert.ok(taskWatcher)
const compiled = ts.transpileModule(`${initialTimeline.getText(ast)}\n${declarations.join('\n')}\nglobalThis.editor = { ${names.join(', ')}, timeline, updateInitialTask: ${taskWatcher.expression.arguments[1].getText(ast)} }`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const plain = value => JSON.parse(JSON.stringify(value))

function taskValue(overrides = {}) {
  return { id: 'new-task', mode: 'video', name: '旧名称', version: 1, knowledge_resource_ids: [], product_resource_id: null, keywords: [], ...overrides }
}

function deferred() {
  let resolve
  const promise = new Promise((done) => {
    resolve = done
  })
  return { resolve, promise }
}

function fixture(taskId = '', mode = 'video') {
  const requests = []
  const serverTimeline = { ...createTimeline(), canvas: { width: 1280, height: 720, aspect_ratio: '16:9', fps: 25 } }
  const projectValue = { id: 'project-1', current_revision: 1, revision: { timeline: serverTimeline } }
  const scope = {
    ref,
    nextTick,
    createTimeline,
    normalizeTimeline,
    props: { workflowMode: true, initialTask: undefined },
    DEFAULT_VIDEO_TASK_NAME: '新建快捷视频',
    resolvedTaskId: ref(taskId),
    resolvedTaskMode: ref(mode),
    taskVersion: ref(0),
    taskForm: reactive({ name: '', topic: '', product: '', audience: '', content_type: '短视频', style: '品牌宣传型', length: '中', requirements: '' }),
    taskKnowledgeResourceIds: ref([]),
    taskProductResourceId: ref(null),
    taskProductLabel: ref(''),
    taskKeywords: ref([]),
    savedTaskPayload: ref(''),
    taskDirty: ref(false),
    pendingCreatedTaskId: ref(''),
    pendingOperations: ref([]),
    project: ref(undefined),
    past: ref([]),
    future: ref([]),
    taskWorkflow: ref(undefined),
    taskArtifacts: ref([]),
    hydrated: ref(!taskId),
    saveInFlight: ref(false),
    saveState: ref(taskId ? 'loading' : 'saved'),
    saveMessage: ref(''),
    loadingError: ref(''),
    liveMessage: ref(''),
    messageOf: error => error.message,
    ElMessage: { warning() {} },
    hydrateVisibleMedia: async () => {},
    adoptPersistedTrackIds() {},
    restorePersistedVideoJobs: async () => {},
    observePreviewViewport() {},
    contentApi: {
      async createTask(payload) {
        requests.push({ type: 'create-task', ...plain(payload) })
        return taskValue(payload)
      },
      async createVideoProject(id) {
        requests.push({ type: 'create-project', id })
        return projectValue
      },
      async updateTask(id, payload) {
        requests.push({ type: 'update-task', id, ...plain(payload) })
        return taskValue({ ...payload, version: 2 })
      },
      getTask: async () => taskValue({ mode }),
      getVideoProject: async () => projectValue,
      getWorkflow: async () => ({ stages: [] }),
      listArtifacts: async () => [],
    },
  }
  vm.createContext(scope)
  vm.runInContext(compiled, scope)
  return { scope, editor: scope.editor, requests, projectValue }
}

test('空白进入不创建任务，画布初始值与服务器1280×720/16:9一致', async () => {
  const f = fixture()
  await f.editor.initialize()
  assert.deepEqual(f.requests, [])
  assert.equal(f.scope.hydrated.value, true)
  assert.deepEqual(plain(f.editor.timeline.value.canvas), f.projectValue.revision.timeline.canvas)
  f.scope.taskForm.name = '完整名称'
  await f.editor.ensureTaskAndProject()
  assert.deepEqual(plain(f.editor.timeline.value.canvas), f.projectValue.revision.timeline.canvas)
})

test('首次创建等待期间继续输入，不被创建响应覆盖且保留待保存状态', async () => {
  const f = fixture()
  const created = deferred()
  f.scope.taskForm.name = '新'
  f.scope.contentApi.createTask = () => created.promise
  const saving = f.editor.ensureTaskAndProject()
  f.scope.taskForm.name = '新春宣传最终名称'
  f.scope.taskForm.requirements = '保存等待期间填写的要求'
  created.resolve(taskValue({ name: '新', version: 3 }))
  await saving
  assert.equal(f.scope.resolvedTaskId.value, 'new-task')
  assert.equal(f.scope.taskVersion.value, 3)
  assert.equal(f.scope.taskForm.name, '新春宣传最终名称')
  assert.equal(f.scope.taskForm.requirements, '保存等待期间填写的要求')
  assert.equal(f.scope.taskDirty.value, true)
  await f.editor.saveTaskInformation()
  assert.equal(f.requests.at(-1).name, '新春宣传最终名称')
  assert.equal(f.requests.at(-1).requirements, '保存等待期间填写的要求')
  assert.equal(f.scope.taskDirty.value, false)
})

for (const mode of ['full', 'video']) {
  test(`${mode}：已有工程加载完成前不开放默认画布，按保存的比例一次性显示`, async () => {
    const f = fixture('existing-task', mode)
    const loaded = deferred()
    f.scope.contentApi.getVideoProject = () => loaded.promise
    const initializing = f.editor.initialize()
    assert.equal(f.scope.hydrated.value, false)
    assert.match(descriptor.template.content, /<ElSkeleton\s+v-if="!hydrated"/u)
    assert.match(descriptor.template.content, /<section\s+v-else\s+class="video-editor"/u)
    const savedTimeline = { ...createTimeline(), canvas: { width: 1080, height: 1080, aspect_ratio: '1:1', fps: 30 } }
    loaded.resolve({ ...f.projectValue, revision: { timeline: savedTimeline } })
    await initializing
    assert.equal(f.scope.hydrated.value, true)
    assert.deepEqual(plain(f.editor.timeline.value.canvas), savedTimeline.canvas)
  })

  test(`${mode}：自动保存响应与父页面同步均不能覆盖正在编辑的名称/产品`, async () => {
    const f = fixture('existing-task', mode)
    f.scope.hydrated.value = true
    f.scope.taskForm.name = '已提交名称'
    f.scope.taskDirty.value = true
    const updated = deferred()
    f.scope.contentApi.updateTask = () => updated.promise
    const saving = f.editor.saveTaskInformation()
    f.scope.taskForm.name = '仍在输入的新名称'
    f.scope.taskForm.product = '新产品'
    f.scope.taskProductResourceId.value = 'new-product'
    f.scope.taskProductLabel.value = '新产品'
    updated.resolve(taskValue({ id: 'existing-task', name: '已提交名称', mode, version: 6 }))
    await saving
    assert.equal(f.scope.taskForm.name, '仍在输入的新名称')
    assert.equal(f.scope.taskForm.product, '新产品')
    assert.equal(f.scope.taskVersion.value, 6)
    assert.equal(f.scope.taskDirty.value, true)
    f.editor.updateInitialTask(taskValue({ id: 'existing-task', name: '旧名称', mode, version: 5 }))
    assert.equal(f.scope.taskForm.name, '仍在输入的新名称')
    assert.equal(f.scope.taskVersion.value, 6)
    f.scope.taskDirty.value = false
    f.editor.updateInitialTask(taskValue({ id: 'existing-task', name: '旧名称', mode, version: 5 }))
    assert.equal(f.scope.taskForm.name, '仍在输入的新名称', 'an older parent read must not roll back an acknowledged save')
    assert.equal(f.scope.taskVersion.value, 6)
  })
}
