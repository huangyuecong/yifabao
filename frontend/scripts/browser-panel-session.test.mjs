import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const dataModule = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`
const source = readFileSync(new URL('../src/views/agent/content/ContentBrowserPanel.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'browser-panel-test', inlineTemplate: true })
let api = {}
globalThis.__browserPanelTest = { call: (method, ...args) => api[method](...args) }
const modules = {
  'vue': import.meta.resolve('vue'),
  '@/api/agent/automation': dataModule(`export const automationApi = Object.fromEntries(['browserSessions', 'openBrowser', 'inspectBrowser'].map(method => [method, (...args) => globalThis.__browserPanelTest.call(method, ...args)]));`),
  // Online-trial pieces: off here (no remote browser), so the local behaviour is what is tested.
  'element-plus': dataModule(`export const ElMessage = { success() {}, error() {} }; export const ElMessageBox = { confirm: async () => {} };`),
  '@/api/agent/trial': dataModule(`import { ref } from '${import.meta.resolve('vue')}'; export const trialMe = ref({ enabled: false }); export const remoteLoginApi = {};`),
  './RemoteLoginDialog.vue': dataModule(`export default { name: 'RemoteLoginDialog', render: () => null }`),
}
const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, specifier) => {
  assert.ok(modules[specifier], `Unexpected import: ${specifier}`)
  return `from ${quote}${modules[specifier]}${quote}`
})
// eslint-disable-next-line no-unsanitized/method -- Local component with controlled dependencies.
const { default: BrowserPanel } = await import(dataModule(ts.transpileModule(replaced, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText))

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
function mount(props = {}) {
  const root = node('root')
  const visible = Vue.ref(true)
  const app = renderer.createApp({
    render: () => Vue.h(Vue.KeepAlive, null, {
      default: () => visible.value ? Vue.h(BrowserPanel, { enabled: true, ...props }) : null,
    }),
  })
  for (const name of ['ElButton', 'ElDrawer', 'ElInput', 'ElSwitch', 'ElCheckbox', 'ElTag']) {
    app.component(name, Vue.defineComponent({ setup: (props, { slots, attrs }) => () => Vue.h(name, { ...attrs, modelValue: attrs.modelValue ?? attrs['model-value'] }, name !== 'ElDrawer' || (attrs.modelValue ?? attrs['model-value']) ? slots.default?.() ?? [] : []) }))
  }
  app.config.warnHandler = () => {}
  app.mount(root)
  return {
    root,
    all: type => descendants(root).filter(item => item.type === type),
    button: label => descendants(root).find(item => item.type === 'ElButton' && textOf(item).trim() === label),
    show: async (value) => {
      visible.value = value
      await settle()
    },
    close: async () => {
      app.unmount()
      await settle()
    },
  }
}

const unknown = platform => ({ platform, connected: true, state: 'unknown', publish_ready: false })
const loggedIn = (platform, account = 'verified-account') => ({ platform, connected: true, state: 'logged_in', account_id: account, account_label: account, publish_ready: true })
function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

test('listing presents only Xianyu web login and forwards the observed account', async () => {
  const calls = []
  const purposes = []
  const observed = []
  api = {
    browserSessions: async (purpose) => {
      purposes.push(purpose)
      return { items: ['xianyu', 'douyin', 'xiaohongshu', 'kuaishou'].map(unknown) }
    },
    openBrowser: async (platform, page) => {
      calls.push([platform, page])
      return unknown(platform)
    },
    inspectBrowser: async platform => loggedIn(platform),
  }
  const view = mount({ purpose: 'listing', onUpdated: session => observed.push(session) })
  try {
    await settle()
    assert.deepEqual(purposes, ['listing'])
    assert.ok(view.button('打开闲鱼登录'))
    assert.equal(view.button('打开抖音登录'), undefined)
    await view.button('打开闲鱼登录').props.onClick()
    await view.button('打开发布页').props.onClick()
    await view.button('检测登录').props.onClick()
    await settle()
    assert.deepEqual(calls, [['xianyu', 'login'], ['xianyu', 'publish']])
    assert.ok(observed.every(item => item.platform === 'xianyu'))
    assert.equal(observed.at(-1).account_id, 'verified-account')
  }
  finally {
    await view.close()
  }
})

test('all promotion platforms offer web login and publish pages without claiming an opened page is logged in', async () => {
  const calls = []
  const observed = []
  api = {
    browserSessions: async () => ({ items: ['douyin', 'xiaohongshu', 'kuaishou'].map(unknown) }),
    openBrowser: async (platform, page) => {
      calls.push([platform, page])
      return unknown(platform)
    },
  }
  const view = mount({ onUpdated: session => observed.push(session) })
  try {
    await settle()
    for (const label of ['抖音', '小红书', '快手']) {
      assert.ok(view.button(`打开${label}登录`))
    }
    assert.equal(view.all('ElButton').filter(item => textOf(item).trim() === '打开发布页').length, 3)
    await view.button('打开小红书登录').props.onClick()
    await settle()
    await view.all('ElButton').filter(item => textOf(item).trim() === '打开发布页')[2].props.onClick()
    await settle()
    assert.deepEqual(calls, [['xiaohongshu', 'login'], ['kuaishou', 'publish']])
    assert.equal(observed.some(item => item.state === 'logged_in'), false)
    assert.doesNotMatch(textOf(view.root), /已检测到登录账号|登录已确认/)
  }
  finally {
    await view.close()
  }
})

test('only a verified login response shows the account and missing automation remains explicit', async () => {
  let attempts = 0
  api = {
    browserSessions: async () => ({ items: [unknown('douyin')] }),
    inspectBrowser: async () => ++attempts === 1 ? unknown('douyin') : { platform: 'douyin', connected: true, state: 'logged_in', account_id: 'verified-id', account_label: '测试账号', publish_ready: false },
  }
  const view = mount()
  try {
    await settle()
    await view.button('检测登录').props.onClick()
    await settle()
    assert.match(textOf(view.root), /尚未确认登录/)
    await view.button('检测登录').props.onClick()
    await settle()
    assert.match(textOf(view.root), /测试账号/)
    assert.match(textOf(view.root), /账号已连接，自动发布仍需完成平台适配/)
  }
  finally {
    await view.close()
  }
})

test('failed browser opening leaves a usable retry button and an error instead of a success notice', async () => {
  let attempts = 0
  api = {
    browserSessions: async () => ({ items: [] }),
    openBrowser: async (platform) => {
      if (++attempts === 1) {
        throw new Error('浏览器未能启动')
      }
      return unknown(platform)
    },
  }
  const view = mount()
  try {
    await settle()
    await view.button('打开抖音登录').props.onClick()
    await settle()
    assert.match(textOf(view.root), /浏览器未能启动/)
    assert.doesNotMatch(textOf(view.root), /已打开网页登录窗口/)
    assert.equal(view.button('打开抖音登录').props.disabled, false)
    await view.button('打开抖音登录').props.onClick()
    await settle()
    assert.equal(attempts, 2)
    assert.match(textOf(view.root), /已打开网页登录窗口/)
  }
  finally {
    await view.close()
  }
})

test('inspection preserves the specific account mismatch message instead of prompting another login', async () => {
  api = {
    browserSessions: async () => ({ items: [unknown('douyin')] }),
    inspectBrowser: async () => ({ ...unknown('douyin'), message: '当前账号与发布规则中的账号不一致，请核对账号。' }),
  }
  const view = mount()
  try {
    await settle()
    await view.button('检测登录').props.onClick()
    await settle()
    assert.match(textOf(view.root), /当前账号与发布规则中的账号不一致/)
    assert.doesNotMatch(textOf(view.root), /尚未确认登录，请在打开的网页完成登录/)
  }
  finally {
    await view.close()
  }
})

test('initial unavailable status shows the concrete browser failure beside its platform', async () => {
  api = {
    browserSessions: async () => ({ items: [{ platform: 'kuaishou', connected: false, state: 'unavailable', publish_ready: false, message: '专用浏览器已关闭，请重新打开。' }] }),
  }
  const view = mount()
  try {
    await settle()
    assert.match(textOf(view.root), /专用浏览器已关闭，请重新打开/)
    assert.equal(view.button('打开快手登录').props.disabled, false)
  }
  finally {
    await view.close()
  }
})

test('a late GET cannot replace a newer login inspection for the same platform', async () => {
  const get = deferred()
  const observed = []
  api = {
    browserSessions: () => get.promise,
    inspectBrowser: async () => loggedIn('douyin', 'new-account'),
  }
  const view = mount({ onUpdated: session => observed.push(session) })
  try {
    await settle()
    await view.button('检测登录').props.onClick()
    get.resolve({ items: [loggedIn('douyin', 'old-account'), unknown('xiaohongshu'), unknown('kuaishou')] })
    await settle()
    assert.match(textOf(view.root), /new-account/)
    assert.doesNotMatch(textOf(view.root), /old-account/)
    assert.equal(observed.filter(item => item.platform === 'douyin').at(-1).account_id, 'new-account')
  }
  finally {
    await view.close()
  }
})

test('a failed stale GET invalidates only platforms not superseded by an inspection', async () => {
  const get = deferred()
  const observed = []
  api = {
    browserSessions: () => get.promise,
    inspectBrowser: async () => loggedIn('douyin'),
  }
  const view = mount({ onUpdated: session => observed.push(session) })
  try {
    await settle()
    await view.button('检测登录').props.onClick()
    get.reject(new Error('状态读取失败'))
    await settle()
    assert.equal(observed.filter(item => item.platform === 'douyin').at(-1).state, 'logged_in')
    assert.equal(observed.filter(item => item.platform === 'kuaishou').at(-1).state, 'unavailable')
  }
  finally {
    await view.close()
  }
})

test('failed inspection removes the prior verified account and disables readiness in the parent state', async () => {
  const observed = []
  const inspection = deferred()
  api = {
    browserSessions: async () => ({ items: [loggedIn('douyin')] }),
    inspectBrowser: () => inspection.promise,
  }
  const view = mount({ onUpdated: session => observed.push(session) })
  try {
    await settle()
    assert.match(textOf(view.root), /verified-account/)
    const operation = view.button('检测登录').props.onClick()
    await settle()
    assert.doesNotMatch(textOf(view.root), /verified-account/)
    assert.equal(observed.filter(item => item.platform === 'douyin').at(-1).publish_ready, false)
    inspection.reject(new Error('检测连接断开'))
    await operation
    await settle()
    const latest = observed.filter(item => item.platform === 'douyin').at(-1)
    assert.equal(latest.state, 'unavailable')
    assert.equal(latest.account_id, undefined)
    assert.match(textOf(view.root), /检测连接断开/)
  }
  finally {
    await view.close()
  }
})

test('reactivation clears an old login before reading and a failed GET cannot restore it', async () => {
  let reads = 0
  const reload = deferred()
  const observed = []
  api = {
    browserSessions: () => ++reads === 1 ? Promise.resolve({ items: [loggedIn('douyin')] }) : reload.promise,
  }
  const view = mount({ onUpdated: session => observed.push(session) })
  try {
    await settle()
    assert.match(textOf(view.root), /verified-account/)
    await view.show(false)
    assert.equal(observed.filter(item => item.platform === 'douyin').at(-1).state, 'unknown')
    await view.show(true)
    assert.doesNotMatch(textOf(view.root), /verified-account/)
    reload.reject(new Error('重新读取失败'))
    await settle()
    assert.equal(observed.filter(item => item.platform === 'douyin').at(-1).state, 'unavailable')
    assert.doesNotMatch(textOf(view.root), /verified-account/)
  }
  finally {
    await view.close()
  }
})

test('an inspection completed after deactivation cannot overwrite the newly activated page', async () => {
  const oldInspection = deferred()
  const observed = []
  api = {
    browserSessions: async () => ({ items: [unknown('douyin')] }),
    inspectBrowser: () => oldInspection.promise,
  }
  const view = mount({ onUpdated: session => observed.push(session) })
  try {
    await settle()
    const operation = view.button('检测登录').props.onClick()
    await view.show(false)
    await view.show(true)
    oldInspection.resolve(loggedIn('douyin', 'late-account'))
    await operation
    await settle()
    assert.doesNotMatch(textOf(view.root), /late-account/)
    assert.equal(observed.filter(item => item.platform === 'douyin').at(-1).state, 'unknown')
    assert.equal(view.button('打开抖音登录').props.disabled, false)
  }
  finally {
    await view.close()
  }
})
