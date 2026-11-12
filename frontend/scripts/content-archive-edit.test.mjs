/* eslint-disable no-new-func, test/no-import-node-test, no-throw-literal */
// Execute actual Vue actions/templates in Node. Element Plus rejects cancelled
// confirmations with the literal "cancel", which the harness intentionally uses.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileTemplate, parse } from 'vue/compiler-sfc'
import { renderToString } from 'vue/server-renderer'

const source = readFileSync(new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url), 'utf8')
const historySource = readFileSync(new URL('../src/views/agent/content/ContentVersionsView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const ast = ts.createSourceFile('ContentTaskView.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)

function declaration(name) {
  const statement = ast.statements.find(item => ts.isFunctionDeclaration(item)
    ? item.name?.text === name
    : ts.isVariableStatement(item) && item.declarationList.declarations.some(entry => entry.name.getText(ast) === name))
  assert.ok(statement, `${name} must come from the production component`)
  return statement.getText(ast)
}

function evaluate(names, bindings, returned = names) {
  const code = ts.transpileModule(names.map(declaration).join('\n'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText
  return new Function(...Object.keys(bindings), `${code}\nreturn { ${returned.join(', ')} };`)(...Object.values(bindings))
}

test('both video entries wait for archive status before mounting an autosaving editor', () => {
  const task = Vue.ref()
  const props = Vue.reactive({ taskId: 'original', stageKey: 'video_preview', mode: 'full' })
  const state = evaluate(['activeStageKey', 'currentMode', 'showVideoEditor', 'taskArchived', 'taskEditDraft'], {
    computed: Vue.computed,
    props,
    task,
  })
  assert.equal(state.showVideoEditor.value, false)
  for (const mode of ['full', 'video']) {
    for (const archive of [{ archived: true }, { status: 'archived' }]) {
      task.value = { id: 'original', mode, ...archive }
      assert.equal(state.showVideoEditor.value, false)
    }
    task.value = { id: 'draft', mode, status: 'draft', is_edit_draft: true }
    assert.equal(state.showVideoEditor.value, true)
    assert.equal(state.taskEditDraft.value, true)
    task.value = { id: 'other-user-task', mode, status: 'draft', can_edit: false }
    assert.equal(state.showVideoEditor.value, false, 'administrator read access must not mount an autosaving editor')
  }
  props.stageKey = 'export'
  assert.equal(state.showVideoEditor.value, false)
  props.taskId = ''
  props.mode = 'video'
  props.stageKey = 'configuration'
  task.value = undefined
  assert.equal(state.showVideoEditor.value, true)
})

function navigationHarness({ archived = true, cancel = false, fail = false } = {}) {
  const task = Vue.ref({ id: 'original', archived, status: archived ? 'confirmed' : 'draft' })
  const calls = []
  const bindings = {
    task,
    taskArchived: Vue.computed(() => task.value.archived),
    flushArtifactSelection: async () => true,
    ElMessageBox: { confirm: async (message) => {
      calls.push(['prompt', message])
      if (cancel) {
        throw 'cancel'
      }
    } },
    contentApi: { restoreTask: async (id) => {
      calls.push(['restore', id])
      if (fail) {
        throw new Error('unavailable')
      }
      return { id: 'draft' }
    } },
    router: { push: async (path) => {
      calls.push(['route', path])
    } },
    ElMessage: { error: message => calls.push(['error', message]) },
    messageOf: error => error.message,
  }
  return { ...evaluate(['navigateToStage'], bindings), task, calls }
}

test('editing an archive routes to a different draft ID without replacing its local snapshot', async () => {
  const { navigateToStage, task, calls } = navigationHarness()
  const before = JSON.stringify(task.value)
  await navigateToStage('video_preview')
  assert.equal(JSON.stringify(task.value), before)
  assert.deepEqual(calls.filter(item => item[0] !== 'prompt'), [
    ['restore', 'original'],
    ['route', '/agent/content/tasks/draft/stages/video_preview'],
  ])
  assert.match(calls[0][1], /最终确认后才另存为新任务/u)
  assert.match(calls[0][1], /未确认就退出，不会增加任务/u)
})

test('cancelled archive edits do not create a draft or change routes', async () => {
  const { navigateToStage, calls } = navigationHarness({ cancel: true })
  await navigateToStage('image')
  assert.deepEqual(calls.map(item => item[0]), ['prompt'])
})

test('failed draft creation leaves the archive in place and explains failure', async () => {
  const { navigateToStage, task, calls } = navigationHarness({ fail: true })
  await navigateToStage('copy')
  assert.equal(task.value.id, 'original')
  assert.equal(task.value.archived, true)
  assert.equal(calls.some(item => item[0] === 'route'), false)
  assert.equal(calls.at(-1)[0], 'error')
})

test('ordinary task navigation and archived export viewing do not clone tasks', async () => {
  for (const [archived, stage] of [[false, 'video_preview'], [true, 'export']]) {
    const { navigateToStage, calls } = navigationHarness({ archived })
    await navigateToStage(stage)
    assert.deepEqual(calls, [['route', `/agent/content/tasks/original/stages/${stage}`]])
  }
})

test('heading renders exactly one state: archive, edit draft, or configuration', async () => {
  const heading = descriptor.template.content.match(/<section v-if="!showVideoEditor" class="task-heading">[\s\S]*?<\/section>/u)?.[0]
  assert.ok(heading)
  const compiled = compileTemplate({ source: heading, filename: 'ArchiveHeading.vue', id: 'archive-test', compilerOptions: { mode: 'function' } })
  assert.deepEqual(compiled.errors, [])
  const render = new Function('Vue', compiled.code)(Vue)
  for (const [archived, draft, expected, absent] of [
    [true, false, '已归档', ['配置中', '修改草稿']],
    [false, true, '修改草稿', ['配置中', '已归档']],
    [false, false, '配置中', ['已归档', '修改草稿']],
  ]) {
    const component = Vue.defineComponent({
      render,
      setup: () => ({
        showVideoEditor: false,
        taskArchived: archived,
        taskEditDraft: draft,
        task: { name: '测试任务' },
        currentMode: 'full',
        modeLabels: { full: '完整内容创作' },
        taskWorkflowStatus: 'draft',
        displayLabel: () => '配置中',
        taskStatusLabels: {},
        router: { push() {} },
      }),
    })
    const app = Vue.createSSRApp(component)
    for (const name of ['ElTag', 'ElButton']) {
      app.component(name, { setup: (_, { slots }) => () => Vue.h('span', {}, slots.default?.()) })
    }
    const html = await renderToString(app)
    assert.match(html, new RegExp(expected, 'u'))
    for (const label of absent) {
      assert.doesNotMatch(html, new RegExp(label, 'u'))
    }
  }
})

test('old archive editor URLs redirect to export and history keeps the original record', () => {
  assert.match(declaration('loadCore'), /taskArchived.value && activeStageKey.value !== 'export'[\s\S]*?router.replace\(`\/agent\/content\/tasks\/\$\{task.value.id\}\/stages\/export`\)/u)
  const continueTask = historySource.slice(historySource.indexOf('async function continueTask'), historySource.indexOf('async function deleteActiveTask'))
  assert.match(continueTask, /contentApi.restoreTask\(task.id\)/u)
  assert.doesNotMatch(continueTask, /tasks.value\s*=|activeTaskId.value\s*=/u)
  assert.match(continueTask, /restored.mode === 'video' \? 'video_preview' : 'configuration'/u)
  assert.match(continueTask, /stages\/\$\{editStage\}/u)
})
