import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// Local regression tests use deferred requests, never a real AI service.
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, ref, watch } from 'vue'
import { parse } from 'vue/compiler-sfc'
import { hasMaterialTransformChange } from '../src/views/agent/content/contentWorkflow.ts'

const source = readFileSync(new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
assert.deepEqual(errors, [])
const ast = ts.createSourceFile('ContentTaskView.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const variables = [
  'busy',
  'task',
  'editDialogOpen',
  'artifactEditorSession',
  'editingArtifact',
  'artifactDraft',
  'transformOperation',
  'transformInstruction',
  'imageRevisionInstruction',
  'imageRevisionNotice',
  'hasArtifactDraftChanges',
]
const functions = ['copyContentWithConfirmedTitle', 'openArtifactEditor', 'saveArtifactEdit', 'transformCopy']
const statements = [...variables, ...functions].map((name) => {
  const statement = ast.statements.find(item => ts.isFunctionDeclaration(item)
    ? item.name?.text === name
    : ts.isVariableStatement(item) && item.declarationList.declarations.some(declaration => declaration.name.getText(ast) === name))
  assert.ok(statement, `test the actual production ${name}`)
  return statement.getText(ast)
})
const dialogWatch = ast.statements.find(item => ts.isExpressionStatement(item)
  && ts.isCallExpression(item.expression)
  && item.expression.expression.getText(ast) === 'watch'
  && item.expression.arguments[0]?.getText(ast) === 'editDialogOpen')
const beforeUnmount = ast.statements.find(item => ts.isExpressionStatement(item)
  && ts.isCallExpression(item.expression)
  && item.expression.expression.getText(ast) === 'onBeforeUnmount')
assert.ok(dialogWatch, 'use the real synchronous dialog-close watcher')
assert.ok(beforeUnmount, 'use the real unmount invalidation')
const compile = text => ts.transpileModule(text, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const editorCode = compile(`${statements.join('\n')}\n${dialogWatch.getText(ast)}\n${beforeUnmount.getText(ast)}
globalThis.editor = { ${[...variables.filter(name => name !== 'artifactEditorSession'), ...functions].join(', ')} }`)

// Extract the real API wrapper too: payload assertions exercise its body mapping.
const apiSource = readFileSync(new URL('../src/api/agent/content.ts', import.meta.url), 'utf8')
const apiAst = ts.createSourceFile('content.ts', apiSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const apiDeclaration = apiAst.statements.filter(ts.isVariableStatement)
  .flatMap(statement => [...statement.declarationList.declarations])
  .find(declaration => declaration.name.getText(apiAst) === 'contentApi')
assert.ok(apiDeclaration && ts.isObjectLiteralExpression(apiDeclaration.initializer))
const previewProperty = apiDeclaration.initializer.properties.find(property => property.name?.getText(apiAst) === 'previewTransformCopy')
assert.ok(previewProperty && ts.isPropertyAssignment(previewProperty))
const apiCode = compile(`globalThis.previewTransformCopy = ${previewProperty.initializer.getText(apiAst)}`)
const plain = value => JSON.parse(JSON.stringify(value))

function deferred() {
  let resolve
  let reject
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
}

function artifact(id = 'copy-7', content = { title: '原始标题', body: '原始正文' }, extra = {}) {
  return { id, task_id: 'source-task', kind: 'copy', version: 7, content: plain(content), ...extra }
}

function fixture(t, initial = artifact()) {
  const requests = []
  const pending = []
  const patches = []
  const comparisons = []
  const messages = []
  const mutations = { flush: 0, refresh: 0, errors: 0 }
  const hooks = []
  function capture(config) {
    requests.push(plain(config))
    const request = deferred()
    pending.push(request)
    return request.promise
  }
  // AI previews go through generationRequest (generation error wrapping); others through request.
  const apiScope = { request: capture, generationRequest: capture }
  vm.createContext(apiScope)
  vm.runInContext(apiCode, apiScope)
  const scope = {
    ref,
    computed,
    watch,
    onBeforeUnmount: callback => hooks.push(callback),
    confirmedTopicTitle: ref('已确认的品牌标题'),
    mediaUrls: {},
    URL: { revokeObjectURL() {} },
    artifactSelectionQueue: { flush: async () => {} },
    loadArtifactPreview: async () => assert.fail('text preview must not load image media'),
    contentApi: {
      previewTransformCopy: apiScope.previewTransformCopy,
      async patchArtifact(id, content, version, targetTaskId) {
        patches.push({ id, content: plain(content), version, targetTaskId })
        return artifact(`${id}-saved`, content, { version: version + 1 })
      },
    },
    flushArtifactSelection: async () => {
      mutations.flush += 1
      return true
    },
    refreshWorkflowAndArtifacts: async () => {
      mutations.refresh += 1
    },
    handleWorkflowMutationError: async () => {
      mutations.errors += 1
    },
    messageOf: error => error.message,
    hasMaterialTransformChange(...args) {
      comparisons.push(plain(args))
      return hasMaterialTransformChange(...args)
    },
    ElMessage: Object.fromEntries(['success', 'error', 'warning', 'info'].map(kind => [kind, text => messages.push({ kind, text })])),
  }
  const effects = effectScope()
  vm.createContext(scope)
  effects.run(() => vm.runInContext(editorCode, scope))
  t.after(() => effects.stop())
  const editor = scope.editor
  editor.task.value = { id: 'editing-task' }
  editor.openArtifactEditor(initial)
  return {
    editor,
    scope,
    requests,
    pending,
    patches,
    comparisons,
    messages,
    mutations,
    dispose: () => hooks.forEach(callback => callback()),
    assertReadOnly() {
      assert.equal(patches.length, 0, 'AI preview must not create a version')
      assert.deepEqual(mutations, { flush: 0, refresh: 0, errors: 0 }, 'preview must not mutate or refresh selection/workflow')
    },
  }
}

test('手工修改后 AI 预览通过真实 API 包装器提交草稿，不保存、不刷新、不改版本', async (t) => {
  const original = artifact()
  const f = fixture(t, original)
  f.editor.artifactDraft.value.body = '这是尚未保存的手工正文'
  f.editor.transformOperation.value = 'expand'
  f.editor.transformInstruction.value = '扩写正文并保持品牌事实'
  const run = f.editor.transformCopy()
  assert.equal(f.requests.length, 1)
  assert.deepEqual(f.requests[0], {
    url: '/api/v1/artifacts/copy-7/transform-copy',
    method: 'POST',
    data: {
      operation: 'expand',
      instruction: '扩写正文并保持品牌事实',
      preview: true,
      draft_content: { title: '已确认的品牌标题', body: '这是尚未保存的手工正文' },
      expected_version: 7,
    },
  })
  f.pending[0].resolve({ content: { title: 'AI 临时标题', body: 'AI 扩写后的全新正文' } })
  await run
  assert.deepEqual(plain(f.editor.artifactDraft.value), { title: '已确认的品牌标题', body: 'AI 扩写后的全新正文' })
  assert.deepEqual(plain(f.editor.editingArtifact.value), original)
  assert.equal(f.editor.busy.value, '')
  f.assertReadOnly()
})

test('连续预览基于上一轮可见草稿，源 ID 和版本不变，明确保存才新增一次', async (t) => {
  const f = fixture(t)
  f.editor.artifactDraft.value.body = '手工第一稿'
  for (const [index, expectedInput, output] of [[0, '手工第一稿', 'AI 第一稿'], [1, 'AI 第一稿', 'AI 第二稿']]) {
    const run = f.editor.transformCopy()
    assert.equal(f.requests[index].data.draft_content.body, expectedInput)
    assert.equal(f.requests[index].data.expected_version, 7)
    assert.equal(f.requests[index].url, '/api/v1/artifacts/copy-7/transform-copy')
    f.pending[index].resolve({ content: { body: output } })
    await run
    assert.equal(f.comparisons[index][1].body, expectedInput, 'material-change check must use the submitted draft')
    assert.equal(f.editor.editingArtifact.value.id, 'copy-7')
    assert.equal(f.editor.editingArtifact.value.version, 7)
    f.assertReadOnly()
  }
  const save = f.editor.saveArtifactEdit()
  await f.editor.saveArtifactEdit()
  await f.editor.transformCopy()
  await save
  assert.deepEqual(f.patches, [{ id: 'copy-7', version: 7, targetTaskId: undefined, content: { title: '已确认的品牌标题', body: 'AI 第二稿' } }])
  assert.deepEqual(f.mutations, { flush: 1, refresh: 1, errors: 0 })
  assert.equal(f.requests.length, 2, 'preview cannot run while saving')
  assert.equal(f.editor.editingArtifact.value.version, 8)
  await f.editor.saveArtifactEdit()
  assert.equal(f.patches.length, 1, 'unchanged saved content does not create another version')
})

test('预览等待期间连点只提交一次，保存被阻止；完成后按钮恢复', async (t) => {
  const f = fixture(t)
  f.editor.artifactDraft.value.body = '人工改过的正文'
  const first = f.editor.transformCopy()
  await f.editor.transformCopy()
  await f.editor.saveArtifactEdit()
  assert.equal(f.requests.length, 1)
  assert.equal(f.editor.busy.value, 'transform')
  f.assertReadOnly()
  f.pending[0].resolve({ content: { body: '预览完成的新正文' } })
  await first
  assert.equal(f.editor.busy.value, '')
})

test('预览失败保留全部草稿并可重试，不产生中间版本', async (t) => {
  const f = fixture(t)
  f.editor.artifactDraft.value = { title: '手工标题', body: '手工正文', custom: '保留字段' }
  const before = plain(f.editor.artifactDraft.value)
  const failed = f.editor.transformCopy()
  f.pending[0].reject(new Error('文案服务暂时不可用，请稍后重试'))
  await failed
  assert.deepEqual(plain(f.editor.artifactDraft.value), before)
  assert.equal(f.editor.busy.value, '')
  assert.equal(f.messages.at(-1).kind, 'error')
  const retry = f.editor.transformCopy()
  assert.deepEqual(f.requests[1].data, f.requests[0].data)
  f.pending[1].resolve({ content: { body: '恢复后的新文案' } })
  await retry
  assert.equal(f.editor.artifactDraft.value.body, '恢复后的新文案')
  f.assertReadOnly()
})

for (const target of ['same', 'other']) {
  for (const response of ['success', 'failure']) {
    test(`关闭重开${target === 'same' ? '同一文案' : '其他文案'}后，旧请求${response === 'success' ? '成功' : '失败'}不回填也不弹提示`, async (t) => {
      const original = artifact()
      const f = fixture(t, original)
      const run = f.editor.transformCopy()
      f.editor.editDialogOpen.value = false
      const next = target === 'same' ? original : artifact('copy-B', { body: 'B 的原始文案' })
      f.editor.openArtifactEditor(next)
      if (response === 'success') {
        f.pending[0].resolve({ content: { body: '过期的 A 结果' } })
      }
      else {
        f.pending[0].reject(new Error('已关闭会话的服务失败'))
      }
      await run
      assert.deepEqual(plain(f.editor.artifactDraft.value), next.content)
      assert.equal(f.editor.editingArtifact.value.id, next.id)
      assert.deepEqual(f.messages, [])
      assert.equal(f.editor.busy.value, '')
      f.assertReadOnly()
    })
  }
}

test('只关闭弹窗或离开页面后，旧预览不会回填隐藏草稿', async (t) => {
  for (const close of ['dialog', 'unmount']) {
    const f = fixture(t)
    const before = plain(f.editor.artifactDraft.value)
    const run = f.editor.transformCopy()
    if (close === 'dialog') {
      f.editor.editDialogOpen.value = false
    }
    else {
      f.dispose()
    }
    f.pending[0].resolve({ content: { body: '已过期的预览' } })
    await run
    assert.deepEqual(plain(f.editor.artifactDraft.value), before)
    assert.deepEqual(f.messages, [])
    f.assertReadOnly()
  }
})

test('预览期间新增手工修改会被保留，不让较早 AI 结果覆盖', async (t) => {
  const f = fixture(t)
  f.editor.artifactDraft.value.body = '提交前的正文'
  const run = f.editor.transformCopy()
  f.editor.artifactDraft.value.body = '等待期间刚修改的新正文'
  f.pending[0].resolve({ content: { body: '基于旧正文的 AI 结果' } })
  await run
  assert.equal(f.requests[0].data.draft_content.body, '提交前的正文')
  assert.equal(f.editor.artifactDraft.value.body, '等待期间刚修改的新正文')
  assert.match(f.messages.at(-1).text, /已保留你的修改/u)
  f.assertReadOnly()
})

test('操作和指令使用请求快照，未变化结果与本地草稿比较而不是持久化原文', async (t) => {
  const f = fixture(t)
  f.editor.artifactDraft.value.body = '上一轮预览已经存在的文案'
  f.editor.transformInstruction.value = '只扩写正文，保留标题'
  const run = f.editor.transformCopy()
  f.editor.transformInstruction.value = '修改标题并缩写'
  f.editor.transformOperation.value = 'shorten'
  f.pending[0].resolve({ content: { body: '上一轮预览已经存在的文案' } })
  await run
  assert.equal(f.requests[0].data.operation, 'rewrite')
  assert.equal(f.comparisons[0][3], '只扩写正文，保留标题')
  assert.match(f.messages.at(-1).text, /AI 未按要求修改内容/u)
  assert.equal(f.editor.artifactDraft.value.body, '上一轮预览已经存在的文案')
  f.assertReadOnly()
})

test('引用文案带目标任务，当前固定标题贯穿候选、选中快照、预览和保存', async (t) => {
  const candidate = { title: '引用的旧标题', body: '引用原始正文' }
  const initial = artifact('linked-copy', { title: candidate.title, candidates: [candidate], selected_candidate: candidate, selected_candidate_index: 0 }, { linked: true })
  const original = plain(initial)
  const f = fixture(t, initial)
  const run = f.editor.transformCopy()
  const input = f.requests[0].data
  assert.equal(input.target_task_id, 'editing-task')
  assert.equal(input.expected_version, 7)
  assert.equal(input.draft_content.title, '已确认的品牌标题')
  assert.equal(input.draft_content.candidates[0].title, '已确认的品牌标题')
  assert.equal(input.draft_content.selected_candidate.title, '已确认的品牌标题')
  const changed = { title: 'AI 自拟标题', body: '引用文案的 AI 修改正文' }
  f.pending[0].resolve({ content: { title: changed.title, candidates: [changed], selected_candidate: changed, selected_candidate_index: 0 } })
  await run
  assert.equal(f.editor.artifactDraft.value.candidates[0].title, '已确认的品牌标题')
  assert.equal(f.editor.artifactDraft.value.selected_candidate.title, '已确认的品牌标题')
  assert.deepEqual(initial, original, 'preview must not mutate the linked source')
  f.assertReadOnly()
  await f.editor.saveArtifactEdit()
  assert.equal(f.patches[0].targetTaskId, 'editing-task')
  assert.equal(f.patches[0].content.candidates[0].body, changed.body)
})

test('标题预览同样只用当前草稿，不套用文案固定标题规则', async (t) => {
  const initial = artifact('topic-3', { candidates: [{ title: '原始标题' }] }, { kind: 'topic', version: 3 })
  const f = fixture(t, initial)
  f.editor.artifactDraft.value.candidates[0].title = '手工调整的标题'
  const run = f.editor.transformCopy()
  assert.equal(f.requests[0].data.expected_version, 3)
  assert.deepEqual(f.requests[0].data.draft_content, { candidates: [{ title: '手工调整的标题' }] })
  f.pending[0].resolve({ content: { candidates: [{ title: 'AI 精炼后的标题' }] } })
  await run
  assert.equal(f.editor.artifactDraft.value.candidates[0].title, 'AI 精炼后的标题')
  assert.equal(f.editor.editingArtifact.value.version, 3)
  f.assertReadOnly()
})
