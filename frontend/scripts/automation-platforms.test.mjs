import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'
import { automationPurpose, compatiblePlatforms, executionMode, platformLimits, publishBlockReason, purposeExecutionMode, workflowForPurpose } from '../src/api/agent/automationPolicy.ts'

const catalog = [
  { id: 'xianyu', label: '闲鱼', purpose: 'listing', operations: ['publish', 'relist', 'delist'], execution_modes: ['browser'], requires_video: false, title_limit: 30, description_limit: 1000 },
  { id: 'douyin', label: '抖音', purpose: 'promotion', operations: ['publish'], execution_modes: ['browser'], requires_video: true, title_limit: 30, description_limit: 1000 },
  { id: 'xiaohongshu', label: '小红书', purpose: 'promotion', operations: ['publish'], execution_modes: ['browser'], requires_video: true, title_limit: 20, description_limit: 1000 },
  { id: 'kuaishou', label: '快手', purpose: 'promotion', operations: ['publish'], execution_modes: ['browser'], requires_video: true, title_limit: 30, description_limit: 500 },
]
const config = {
  name: '多平台宣传',
  purpose: 'promotion',
  execution_mode: 'android',
  device_id: null,
  operation: 'publish',
  source_mode: 'custom',
  custom_product_name: '测试商品',
  custom_product_description: '测试商品的真实特点',
  platforms: ['douyin', 'xiaohongshu', 'kuaishou'],
  accounts: {},
  image_ids: ['image-1'],
  condition: '',
  location: '',
  shipping_fee: 0,
  instructions: '',
  auto_publish: false,
  video_mode: 'slideshow',
}
const status = {
  browser: { configured: true, available: true, missing: [], platforms: {
    douyin: { configured: true, missing: [], accounts: [{ account_id: 'browser-account', label: '抖音账号', configured: true }] },
    xianyu: { configured: true, missing: [], accounts: [] },
  } },
  android: { configured: false, available: false, ready: false, missing: ['尚未配置安卓模拟器', 'App 发布适配器待接入'], devices: [], platforms: {}, adb_configured: false },
}
const workflow = { id: 'workflow-1', name: config.name, config, created_at: '2026-10-18T00:00:00Z', updated_at: '2026-10-18T00:00:00Z' }

test('multiple promotion platforms remain separate from listing and legacy mixed records', () => {
  assert.equal(automationPurpose(config), 'promotion')
  assert.equal(automationPurpose({ ...config, platforms: ['xianyu', 'douyin'] }), null)
  assert.equal(automationPurpose({ ...config, platforms: ['xianyu'] }), null)
  assert.equal(automationPurpose({ ...config, operation: 'delist' }), null)
  assert.equal(automationPurpose({ ...config, purpose: 'listing', platforms: ['xianyu'], operation: 'delist' }), 'listing')
  assert.equal(executionMode({}), 'browser')
})

test('purpose fixes the executor and migrating a workflow clears incompatible account and device bindings', () => {
  const original = { ...config, device_id: 'configured-device', accounts: { douyin: 'android-account' } }
  const browser = workflowForPurpose(original, 'promotion')
  assert.equal(purposeExecutionMode('promotion'), 'browser')
  assert.equal(purposeExecutionMode('listing'), 'browser')
  assert.deepEqual(browser.platforms, config.platforms)
  assert.deepEqual(browser.accounts, {})
  assert.equal(browser.device_id, null)
  assert.equal(original.device_id, 'configured-device')
  const listing = workflowForPurpose({ ...config, purpose: 'listing', platforms: ['xianyu'], device_id: 'old-device', accounts: { xianyu: 'android-account' }, auto_publish: true }, 'listing')
  assert.equal(listing.execution_mode, 'browser')
  assert.equal(listing.auto_publish, true)
  assert.equal(listing.device_id, null)
  assert.deepEqual(listing.accounts, {})
  assert.deepEqual(compatiblePlatforms(catalog, 'promotion', 'browser').map(item => item.id), ['douyin', 'xiaohongshu', 'kuaishou'])
  assert.deepEqual(platformLimits(config.platforms, catalog), { title: 20, description: 500 })
})

