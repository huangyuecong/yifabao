import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// Local router/component simulation: no saved user tasks are created or changed.
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, createRenderer, defineComponent, h, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { parse } from 'vue/compiler-sfc'
import { beginVideoEditorRouteHandoff, CONTENT_AGENT_SHELL_KEY, contentRoutePageKey, contentTaskViewKey } from '../src/router/videoEditorRouteSession.ts'

const taskSource = readFileSync(new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url), 'utf8')
const pageSource = readFileSync(new URL('../src/views/agent/ContentWorkbench.vue', import.meta.url), 'utf8')
const appSource = readFileSync(new URL('../src/layout/components/AppView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(taskSource)
const ast = ts.createSourceFile('TaskView.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const handler = ast.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === 'handleVideoTaskCreated')
assert.ok(handler)
const showEditor = ast.statements.find(item => ts.isVariableStatement(item)
  && item.declarationList.declarations.some(value => value.name.getText(ast) === 'showVideoEditor'))
assert.ok(showEditor)
const compiledHandler = ts.transpileModule(handler.getText(ast), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const compiledVisible = ts.transpileModule(`${showEditor.getText(ast)}\nglobalThis.showVideoEditor = showVideoEditor`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function node(type, text = '') {
  return { type, text, props: {}, children: [], parent: null }
}
const renderer = createRenderer({
  createElement: type => node(type),
  createText: text => node('#text', text),
  createComment: text => node('#comment', text),
  setText: (target, text) => {
    target.text = text
  },
  setElementText: (target, text) => {
    target.text = text
  },
  patchProp: (target, key, previous, value) => {
    target.props[key] = value
  },
  parentNode: target => target.parent,
  nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  insert(target, parent, anchor = null) {
    target.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, target)
  },
  remove(target) {
    target.parent?.children.splice(target.parent.children.indexOf(target), 1)
    target.parent = null
  },
})

function routeValue(fullPath) {
  const match = fullPath.match(/^\/agent\/content\/tasks\/([^/]+)\/stages\/([^/?#]+)/u)
  const createVideo = fullPath.split(/[?#]/u)[0] === '/agent/content/create/video'
  return {
    fullPath,
    params: match ? { taskId: match[1], stageKey: match[2] } : {},
    meta: { contentView: match || createVideo ? 'task' : 'home', ...(createVideo ? { contentMode: 'video' } : {}) },
  }
}

function fixture() {
  contentRoutePageKey({ fullPath: '/reset', meta: {} })
  const route = reactive(routeValue('/agent/content/create/video'))
  const props = reactive({ taskId: '', stageKey: 'configuration', mode: 'video' })
  const task = ref(undefined)
  const replacements = []
  const scope = {
    computed,
    props,
    task,
    workflow: ref(undefined),
    artifacts: ref([]),
    taskArchived: ref(false),
    activeStageKey: computed(() => props.stageKey),
    currentMode: computed(() => task.value?.mode || props.mode),
    beginVideoEditorRouteHandoff,
    router: {
      currentRoute: ref(route),
      async replace(path) {
        replacements.push(path)
        navigate(path)
        await nextTick()
      },
    },
    contentApi: {
      getTask: async id => ({ id, mode: 'video', name: '已保存名称' }),
      getWorkflow: async () => ({ stages: [] }),
      listArtifacts: async () => [],
    },
  }
  function navigate(path) {
    Object.assign(route, routeValue(path))
    Object.assign(props, { taskId: route.params.taskId || '', stageKey: route.params.stageKey || 'configuration', mode: route.meta.contentMode || 'full' })
  }
  vm.createContext(scope)
  vm.runInContext(`${compiledHandler}\n${compiledVisible}`, scope)
  return { scope, route, props, replacements, navigate }
}

test('首次保存URL更新复用实际两层组件，编辑器挂载一次并保留正在输入的值', async () => {
  assert.match(appSource, /:key="contentRoutePageKey\(route\)"/u)
  assert.match(appSource, /contentWorkbenchName/u)
  assert.match(pageSource, /:key="contentTaskViewKey\(route.fullPath, `\$\{taskId\}:\$\{stageKey\}:\$\{currentMode\}`\)"/u)
  assert.match(pageSource, /<keep-alive/u)
  const f = fixture()
  const mounts = { page: 0, task: 0, editor: 0, destroyed: 0 }
  let input
  const Editor = defineComponent({ setup() {
    input = ref('输入中的完整名称')
    onMounted(() => {
      mounts.editor += 1
    })
    onUnmounted(() => {
      mounts.destroyed += 1
    })
    return () => h('input', { value: input.value })
  } })
  const Task = defineComponent({ setup() {
    onMounted(() => {
      mounts.task += 1
    })
    return () => f.scope.showVideoEditor.value ? h(Editor) : h('div')
  } })
  const Page = defineComponent({ setup() {
    onMounted(() => {
      mounts.page += 1
    })
    return () => f.route.meta.contentView === 'task'
      ? h(Task, { key: contentTaskViewKey(f.route.fullPath, `${f.props.taskId}:${f.props.stageKey}:${f.props.mode}`) })
      : h('div')
  } })
  const app = renderer.createApp({ setup: () => () => h(Page, { key: contentRoutePageKey(f.route) }) })
  app.mount(node('root'))
  await f.scope.handleVideoTaskCreated('saved-task')
  await nextTick()
  assert.equal(f.route.fullPath, '/agent/content/tasks/saved-task/stages/video_preview')
  assert.deepEqual(mounts, { page: 1, task: 1, editor: 1, destroyed: 0 })
  assert.equal(input.value, '输入中的完整名称')
  f.navigate('/agent/content')
  await nextTick()
  assert.equal(mounts.destroyed, 1)
  f.navigate('/agent/content/tasks/another-task/stages/video_preview')
  await nextTick()
  assert.equal(mounts.editor, 2, 'another task must mount a new editor')
  assert.equal(contentRoutePageKey(f.route), CONTENT_AGENT_SHELL_KEY)
  app.unmount()
})

test('新建快捷视频从进入起使用工作流完成按钮，已有full/video加载前不闪旧界面', () => {
  assert.match(taskSource, /:workflow-mode="currentMode === 'video' \|\| activeStageKey === 'video_preview'"/u)
  assert.match(taskSource, /v-if="taskId && stageKey === 'video_preview' && !task && !pageError"/u)
})

test('内容智能体各页共用外壳缓存键，仅精确新建视频交接任务视图', () => {
  contentRoutePageKey({ fullPath: '/reset', meta: {} })
  assert.equal(beginVideoEditorRouteHandoff('/agent/content/create/image', 'a', 'image'), false)
  assert.equal(beginVideoEditorRouteHandoff('/agent/content/create/full', 'a', 'full'), false)
  assert.equal(beginVideoEditorRouteHandoff('/agent/content/create/video', 'a', 'video'), true)
  const target = '/agent/content/tasks/a/stages/video_preview'
  assert.equal(contentTaskViewKey(target, 'a:video_preview:full'), 'video')
  assert.equal(contentRoutePageKey({ fullPath: '/agent/content/create/video', meta: {} }), CONTENT_AGENT_SHELL_KEY)
  assert.equal(contentRoutePageKey({ fullPath: target, meta: {} }), CONTENT_AGENT_SHELL_KEY)
  assert.equal(contentTaskViewKey(target, 'a:video_preview:full'), 'video')
  assert.equal(contentRoutePageKey({ fullPath: '/agent/content/library', meta: {} }), CONTENT_AGENT_SHELL_KEY)
  const exportPath = '/agent/content/tasks/a/stages/export'
  assert.equal(contentRoutePageKey({ fullPath: exportPath, meta: {} }), CONTENT_AGENT_SHELL_KEY)
  assert.equal(contentTaskViewKey(exportPath, 'a:export:full'), 'a:export:full', 'after leaving, returning or refresh uses canonical task key')
  beginVideoEditorRouteHandoff('/agent/content/create/video', 'a', 'video')
  assert.equal(contentRoutePageKey({ fullPath: `${target}?different=1`, meta: {} }), CONTENT_AGENT_SHELL_KEY)
  beginVideoEditorRouteHandoff('/agent/content/create/video', 'a', 'video')
  assert.equal(contentRoutePageKey({ fullPath: target, meta: { appType: 'micro-app', origin: 'micro-origin' } }), 'micro-origin')
  assert.equal(contentRoutePageKey({ fullPath: '/components', meta: {} }), '/components')
})

test('任务补充读取晚返回时，已离开工作台不会被拉回编辑器', async () => {
  const f = fixture()
  let resolveTask
  f.scope.contentApi.getTask = () => new Promise((resolve) => {
    resolveTask = resolve
  })
  const handoff = f.scope.handleVideoTaskCreated('saved-task')
  f.navigate('/agent/content')
  resolveTask({ id: 'saved-task', mode: 'video' })
  await handoff
  assert.deepEqual(f.replacements, [])
  assert.equal(f.scope.task.value, undefined)
})

test('可选补充读取失败仍保留已保存任务的canonical地址供刷新恢复', async () => {
  const f = fixture()
  f.scope.contentApi.getWorkflow = async () => {
    throw new Error('temporary')
  }
  await f.scope.handleVideoTaskCreated('saved-task')
  assert.deepEqual(f.replacements, ['/agent/content/tasks/saved-task/stages/video_preview'])
  assert.equal(contentRoutePageKey(f.route), CONTENT_AGENT_SHELL_KEY)
})
