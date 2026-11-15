import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const dataModule = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`
const transpile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
let api = {}
let confirm = async () => true
const events = new Map()
globalThis.window = { addEventListener: (type, callback) => events.set(callback, type), removeEventListener: (type, callback) => events.delete(callback) }
globalThis.__accessTest = { call: (method, ...args) => api[method](...args), confirm: (...args) => confirm(...args) }
const moduleUrls = {
  'vue': import.meta.resolve('vue'),
  'element-plus': dataModule('export const ElMessage = { success() {} }; export const ElNotification = () => {}; export const ElMessageBox = { confirm: (...args) => globalThis.__accessTest.confirm(...args) };'),
  '@/api/agent/access': dataModule('export const contentAccessApi = Object.fromEntries(["me", "users", "update"].map(method => [method, (...args) => globalThis.__accessTest.call(method, ...args)]));'),
}
function replaceImports(text) {
  return text.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, specifier) => {
    assert.ok(moduleUrls[specifier], `Unexpected import: ${specifier}`)
    return `from ${quote}${moduleUrls[specifier]}${quote}`
  }).replace(/import\(\s*(['"])([^'"]+)\1\s*\)/gu, (match, quote, specifier) => {
    // Lazily loaded pages resolve to the same stubs as static imports.
    assert.ok(moduleUrls[specifier], `Unexpected dynamic import: ${specifier}`)
    return `import(${quote}${moduleUrls[specifier]}${quote})`
  })
}
const accessSource = readFileSync(new URL('../src/views/agent/content/localAccess.ts', import.meta.url), 'utf8')
moduleUrls['./localAccess'] = dataModule(replaceImports(transpile(accessSource)))
const source = readFileSync(new URL('../src/views/agent/content/ContentAccessView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'access-test', inlineTemplate: true })
// eslint-disable-next-line no-unsanitized/method -- Compiled local component with controlled dependency stubs.
const { default: View } = await import(dataModule(replaceImports(transpile(compiled.content))))
const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null })
const renderer = Vue.createRenderer({
  createElement: node,
  createText: text => node('#text', text),
  createComment: text => node('#comment', text),
  setText: (target, text) => {
    target.text = text
  },
  setElementText: (target, text) => {
    target.text = text
    target.children = []
  },
  parentNode: target => target.parent,
  nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  patchProp: (target, key, previous, next) => {
    target.props[key] = next
  },
  insert(target, parent, anchor = null) {
    if (target.parent) {
      target.parent.children.splice(target.parent.children.indexOf(target), 1)
    }
    target.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) {
      parent.children.push(target)
    }
    else {
      parent.children.splice(index, 0, target)
    }
  },
  remove(target) {
    if (target.parent) {
      target.parent.children.splice(target.parent.children.indexOf(target), 1)
    }
    target.parent = null
  },
})
const descendants = target => [target, ...target.children.flatMap(descendants)]
const textOf = target => target.text + target.children.map(textOf).join('')
async function settle() {
  for (let index = 0; index < 12; index++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
function mount(Component = View) {
  const root = node('root')
  const app = renderer.createApp(Component)
  for (const name of ['ElButton', 'ElDrawer', 'ElForm', 'ElFormItem', 'ElInput', 'ElAlert', 'ElCheckbox', 'ElCheckboxGroup', 'ElSwitch', 'ElTag', 'ElPagination', 'ElIcon', 'ElTabs', 'ElTabPane', 'ElDialog', 'ElRadio', 'ElRadioGroup', 'ElSelect', 'ElOption']) {
    app.component(name, Vue.defineComponent({ setup: (props, { slots, attrs }) => () => Vue.h(name, attrs, ['ElDrawer', 'ElDialog'].includes(name) && !attrs.modelValue ? [] : [...(slots.default?.() ?? []), ...(slots.footer?.() ?? [])]) }))
  }
  app.component('ElTable', Vue.defineComponent({ setup(props, { attrs, slots }) {
    Vue.provide('rows', () => attrs.data)
    return () => Vue.h('ElTable', attrs, slots.default?.())
  } }))
  app.component('ElTableColumn', Vue.defineComponent({ setup(props, { attrs, slots }) {
    const rows = Vue.inject('rows')
    return () => Vue.h('ElTableColumn', attrs, rows().map(row => Vue.h('cell', {}, slots.default?.({ row }) ?? row[attrs.prop])))
  } }))
  app.config.warnHandler = () => {}
  app.mount(root)
  return {
    root,
    all: type => descendants(root).filter(item => item.type === type),
    button: label => descendants(root).find(item => item.type === 'ElButton' && textOf(item).trim() === label),
    text: () => textOf(root),
    close: () => app.unmount(),
  }
}
const me = (manager = true) => ({ authentication_ready: true, subject: 'admin-1', display_name: '管理员', department: '信息部', is_project_admin: manager, permissions: manager ? ['models.manage', 'library.manage', 'rules.manage', 'audit.read'] : [], can_manage_users: manager, message: manager ? '' : '当前账号没有人员管理权限' })
const user = (overrides = {}) => ({ id: 'local-user-2', subject: 'account-2', display_name: '同名人员', department: '营销部', is_project_admin: false, permissions: [], revision: 3, last_seen_at: '2026-09-29T00:00:00Z', ...overrides })
const list = (items = [user()]) => ({ items, total: items.length, page: 1, page_size: 20 })

test('ordinary and development users never request protected personnel lists', async () => {
  for (const identity of [me(false), { ...me(false), authentication_ready: false, message: '请通过超级入口登录' }]) {
    let calls = 0
    api = { me: async () => identity, users: async () => {
      calls++
      return list()
    } }
    const view = mount()
    await settle()
    assert.equal(calls, 0)
    assert.equal(view.button('设置权限'), undefined)
    assert.ok(view.all('ElAlert').some(item => item.props.title === identity.message))
    view.close()
  }
})

test('administrator sees stable account identifiers, grants scoped permissions, and preserves choices after errors', async () => {
  const calls = []
  let fail = true
  api = {
    me: async () => me(),
    users: async (params) => {
      calls.push(['users', params])
      return list()
    },
    update: async (id, payload) => {
      calls.push(['update', id, payload])
      if (fail) {
        throw new Error('暂时无法保存')
      }
      return user({ permissions: payload.permissions, revision: 4 })
    },
  }
  const view = mount()
  await settle()
  assert.ok(view.text().includes('account-2'))
  view.button('设置权限').props.onClick()
  await settle()
  const checks = () => view.all('ElCheckboxGroup')[0]
  checks().props['onUpdate:modelValue'](['models.manage'])
  await view.button('保存权限').props.onClick()
  await settle()
  assert.deepEqual(checks().props.modelValue, ['models.manage'])
  assert.ok(view.all('ElAlert').some(item => item.props.title === '暂时无法保存'))
  fail = false
  await view.button('保存权限').props.onClick()
  await settle()
  assert.deepEqual(calls.filter(call => call[0] === 'update').at(-1), ['update', 'local-user-2', { expected_revision: 3, is_project_admin: false, permissions: ['models.manage'] }])
  assert.equal(view.all('ElDrawer')[0].props.modelValue, false)
  view.close()
})

test('promoting or demoting project admins requires confirmation and cancellation writes nothing', async () => {
  let writes = 0
  let prompt = ''
  api = { me: async () => me(), users: async () => list(), update: async () => {
    writes++
    return user()
  } }
  confirm = async (value) => {
    prompt = value
    // eslint-disable-next-line prefer-promise-reject-errors -- Element Plus rejects confirmations with the action string.
    return Promise.reject('cancel')
  }
  const view = mount()
  await settle()
  view.button('设置权限').props.onClick()
  await settle()
  view.all('ElSwitch')[0].props['onUpdate:modelValue'](true)
  await view.button('保存权限').props.onClick()
  await settle()
  assert.ok(prompt.includes('account-2'))
  assert.ok(prompt.includes('全部四项'))
  assert.equal(writes, 0)
  assert.equal(view.all('ElDrawer')[0].props.modelValue, true)
  view.close()
  confirm = async () => true
})

test('conflicting writes require reloading server revision before saving again', async () => {
  let changed = false
  api = {
    me: async () => me(),
    users: async () => list([user(changed ? { revision: 4, permissions: ['audit.read'] } : {})]),
    update: async () => {
      changed = true
      throw Object.assign(new Error('权限已被其他管理员修改'), { status: 409 })
    },
  }
  const view = mount()
  await settle()
  view.button('设置权限').props.onClick()
  await settle()
  view.all('ElCheckboxGroup')[0].props['onUpdate:modelValue'](['models.manage'])
  await view.button('保存权限').props.onClick()
  await settle()
  assert.equal(view.button('保存权限').props.disabled, true)
  assert.deepEqual(view.all('ElCheckboxGroup')[0].props.modelValue, ['models.manage'])
  await view.button('重新加载该人员').props.onClick()
  await settle()
  assert.deepEqual(view.all('ElCheckboxGroup')[0].props.modelValue, ['audit.read'])
  assert.equal(view.button('保存权限').props.disabled, false)
  view.close()
})

test('revocation on save clears the personnel list and open editor', async () => {
  let allowed = true
  api = { me: async () => me(allowed), users: async () => list(), update: async () => {
    allowed = false
    throw Object.assign(new Error('无人员管理权限'), { status: 403 })
  } }
  const view = mount()
  await settle()
  view.button('设置权限').props.onClick()
  await settle()
  await view.button('保存权限').props.onClick()
  await settle()
  assert.equal(view.button('设置权限'), undefined)
  assert.equal(view.all('ElDrawer')[0].props.modelValue, false)
  assert.ok(!view.text().includes('account-2'))
  view.close()
})

test('returning browser focus refreshes revoked authorization', async () => {
  let allowed = true
  api = { me: async () => me(allowed), users: async () => list() }
  const view = mount()
  await settle()
  assert.ok(view.button('设置权限'))
  allowed = false
  for (const [callback, type] of events) {
    if (type === 'focus') {
      callback()
    }
  }
  await settle()
  assert.equal(view.button('设置权限'), undefined)
  view.close()
  assert.equal(events.size, 0)
})

test('switching administrators discards the old editor and loads the new account in the same refresh', async () => {
  let subject = 'admin-1'
  api = {
    me: async () => ({ ...me(), subject }),
    users: async () => list([user({ subject: `${subject}-visible` })]),
  }
  const view = mount()
  await settle()
  view.button('设置权限').props.onClick()
  await settle()
  subject = 'admin-2'
  await view.button('刷新权限').props.onClick()
  await settle()
  assert.equal(view.all('ElDrawer')[0].props.modelValue, false)
  assert.ok(!view.text().includes('admin-1-visible'))
  assert.ok(view.text().includes('admin-2-visible'))
  view.close()
})

moduleUrls['@/api/agent/content'] = dataModule('export const contentApi = new Proxy({}, { get: (_, method) => (...args) => globalThis.__accessTest.call(method, ...args) });')
const emptyComponent = dataModule('export default { render() { return null } }')
const vueUrl = import.meta.resolve('vue')
moduleUrls['@element-plus/icons-vue'] = dataModule('const icon = { render() { return null } }; export const ChatDotRound = icon, Collection = icon, DataAnalysis = icon, Document = icon, Edit = icon, Film = icon, FolderOpened = icon, FullScreen = icon, Goods = icon, House = icon, List = icon, Monitor = icon, Picture = icon, Promotion = icon, Sell = icon, Stamp = icon, Tickets = icon, UploadFilled = icon, User = icon;')
moduleUrls['@vueuse/core'] = dataModule('export const useFullscreen = () => ({ toggle() {} });')
moduleUrls['@/auth/workspaceMode'] = dataModule('export const isStandaloneMode = false;')
moduleUrls['@/api/agent/catalog'] = dataModule('export const catalogApi = { overview: async () => ({ items: {} }) };')
moduleUrls['vue-router'] = dataModule('export const useRoute = () => ({ meta: {}, params: {}, query: {}, fullPath: "/agent/content" }); export const useRouter = () => ({ push() {} });')
moduleUrls['@/router/videoEditorRouteSession'] = dataModule('export const contentCreateViewKey = () => "test"; export const contentTaskViewKey = () => "test";')
moduleUrls['./content/contentWorkflow'] = dataModule('export const normalizeStageKey = value => value;')
moduleUrls['./content/serviceStatusPresentation'] = dataModule('export const serviceStatusPresentation = () => ({ label: "可用", type: "success" });')
moduleUrls['./content/localAccess'] = moduleUrls['./localAccess']
moduleUrls['./content/reviewBadge'] = dataModule(`import { ref } from "${import.meta.resolve('vue')}"; export const pendingReviewCount = ref(0);`)
// Online-trial accounts are off locally, so the enterprise access pages are what is tested.
moduleUrls['@/api/agent/trial'] = dataModule(`import { ref } from "${import.meta.resolve('vue')}"; export const trialMe = ref({ enabled: false }); export const loadTrialMe = async () => {};`)
for (const component of ['AssistantBall', 'ContentHomeView', 'ContentAccessView', 'ContentAuditView', 'ContentLibraryView', 'ContentServicesView', 'ContentTaskView', 'ContentVersionsView', 'ContentAutomationView', 'ContentChatView', 'CatalogContentsView', 'CatalogProductsView', 'CatalogPublishView', 'CatalogRecordsView', 'CatalogReviewView', 'CatalogListingWizard', 'TrialAccountsView']) {
  moduleUrls[`./content/${component}.vue`] = emptyComponent
}
async function compileView(path) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8').replace(/import ['"][^'"]+\.css['"]\s*/gu, '')
  const { descriptor } = parse(source)
  const compiled = compileScript(descriptor, { id: 'related-access-test', inlineTemplate: true })
  // eslint-disable-next-line no-unsanitized/method -- Local compiled SFC with controlled mocks.
  return (await import(dataModule(replaceImports(transpile(compiled.content))))).default
}
const Workbench = await compileView('../src/views/agent/ContentWorkbench.vue')

test('navigation exposes personnel only to project admins and audit separately to audit readers', async () => {
  for (const [identity, usersVisible, auditVisible] of [
    [me(false), false, false],
    [{ ...me(false), permissions: ['models.manage', 'audit.read'] }, false, true],
    [me(), true, true],
  ]) {
    api = { me: async () => identity, serviceStatus: async () => ({ overall: 'available' }) }
    const view = mount(Workbench)
    await settle()
    assert.equal(view.text().includes('人员与权限'), usersVisible)
    assert.equal(view.text().includes('操作记录'), auditVisible)
    view.close()
  }
})

moduleUrls['./ContentLibraryCatalog.vue'] = dataModule(`import { h } from "${vueUrl}"; export default { props: ['resources', 'canEdit'], setup: props => () => h('catalog', {}, props.resources.filter(row => props.canEdit?.(row)).map(row => h('editable-resource', {}, row.id))) };`)
moduleUrls['./ContentLibraryFilePreview.vue'] = emptyComponent
moduleUrls['./contentWorkflow'] = dataModule('export const friendlyContentError = value => String(value); export const formatDate = value => String(value || "");')
moduleUrls['./contentResourceCatalog'] = dataModule('export const categoryLabel = value => String(value || ""); export const isProductResource = () => false;')
moduleUrls['./libraryUpload'] = dataModule('export const inspectLibraryFile = () => ({}); export const libraryFileAccept = () => "";')
const Library = await compileView('../src/views/agent/content/ContentLibraryView.vue')

test('library edits follow ownership and library permission; rule editing follows its separate grant', async () => {
  for (const [grants, editable, rulesVisible] of [
    [[], ['own'], false],
    [['models.manage'], ['own'], false],
    [['library.manage'], ['own', 'other'], false],
    [['rules.manage'], ['own'], true],
  ]) {
    api = {
      me: async () => ({ ...me(false), permissions: grants }),
      listLibrary: async () => [{ id: 'own', owner_id: 'admin-1' }, { id: 'other', owner_id: 'someone-else' }],
      listLibraryCategories: async () => [],
      listRules: async () => [],
    }
    const view = mount(Library)
    await settle()
    assert.deepEqual(view.all('editable-resource').map(textOf), editable)
    assert.equal(Boolean(view.button('保存规则')), rulesVisible)
    view.close()
  }
})

const Audit = await compileView('../src/views/agent/content/ContentAuditView.vue')
test('audit readers fetch older server pages, filter exactly, and can inspect permission changes', async () => {
  const calls = []
  api = {
    me: async () => ({ ...me(false), permissions: ['audit.read'] }),
    auditLogs: async (params) => {
      calls.push(params)
      return Array.from({ length: params.page === 1 ? 20 : 1 }, (_, index) => ({ id: String(index), owner_id: 'admin-1', action: 'permissions.update', resource_type: 'user', resource_id: 'account-2', created_at: '2026-09-29T00:00:00Z', details: { before: [], after: ['models.manage'] } }))
    },
  }
  const view = mount(Audit)
  await settle()
  assert.ok(view.text().includes('models.manage'))
  view.button('下一页').props.onClick()
  await settle()
  assert.equal(calls.at(-1).page, 2)
  assert.equal(view.button('下一页').props.disabled, true)
  const inputs = view.all('ElInput')
  inputs[0].props['onUpdate:modelValue']('permissions.update')
  view.all('form')[0].props.onSubmit({ preventDefault() {} })
  await settle()
  assert.deepEqual(calls.at(-1), { page: 1, page_size: 20, action: 'permissions.update', resource_type: undefined })
  view.close()
})