test('Android publication stays blocked and browser publication uses its saved account snapshot', () => {
  assert.match(publishBlockReason(config, status, catalog), /旧执行方式/)
  const browser = { ...config, platforms: ['douyin'], execution_mode: 'browser', accounts: { douyin: 'browser-account' } }
  assert.equal(publishBlockReason(browser, status, catalog), '')
  assert.match(publishBlockReason({ ...browser, accounts: { douyin: 'other-account' } }, status, catalog), /账号/)
  assert.match(publishBlockReason({ ...browser, platforms: ['xiaohongshu'] }, status, catalog), /尚未配置/)
})

const dataModule = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`
const source = readFileSync(new URL('../src/views/agent/content/ContentAutomationView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'automation-platforms-test', inlineTemplate: true })
const policySource = readFileSync(new URL('../src/api/agent/automationPolicy.ts', import.meta.url), 'utf8')
const policyModule = dataModule(ts.transpileModule(policySource, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText)
let api = {}
globalThis.__automationPlatformsTest = { call: (method, ...args) => api[method](...args), sessions: [] }
const stubComponent = dataModule('export default { render() { return null } }')
const moduleUrls = {
  'vue': import.meta.resolve('vue'),
  'vue-router': dataModule(`import { reactive } from '${import.meta.resolve('vue')}'; export const useRoute = () => reactive({ query: {} }); export const useRouter = () => ({ push() {} });`),
  'element-plus': dataModule('export const ElMessage = { success() {} };'),
  '@/api/agent/automation': dataModule(`export { automationPurpose } from '${policyModule}'; export const automationApi = Object.fromEntries(['approvedContents', 'status', 'platforms', 'inspectAndroid', 'workflows', 'runs', 'run', 'saveWorkflow', 'startRun', 'action', 'saveDraft', 'approve', 'revise', 'archive', 'image'].map(method => [method, (...args) => globalThis.__automationPlatformsTest.call(method, ...args)]));`),
  '@/api/agent/automationPolicy': policyModule,
  '@/api/agent/content': dataModule('export const contentApi = { listLibrary: async () => [], uploadLibrary: (...args) => globalThis.__automationPlatformsTest.call("uploadLibrary", ...args) };'),
  './ContentAndroidPanel.vue': stubComponent,
  './ContentBrowserPanel.vue': dataModule(`import { onMounted } from '${import.meta.resolve('vue')}'; export default { setup(_props, { emit }) { onMounted(() => globalThis.__automationPlatformsTest.sessions.forEach(session => emit('updated', session))); return () => null } }`),
  './ContentProductPicker.vue': stubComponent,
  './ContentResourcePicker.vue': stubComponent,
  './ContentResourcePreview.vue': stubComponent,
  './contentResourceCatalog': dataModule('export const relatedMedia = () => [];'),
  './localAccess': dataModule(`import { ref } from '${import.meta.resolve('vue')}'; export const useLocalAccess = () => ({ access: ref({ subject: 'test-user' }), hasPermission: () => true, refresh: async () => {} });`),
}
const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, specifier) => {
  assert.ok(moduleUrls[specifier], `Unexpected import: ${specifier}`)
  return `from ${quote}${moduleUrls[specifier]}${quote}`
})
// eslint-disable-next-line no-unsanitized/method -- Compiled local component with controlled dependencies.
const { default: AutomationView } = await import(dataModule(ts.transpileModule(replaced, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText))

const node = (type, text = '') => ({ type, text, style: {}, props: {}, children: [], parent: null })
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
  patchProp: (target, key, prev, next) => {
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
  for (let index = 0; index < 8; index++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
function mount(purpose = 'promotion') {
  const root = node('root')
  const app = renderer.createApp({ render: () => Vue.h(Vue.KeepAlive, null, { default: () => Vue.h(AutomationView, { purpose }) }) })
  for (const name of ['ElButton', 'ElRadioGroup', 'ElRadioButton', 'ElRadio', 'ElSwitch', 'ElCheckboxGroup', 'ElCheckbox', 'ElForm', 'ElFormItem', 'ElSelect', 'ElOption', 'ElInput', 'ElInputNumber', 'ElAlert', 'ElTag', 'ElEmpty']) {
    app.component(name, Vue.defineComponent({ setup: (props, { slots, attrs }) => () => Vue.h(name, attrs, slots.default?.() ?? []) }))
  }
  app.config.warnHandler = () => {}
  app.mount(root)
  return {
    root,
    all: type => descendants(root).filter(item => item.type === type),
    button: label => descendants(root).find(item => item.type === 'ElButton' && textOf(item).trim() === label),
    field: label => descendants(root).find(item => item.type === 'ElFormItem' && item.props.label === label),
    close: () => app.unmount(),
  }
}
function fixture({ workflows = [workflow], runs = [], sessions = [] } = {}) {
  const calls = []
  globalThis.__automationPlatformsTest.sessions = sessions
  api = {
    approvedContents: async () => ({ items: [] }),
    status: async () => structuredClone(status),
    platforms: async () => ({ items: structuredClone(catalog) }),
    inspectAndroid: async () => {
      calls.push(['inspect'])
      return structuredClone(status.android)
    },
    workflows: async purpose => ({ items: purpose === 'legacy' ? [] : structuredClone(workflows) }),
    runs: async (_workflowId, purpose) => ({ items: purpose === 'legacy' ? [] : structuredClone(runs) }),
    run: async id => structuredClone(runs.find(item => item.id === id)),
    saveWorkflow: async (data) => {
      calls.push(['save', structuredClone(data)])
      return { ...structuredClone(workflow), config: data }
    },
    startRun: async (id, key) => {
      calls.push(['start', id, key])
      const saved = calls.filter(item => item[0] === 'save').at(-1)[1]
      return { id: 'new-draft-run', workflow_id: id, config: structuredClone(saved), status: 'queued', draft: {}, platform_results: {}, events: [], created_at: workflow.created_at, updated_at: workflow.updated_at }
    },
    saveDraft: async (id) => {
      calls.push(['draft', id])
      return structuredClone(runs.find(item => item.id === id))
    },
    action: async (id, action) => {
      calls.push(['action', id, action])
      return structuredClone(runs.find(item => item.id === id))
    },
  }
  return calls
}

test('promotion uses web platforms and editing migrates old Android workflows without changing history', async () => {
  const calls = fixture()
  const view = mount()
  try {
    await settle()
    assert.match(textOf(view.field('发布方式')), /网页版/)
    assert.equal(view.field('执行方式'), undefined)
    assert.deepEqual(view.all('ElCheckbox').map(item => item.props.value), ['douyin', 'xiaohongshu', 'kuaishou'])
    assert.notEqual(view.all('ElSwitch')[0].props.disabled, true)
    view.all('button').find(item => item.props.class?.includes('workflow-item'))?.props.onClick()
    await settle()
    assert.deepEqual(view.all('ElCheckboxGroup')[0].props.modelValue, config.platforms)
    view.all('button').find(item => textOf(item).includes('文案与发布策略')).props.onClick()
    await settle()
    await view.button('保存工作流').props.onClick()
    await settle()
    assert.deepEqual(calls.find(item => item[0] === 'save')[1].platforms, config.platforms)
    assert.equal(calls.find(item => item[0] === 'save')[1].execution_mode, 'browser')
    assert.equal(calls.find(item => item[0] === 'save')[1].device_id, null)
    assert.deepEqual(calls.find(item => item[0] === 'save')[1].accounts, {})
    assert.equal(config.execution_mode, 'android')
    assert.equal(calls.find(item => item[0] === 'save')[1].auto_publish, false)
    view.all('ElSwitch')[0].props['onUpdate:modelValue'](true)
    await settle()
    assert.equal(view.button('运行并发布到所选平台').props.disabled, true)
    await view.button('保存工作流').props.onClick()
    await settle()
    assert.equal(calls.filter(item => item[0] === 'save').at(-1)[1].auto_publish, true)
    view.all('button').find(item => item.props.class?.includes('workflow-item'))?.props.onClick()
    await settle()
    assert.equal(view.all('ElSwitch')[0].props.modelValue, true)
    view.all('button').find(item => textOf(item).includes('文案与发布策略')).props.onClick()
    await settle()
    const savedCount = calls.filter(item => item[0] === 'save').length
    await view.button('运行并发布到所选平台').props.onClick()
    await settle()
    assert.equal(calls.filter(item => item[0] === 'save').length, savedCount)
    assert.equal(view.button('检测设备'), undefined)
  }
  finally {
    view.close()
  }
})

test('listing uses the web and only offers Xianyu without an Android connection', async () => {
  fixture({ workflows: [] })
  const view = mount('listing')
  try {
    await settle()
    assert.match(textOf(view.field('发布方式')), /闲鱼.*网页版/)
    assert.deepEqual(view.all('ElCheckboxGroup')[0].props.modelValue, ['xianyu'])
    assert.deepEqual(view.all('ElCheckbox').map(item => item.props.value), ['xianyu'])
    assert.equal(view.button('检测设备'), undefined)
  }
  finally {
    view.close()
  }
})

for (const platform of ['douyin', 'xiaohongshu', 'kuaishou']) {
  test(`${platform} can save and start a draft without login or an account when auto publishing is off`, async () => {
    const draftOnly = { ...workflow, config: { ...config, execution_mode: 'browser', platforms: [platform], accounts: {}, auto_publish: false } }
    const calls = fixture({ workflows: [draftOnly], sessions: [{ platform, connected: false, state: 'login_required', publish_ready: false }] })
    const view = mount()
    try {
      await settle()
      view.all('button').find(item => item.props.class?.includes('workflow-item')).props.onClick()
      await settle()
      view.all('button').find(item => textOf(item).includes('文案与发布策略')).props.onClick()
      await settle()
      assert.equal(view.button('保存工作流').props.disabled, false)
      assert.equal(view.button('运行并生成草稿').props.disabled, false)
      await view.button('保存工作流').props.onClick()
      await settle()
      await view.button('运行并生成草稿').props.onClick()
      await settle()
      const saved = calls.filter(item => item[0] === 'save').at(-1)[1]
      assert.equal(saved.auto_publish, false)
      assert.deepEqual(saved.accounts, {})
      assert.deepEqual(saved.platforms, [platform])
      assert.equal(calls.filter(item => item[0] === 'start').length, 1)
      assert.equal(calls.filter(item => item[0] === 'action').length, 0)
    }
    finally {
      view.close()
    }
  })
}

test('an old Android listing migrates and generates a web draft without a device or login', async () => {
  const listing = { ...workflow, config: { ...config, purpose: 'listing', platforms: ['xianyu'], price: 12, condition: '全新', location: '贵阳' } }
  const calls = fixture({ workflows: [listing] })
  const view = mount('listing')
  try {
    await settle()
    view.all('button').find(item => item.props.class?.includes('workflow-item')).props.onClick()
    await settle()
    view.all('button').find(item => textOf(item).includes('文案与发布策略')).props.onClick()
    await settle()
    await view.button('运行并生成草稿').props.onClick()
    await settle()
    const saved = calls.find(item => item[0] === 'save')[1]
    assert.equal(saved.execution_mode, 'browser')
    assert.equal(saved.device_id, null)
    assert.deepEqual(saved.accounts, {})
    assert.equal(calls.filter(item => item[0] === 'start').length, 1)
    assert.equal(calls.filter(item => item[0] === 'inspect').length, 0)
  }
  finally {
    view.close()
  }
})

test('approved library content is selected without requesting new copy or video sources', async () => {
  const calls = fixture({ workflows: [], sessions: [{ platform: 'douyin', connected: true, state: 'logged_in', account_id: 'browser-account', publish_ready: true }] })
  api.approvedContents = async () => ({ items: [{
    id: 'approved-package',
    product_name: '已审核产品',
    title: '已审核宣传',
    description: '已审核正文',
    listing_title: '上架名称',
    listing_description: '上架描述',
    category: '家居',
    image_ids: ['scene-image'],
    original_image_ids: ['original-image'],
    video_id: 'reviewed-video',
    revision: 3,
  }] })
  const view = mount('promotion')
  try {
    await settle()
    const picker = descendants(view.field('选择已审核内容')).find(item => item.type === 'ElSelect')
    picker.props['onUpdate:modelValue']('approved-package')
    await settle()
    picker.props.onChange()
    await settle()
    assert.equal(view.field('宣传资料来源'), undefined)
    assert.equal(view.field('视频来源'), undefined)
    view.all('button').find(item => textOf(item).includes('文案与发布策略')).props.onClick()
    await settle()
    assert.equal(view.button('发布已审核内容').props.disabled, false)
    await view.button('发布已审核内容').props.onClick()
    await settle()
    const saved = calls.find(item => item[0] === 'save')[1]
    assert.equal(saved.approved_content_id, 'approved-package')
    assert.equal(saved.product_id, null)
    assert.deepEqual(saved.image_ids, [])
    assert.equal(saved.video_mode, 'existing')
    assert.equal(saved.require_review, false)
    assert.equal(saved.auto_publish, true)
    assert.equal(calls.filter(item => item[0] === 'start').length, 1)
  }
  finally {
    view.close()
  }
})

test('ready Android run remains blocked while a browser workflow is being edited, with strict shared text limits', async () => {
  const run = { id: 'run-android', workflow_id: workflow.id, config, status: 'ready', draft: { title: '待发布标题', description: '文案' }, platform_results: {}, events: [], created_at: workflow.created_at, updated_at: workflow.updated_at }
  const browserWorkflow = { ...workflow, config: { ...config, execution_mode: 'browser', platforms: ['douyin'] } }
  const calls = fixture({ workflows: [browserWorkflow], runs: [run] })
  const view = mount()
  try {
    await settle()
    view.all('button').find(item => item.props.class?.includes('workflow-item'))?.props.onClick()
    await settle()
    const publish = view.button('上传并发布到所选平台')
    assert.equal(publish, undefined)
    assert.equal(descendants(view.field('标题')).find(item => item.type === 'ElInput').props.maxlength, 20)
    assert.equal(descendants(view.field('文案')).find(item => item.type === 'ElInput').props.maxlength, 500)
    assert.equal(calls.filter(item => item[0] === 'action' || item[0] === 'draft').length, 0)
  }
  finally {
    view.close()
  }
})

test('ready browser run uses its saved executor and a verified web account', async () => {
  const browserConfig = { ...config, execution_mode: 'browser', platforms: ['douyin'], accounts: { douyin: 'browser-account' } }
  const run = { id: 'run-browser', workflow_id: workflow.id, config: browserConfig, status: 'ready', draft: { title: '浏览器草稿', description: '文案' }, platform_results: {}, events: [], created_at: workflow.created_at, updated_at: workflow.updated_at }
  const calls = fixture({ workflows: [], runs: [run], sessions: [{ platform: 'douyin', connected: true, state: 'logged_in', account_id: 'browser-account', publish_ready: true }] })
  const view = mount()
  try {
    await settle()
    const publish = view.button('上传并发布抖音视频')
    assert.equal(publish.props.disabled, false)
    await publish.props.onClick()
    await settle()
    assert.ok(calls.some(item => item[0] === 'action' && item[1] === 'run-browser' && item[2] === 'publish'))
  }
  finally {
    view.close()
  }
})

test('browser recipe configuration alone never enables publishing without a verified login session', async () => {
  const browserConfig = { ...config, execution_mode: 'browser', platforms: ['douyin'], accounts: { douyin: 'browser-account' } }
  const run = { id: 'run-not-logged-in', workflow_id: workflow.id, config: browserConfig, status: 'ready', draft: { title: '草稿', description: '文案' }, platform_results: {}, events: [], created_at: workflow.created_at, updated_at: workflow.updated_at }
  fixture({ workflows: [], runs: [run], sessions: [{ platform: 'douyin', connected: true, state: 'unknown', publish_ready: false }] })
  const view = mount()
  try {
    await settle()
    assert.equal(view.button('上传并发布抖音视频').props.disabled, true)
    assert.equal(run.config.execution_mode, 'browser')
  }
  finally {
    view.close()
  }
})

test('reviewed workflows publish only the version approved by the user', async () => {
  const run = {
    id: 'review-run',
    workflow_id: workflow.id,
    config: { ...config, execution_mode: 'browser', require_review: true, platforms: ['douyin'], accounts: { douyin: 'browser-account' } },
    status: 'ready',
    draft: { title: '待审文案', description: '真实描述', category: '家居', review: { revision: 3, state: 'pending' } },
    platform_results: {},
    events: [],
    created_at: workflow.created_at,
    updated_at: workflow.updated_at,
  }
  const calls = fixture({ workflows: [], runs: [run], sessions: [{ platform: 'douyin', connected: true, state: 'logged_in', account_id: 'browser-account', publish_ready: true }] })
  api.approve = async (id, revision) => {
    assert.equal(id, run.id)
    assert.equal(revision, 3)
    calls.push(['approve', id, revision])
    return { ...structuredClone(run), draft: { ...run.draft, review: { revision, state: 'approved' } } }
  }
  const view = mount()
  try {
    await settle()
    assert.equal(view.button('上传并发布抖音视频').props.disabled, true)
    await view.button('审核通过此版本').props.onClick()
    await settle()
    assert.equal(view.button('上传并发布抖音视频').props.disabled, false)
    await view.button('上传并发布抖音视频').props.onClick()
    assert.deepEqual(calls.filter(item => ['approve', 'action'].includes(item[0])), [['approve', run.id, 3], ['action', run.id, 'publish']])
  }
  finally {
    view.close()
  }
})

test('unbound listing management rules migrate to web and can save but cannot execute', async () => {
  const management = {
    ...config,
    purpose: 'listing',
    platforms: ['xianyu'],
    operation: 'relist',
    auto_publish: true,
    target_url: 'https://www.goofish.com/item?id=123',
    target_title: '目标商品完整标题',
    device_id: null,
    accounts: {},
  }
  const calls = fixture({ workflows: [{ ...workflow, config: management }] })
  const view = mount('listing')
  try {
    await settle()
    view.all('button').find(item => item.props.class?.includes('workflow-item'))?.props.onClick()
    await settle()
    view.all('button').find(item => textOf(item).includes('确认执行规则')).props.onClick()
    await settle()
    assert.equal(view.button('运行并重新上架商品').props.disabled, true)
    await view.button('保存工作流').props.onClick()
    await settle()
    const saved = calls.find(item => item[0] === 'save')[1]
    assert.equal(saved.execution_mode, 'browser')
    assert.equal(saved.device_id, null)
    assert.deepEqual(saved.accounts, {})
    assert.equal(saved.auto_publish, true)
    assert.equal(saved.operation, 'relist')
  }
  finally {
    view.close()
  }
})

test('old Android listing snapshots stay blocked even when an Android account is connected', () => {
  const live = { android: { ready: true, devices: [{ device_id: 'first', configured: true, connected: true, platforms: ['xianyu'], apps: { xianyu: { state: 'logged_in', account_id: 'first-account' } } }], platforms: { xianyu: { ready: true, configured: true, accounts: [{ account_id: 'other-device-account' }] } } } }
  const bound = { ...config, purpose: 'listing', device_id: 'first', platforms: ['xianyu'], accounts: { xianyu: 'other-device-account' } }
  assert.match(publishBlockReason(bound, live, catalog), /旧执行方式/)
  assert.match(publishBlockReason({ ...bound, accounts: { xianyu: 'first-account' } }, live, catalog), /旧执行方式/)
})

test('local image upload preserves successful files on partial failure and manual copy saves unchanged', async () => {
  const calls = fixture({ workflows: [] })
  let uploadCount = 0
  api.uploadLibrary = async (data) => {
    uploadCount++
    if (uploadCount === 2) {
      throw new Error('上传失败')
    }
    return { id: `local-${uploadCount}`, name: data.name, resource_kind: 'image', has_asset: true }
  }
  const view = mount('listing')
  try {
    await settle()
    assert.ok(view.button('本地上传图片'))
    assert.equal(view.all('ElRadio').some(item => item.props.value === 'delist'), false)
    const change = view.all('input').find(item => item.props.type === 'file').props.onChange
    await change({ target: { value: '', files: [{ name: 'one.png', size: 100 }, { name: 'two.png', size: 100 }] } })
    await settle()
    assert.match(view.all('ElAlert').find(item => item.props.type === 'error').props.title, /已添加 1 张/)
    assert.match(view.all('ElAlert').find(item => item.props.type === 'error').props.title, /成功的图片已保留/)
    const input = label => descendants(view.field(label)).find(item => item.type === 'ElInput')
    input('工作流名称').props['onUpdate:modelValue']('直接上架')
    input('自定义商品名称').props['onUpdate:modelValue']('收纳盒')
    input('商品发布文案').props['onUpdate:modelValue']('有使用痕迹，配件齐全。')
    input('发货地').props['onUpdate:modelValue']('测试地点')
    input('商品分类').props['onUpdate:modelValue']('其他闲置')
    descendants(view.field('商品售价（元）')).find(item => item.type === 'ElInputNumber').props['onUpdate:modelValue'](25)
    view.all('button').find(item => textOf(item).includes('文案与发布策略')).props.onClick()
    await settle()
    await view.button('保存工作流').props.onClick()
    const saved = calls.find(item => item[0] === 'save')[1]
    assert.deepEqual(saved.image_ids, ['local-1'])
    assert.equal(saved.copy_mode, 'manual')
    assert.equal(saved.custom_product_description, '有使用痕迹，配件齐全。')
    assert.equal(saved.auto_publish, false)
    assert.equal(saved.require_review, true)
    await change({ target: { value: '', files: [{ name: 'bad.exe', size: 100 }] } })
    assert.equal(uploadCount, 2)
  }
  finally {
    view.close()
  }
})
