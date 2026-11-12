/* eslint-disable financial/no-division, no-new-func, test/no-import-node-test */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileTemplate, parse } from 'vue/compiler-sfc'
import { renderToString } from 'vue/server-renderer'

const sources = Object.fromEntries(['ContentVersionsView', 'ContentHomeView', 'ContentTaskView'].map(name => [
  name,
  parse(readFileSync(new URL(`../src/views/agent/content/${name}.vue`, import.meta.url), 'utf8')).descriptor,
]))

function evaluate(view, names, bindings) {
  const ast = ts.createSourceFile(`${view}.ts`, sources[view].scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const declarations = names.map((name) => {
    const statement = ast.statements.find(item => ts.isFunctionDeclaration(item)
      ? item.name?.text === name
      : ts.isVariableStatement(item) && item.declarationList.declarations.some(entry => entry.name.getText(ast) === name))
    assert.ok(statement, `${name} must come from the production component`)
    return statement.getText(ast)
  })
  const code = ts.transpileModule(declarations.join('\n'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText
  return new Function(...Object.keys(bindings), `${code}\nreturn { ${names.join(', ')} };`)(...Object.values(bindings))
}

test('read-only history hides task writes but retains confirmed download', async () => {
  const actions = sources.ContentVersionsView.template.content.match(/<div class="history-actions">[\s\S]*?<\/div>/u)?.[0]
  assert.ok(actions)
  const compiled = compileTemplate({ source: actions, filename: 'HistoryActions.vue', id: 'history-access', compilerOptions: { mode: 'function' } })
  assert.deepEqual(compiled.errors, [])
  const render = new Function('Vue', compiled.code)(Vue)
  for (const canEdit of [false, true, undefined]) {
    const activeTask = Vue.ref({ id: 'task', can_edit: canEdit, archived: true, status: 'confirmed' })
    const { activeTaskReadOnly } = evaluate('ContentVersionsView', ['activeTaskReadOnly'], { computed: Vue.computed, activeTask })
    const app = Vue.createSSRApp({
      render,
      setup: () => ({
        activeTask,
        activeTaskReadOnly,
        currentArtifacts: [{ id: 'artifact' }],
        workflowStatus: task => task.status,
        continueTask() {},
        exportTask() {},
        deleteActiveTask() {},
      }),
    })
    app.component('ElButton', { setup: (_, { slots }) => () => Vue.h('button', {}, slots.default?.()) })
    const html = await renderToString(app)
    assert.match(html, /导出已确认素材包/u)
    if (canEdit === false) {
      assert.doesNotMatch(html, /修改归档内容|继续配置|删除任务/u)
    }
    else {
      assert.match(html, /修改归档内容/u)
      assert.match(html, /删除任务/u)
    }
  }
})

test('read-only history actions refuse edits and deletion while downloads still work', async () => {
  const calls = []
  const activeTask = Vue.ref({ id: 'other-task', name: '其他人的内容', archived: true, can_edit: false })
  const actions = evaluate('ContentVersionsView', ['activeTaskReadOnly', 'continueTask', 'deleteActiveTask', 'exportTask', 'exportArtifact'], {
    computed: Vue.computed,
    activeTask,
    contentApi: {
      exportTask: async (id) => {
        calls.push(['export', id])
        return { blob: 'zip', fileName: 'all.zip' }
      },
      downloadArtifact: async (id) => {
        calls.push(['download', id])
        return { blob: 'video', fileName: 'video.mp4' }
      },
    },
    saveBlob: (blob, name) => calls.push(['save', blob, name]),
  })
  // No edit/delete dependency is supplied: neither action may proceed to a
  // confirmation, mutation, or editor navigation when the task is read-only.
  await actions.continueTask()
  await actions.deleteActiveTask()
  assert.deepEqual(calls, [])
  await actions.exportTask()
  await actions.exportArtifact({ id: 'video-result' })
  assert.deepEqual(calls, [
    ['export', 'other-task'],
    ['save', 'zip', 'all.zip'],
    ['download', 'video-result'],
    ['save', 'video', 'video.mp4'],
  ])
})

test('creator filter keeps the original owner identifier including legacy development tasks', () => {
  const tasks = Vue.ref([
    { id: 'old', owner_id: 'dev-user', name: '旧内容', topic: '', product: '', content_type: '', updated_at: '2026-09-28', status: 'archived' },
    { id: 'new', owner_id: '2102729372629487617', name: '新内容', topic: '', product: '', content_type: '', updated_at: '2026-10-06', status: 'draft' },
  ])
  const ownerFilter = Vue.ref('')
  const state = evaluate('ContentVersionsView', ['taskOwnerLabel', 'ownerOptions', 'taskUpdatedAt', 'filteredTasks'], {
    computed: Vue.computed,
    tasks,
    ownerFilter,
    query: Vue.ref(''),
    statusFilter: Vue.ref(''),
    dateRange: Vue.ref([]),
    parseContentDateTime: value => new Date(value),
    compareContentDateTimeDescending: (left, right) => new Date(right) - new Date(left),
  })
  assert.deepEqual(state.ownerOptions.value, [
    { value: 'dev-user', label: '历史开发账号（dev-user）' },
    { value: '2102729372629487617', label: '2102729372629487617' },
  ])
  assert.equal(state.filteredTasks.value.length, 2)
  ownerFilter.value = 'dev-user'
  assert.deepEqual(state.filteredTasks.value.map(item => item.id), ['old'])
})

test('workbench opens read-only tasks in history while editable tasks retain their workflow route', () => {
  const paths = []
  const { continueTask } = evaluate('ContentHomeView', ['continueTask'], {
    router: { push: path => paths.push(path) },
    normalizeStageKey: value => value,
  })
  for (const canEdit of [false, true, undefined]) {
    continueTask({ id: 'video-task', can_edit: canEdit, current_stage: 'video_preview' })
  }
  assert.deepEqual(paths, [
    { path: '/agent/content/versions', query: { taskId: 'video-task' } },
    '/agent/content/tasks/video-task/stages/video_preview',
    '/agent/content/tasks/video-task/stages/video_preview',
  ])
})

test('direct editor URLs redirect read-only tasks before populating editable forms', async () => {
  const paths = []
  const task = Vue.ref()
  const loading = Vue.ref(false)
  const { loadCore } = evaluate('ContentTaskView', ['loadCore'], {
    props: { taskId: 'other-video-task' },
    activeStageKey: Vue.ref('video_preview'),
    generationHistoryStages: new Set(),
    loading,
    pageError: Vue.ref(''),
    supportWarning: Vue.ref(''),
    task,
    contentApi: Object.fromEntries(['getTask', 'getWorkflow', 'listArtifacts', 'listLibrary', 'capabilities', 'serviceStatus', 'listTasks'].map(method => [
      method,
      async () => method === 'getTask' ? { id: 'other-video-task', can_edit: false, mode: 'video' } : [],
    ])),
    router: { replace: async path => paths.push(path) },
    fillTaskForm: () => assert.fail('read-only task must not initialize editing'),
  })
  await loadCore()
  assert.equal(loading.value, false)
  assert.deepEqual(paths, [{ path: '/agent/content/versions', query: { taskId: 'other-video-task' } }])
})

test('history deep link opens the requested task without rewriting its owner', async () => {
  const record = { id: 'legacy-task', owner_id: 'dev-user', can_edit: false }
  const opened = []
  const { openRequestedTaskHistory } = evaluate('ContentVersionsView', ['openRequestedTaskHistory'], {
    route: { query: { taskId: record.id } },
    tasks: Vue.ref([record]),
    openTaskHistory: async task => opened.push(task),
  })
  await openRequestedTaskHistory()
  assert.equal(opened[0].owner_id, 'dev-user')
  assert.equal(opened[0].can_edit, false)
})

test('adding generated content to another task only offers writable targets', async () => {
  const allTasks = Vue.ref([
    { id: 'source', mode: 'full', can_edit: true },
    { id: 'other-author', mode: 'video', can_edit: false },
    { id: 'collaborator', mode: 'full', can_edit: true },
    { id: 'old-response', mode: 'video' },
    { id: 'copy-task', mode: 'copy', can_edit: true },
  ])
  const errors = []
  const state = evaluate('ContentTaskView', ['linkTargetTasks', 'linkToFullTask'], {
    computed: Vue.computed,
    task: Vue.ref({ id: 'source' }),
    allTasks,
    linkTargetTaskId: Vue.ref('other-author'),
    linkArtifactIds: Vue.ref(['generated-image']),
    ElMessage: { error: message => errors.push(message) },
  })
  assert.deepEqual(state.linkTargetTasks.value.map(item => item.id), ['collaborator', 'old-response'])
  // A stale/manually selected target must still stop before the write request.
  await state.linkToFullTask()
  assert.deepEqual(errors, ['目标任务不存在或没有编辑权限，请重新选择'])
})

function artifactListAction(request) {
  const source = readFileSync(new URL('../src/api/agent/content.ts', import.meta.url), 'utf8')
  const ast = ts.createSourceFile('content.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const declaration = ast.statements.flatMap(item => ts.isVariableStatement(item) ? [...item.declarationList.declarations] : [])
    .find(item => item.name.getText(ast) === 'contentApi')
  const property = declaration.initializer.properties.find(item => item.name?.getText(ast) === 'listArtifacts')
  const code = ts.transpileModule(`const listArtifacts = ${property.initializer.getText(ast)}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText
  return new Function('request', `${code}\nreturn listArtifacts;`)(request)
}

test('history reads every artifact page including new versions after the first 50', async () => {
  for (const count of [0, 51, 100, 205]) {
    const rows = Array.from({ length: count }, (_, index) => ({ id: `version-${index}`, version: index + 1 }))
    const pages = []
    const list = artifactListAction(async ({ url, params }) => {
      assert.equal(url, '/api/v1/tasks/history-task/artifacts')
      assert.equal(params.page_size, 100)
      pages.push(params.page)
      return rows.slice((params.page - 1) * params.page_size, params.page * params.page_size)
    })
    assert.deepEqual(await list('history-task'), rows)
    assert.equal(pages.length, Math.floor(count / 100) + 1)
  }
})

test('an unavailable artifact page does not return misleading partial history', async () => {
  const list = artifactListAction(async ({ params }) => {
    if (params.page === 2) {
      throw new Error('page unavailable')
    }
    return Array.from({ length: 100 }, (_, index) => ({ id: String(index) }))
  })
  await assert.rejects(list('history-task'), /page unavailable/u)
})
