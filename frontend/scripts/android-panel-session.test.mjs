import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const dataModule = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`
const source = readFileSync(new URL('../src/views/agent/content/ContentAndroidPanel.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'android-panel-test', inlineTemplate: true })
let api = {}
globalThis.__androidPanelSessionTest = { call: (method, ...args) => api[method](...args) }
const modules = {
  'vue': import.meta.resolve('vue'),
  '@/api/agent/automation': dataModule(`export const automationApi = Object.fromEntries(['cleanupAndroidMedia', 'openAndroidDraft', 'openAndroid', 'androidScreen', 'androidAction', 'verifyAndroid', 'closeAndroid', 'fillAndroidDraft', 'publishAndroidDraft'].map(method => [method, (...args) => globalThis.__androidPanelSessionTest.call(method, ...args)]));`),
}
const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, specifier) => {
  assert.ok(modules[specifier], `Unexpected import: ${specifier}`)
  return `from ${quote}${modules[specifier]}${quote}`
})
// eslint-disable-next-line no-unsanitized/method -- Local component with controlled dependencies.
const { default: AndroidPanel } = await import(dataModule(ts.transpileModule(replaced, {
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
const device = { device_id: 'test-device', label: '测试设备', configured: true, connected: true, platforms: ['douyin', 'xianyu'], apps: {} }
const frame = { session_id: 'test-session', platform: 'douyin', frame_id: 'frame-one', width: 540, height: 960, image: 'data:image/png;base64,test', app: { state: 'login_required' } }
function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}
function mount(props = {}) {
  const root = node('root')
  const app = renderer.createApp({ render: () => Vue.h(AndroidPanel, { device, enabled: true, ...props }) })
  for (const name of ['ElButton', 'ElDrawer', 'ElInput', 'ElSwitch', 'ElCheckbox', 'ElTag']) {
    app.component(name, Vue.defineComponent({ setup: (props, { slots, attrs }) => () => Vue.h(name, { ...attrs, modelValue: attrs.modelValue ?? attrs['model-value'] }, name !== 'ElDrawer' || (attrs.modelValue ?? attrs['model-value']) ? slots.default?.() ?? [] : []) }))
  }
  app.config.warnHandler = () => {}
  app.mount(root)
  return {
    root,
    button: label => descendants(root).find(item => item.type === 'ElButton' && textOf(item).trim() === label),
    drawer: () => descendants(root).find(item => item.type === 'ElDrawer'),
    console: () => descendants(root).find(item => item.props.class === 'android-console'),
    input: () => descendants(root).find(item => item.type === 'ElInput'),
    close: async () => {
      app.unmount()
      await settle()
    },
  }
}

test('login opens a visible connecting drawer before the remote device responds', async () => {
  const request = deferred()
  api = { openAndroid: () => request.promise, closeAndroid: async () => ({}) }
  const view = mount()
  try {
    const opening = view.button('打开抖音').props.onClick()
    await settle()
    assert.equal(view.drawer().props.modelValue, true)
    assert.match(textOf(view.root), /正在连接设备并打开抖音/)
    assert.equal(view.button('打开抖音').props.loading, true)
    request.resolve(frame)
    await opening
    await settle()
    assert.match(textOf(view.root), /检测并绑定登录账号/)
    assert.equal(view.button('打开抖音').props.loading, false)
  }
  finally {
    await view.close()
  }
})

test('closing while connecting releases the late session before another platform opens', async () => {
  const request = deferred()
  const closed = []
  api = { openAndroid: () => request.promise, closeAndroid: async (...args) => closed.push(args) }
  const view = mount()
  try {
    const opening = view.button('打开抖音').props.onClick()
    await settle()
    const closing = view.drawer().props.onClose()
    await settle()
    assert.equal(view.drawer().props.modelValue, false)
    assert.equal(view.button('打开闲鱼').props.disabled, true)
    request.resolve(frame)
    await Promise.all([opening, closing])
    await settle()
    assert.deepEqual(closed, [['test-device', 'test-session']])
    assert.equal(view.drawer().props.modelValue, false)
    assert.equal(view.button('打开闲鱼').props.disabled, false)
  }
  finally {
    await view.close()
  }
})

test('connection failure remains visible and retry can obtain the login screen', async () => {
  let attempts = 0
  api = {
    openAndroid: async () => {
      if (++attempts === 1) {
        throw new Error('设备连接暂时失败')
      }
      return frame
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    assert.equal(view.drawer().props.modelValue, true)
    assert.match(textOf(view.root), /设备连接暂时失败/)
    await view.button('重新连接').props.onClick()
    await settle()
    assert.equal(attempts, 2)
    assert.doesNotMatch(textOf(view.root), /设备连接暂时失败/)
    assert.match(textOf(view.root), /检测并绑定登录账号/)
  }
  finally {
    await view.close()
  }
})

test('refresh recovers a failed frame without retaining the old error', async () => {
  let attempts = 0
  api = {
    openAndroid: async () => frame,
    androidScreen: async () => {
      if (++attempts === 1) {
        throw new Error('画面暂时不可用')
      }
      return { ...frame, frame_id: 'frame-two' }
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    await view.button('刷新画面').props.onClick()
    await settle()
    assert.match(textOf(view.root), /画面暂时不可用/)
    await view.button('刷新画面').props.onClick()
    await settle()
    assert.doesNotMatch(textOf(view.root), /画面暂时不可用/)
  }
  finally {
    await view.close()
  }
})

test('automatic frames pause while the pointer is in the controls and resume after leaving', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  let refreshes = 0
  api = {
    openAndroid: async () => frame,
    androidScreen: async () => {
      refreshes++
      return frame
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    view.console().props.onPointerenter({ pointerType: 'mouse' })
    context.mock.timers.tick(12_000)
    await settle()
    assert.equal(refreshes, 0)
    assert.equal(view.button('返回').props.disabled, false)
    view.console().props.onPointerleave({ pointerType: 'mouse' })
    context.mock.timers.tick(3000)
    await settle()
    assert.equal(refreshes, 1)
  }
  finally {
    await view.close()
  }
})

test('focused text input keeps automatic refresh paused even when the pointer leaves', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  let refreshes = 0
  api = {
    openAndroid: async () => frame,
    androidScreen: async () => {
      refreshes++
      return frame
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    view.console().props.onPointerenter({ pointerType: 'mouse' })
    view.input().props.onFocus()
    view.console().props.onPointerleave({ pointerType: 'mouse' })
    context.mock.timers.tick(12_000)
    await settle()
    assert.equal(refreshes, 0)
    view.input().props.onBlur()
    context.mock.timers.tick(3000)
    await settle()
    assert.equal(refreshes, 1)
  }
  finally {
    await view.close()
  }
})

test('an in-flight frame remains serialized and never shows account verification loading', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  const request = deferred()
  const actions = []
  const verifying = deferred()
  api = {
    openAndroid: async () => frame,
    androidScreen: () => request.promise,
    androidAction: async (...args) => {
      actions.push(args)
      return frame
    },
    verifyAndroid: () => verifying.promise,
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    context.mock.timers.tick(3000)
    await settle()
    assert.equal(view.button('刷新画面').props.loading, true)
    assert.equal(view.button('检测并绑定登录账号').props.loading, false)
    assert.equal(view.button('检测并绑定登录账号').props.disabled, true)
    view.console().props.onPointerenter({ pointerType: 'mouse' })
    await view.button('返回').props.onClick()
    assert.equal(actions.length, 0)
    request.resolve({ ...frame, frame_id: 'fresh-frame' })
    await settle()
    assert.equal(view.button('返回').props.disabled, false)
    await view.button('返回').props.onClick()
    await settle()
    assert.equal(actions.length, 1)
    assert.equal(actions[0][2].frame_id, 'fresh-frame')
    const operation = view.button('检测并绑定登录账号').props.onClick()
    await settle()
    assert.equal(view.button('检测并绑定登录账号').props.loading, true)
    verifying.resolve(frame)
    await operation
    await settle()
    assert.equal(view.button('检测并绑定登录账号').props.loading, false)
  }
  finally {
    await view.close()
  }
})

const staleFrame = () => Object.assign(new Error('屏幕已变化，请刷新画面后操作。'), { code: 'android_stale_frame' })

test('stale input reads a new frame and preserves the text until the user sends again', async () => {
  const actions = []
  let reads = 0
  api = {
    openAndroid: async () => frame,
    androidAction: async (...args) => {
      actions.push(args)
      if (actions.length === 1) {
        throw staleFrame()
      }
      return frame
    },
    androidScreen: async () => {
      reads++
      return { ...frame, frame_id: 'fresh-input-frame' }
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    view.input().props['onUpdate:modelValue']('测试输入')
    await settle()
    await view.button('填写到当前输入框').props.onClick()
    await settle()
    assert.equal(reads, 1)
    assert.equal(actions.length, 1)
    assert.equal(view.input().props.modelValue, '测试输入')
    assert.match(textOf(view.root), /画面已刷新，输入内容已保留/)
    assert.equal(view.button('刷新画面').props.loading, false)
    await view.button('填写到当前输入框').props.onClick()
    await settle()
    assert.equal(actions.length, 2)
    assert.equal(actions[1][2].frame_id, 'fresh-input-frame')
    assert.equal(view.input().props.modelValue, '')
  }
  finally {
    await view.close()
  }
})

test('stale tap reads a new frame without replaying coordinates', async () => {
  const actions = []
  api = {
    openAndroid: async () => frame,
    androidAction: async (...args) => {
      actions.push(args)
      throw staleFrame()
    },
    androidScreen: async () => ({ ...frame, frame_id: 'new-layout' }),
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    const screenImage = descendants(view.root).find(item => item.type === 'img')
    const event = {
      pointerId: 1,
      clientX: 30,
      clientY: 60,
      currentTarget: {
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 540, height: 960 }),
        setPointerCapture() {},
      },
    }
    screenImage.props.onPointerdown(event)
    screenImage.props.onPointerup(event)
    await settle()
    assert.equal(actions.length, 1)
    assert.equal(actions[0][2].kind, 'tap')
    assert.match(textOf(view.root), /画面已刷新，请核对当前页面后再次点击或滑动/)
  }
  finally {
    await view.close()
  }
})

test('failed stale-frame recovery stops refreshing and allows a manual recovery', async () => {
  let reads = 0
  api = {
    openAndroid: async () => frame,
    androidAction: async () => {
      throw staleFrame()
    },
    androidScreen: async () => {
      if (++reads === 1) {
        throw new Error('取帧失败')
      }
      return frame
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    await view.button('返回').props.onClick()
    await settle()
    assert.match(textOf(view.root), /画面已过期且刷新未完成/)
    assert.equal(view.button('刷新画面').props.loading, false)
    assert.equal(view.button('刷新画面').props.disabled, false)
    await view.button('刷新画面').props.onClick()
    await settle()
    assert.doesNotMatch(textOf(view.root), /画面已过期/)
  }
  finally {
    await view.close()
  }
})

test('stale publication never resubmits and requires checking the new frame again', async () => {
  let publications = 0
  const draftFrame = { ...frame, draft: { run_id: 'test-run', note: '', media_count: 1, files: [], fields: {} } }
  api = {
    openAndroidDraft: async () => draftFrame,
    publishAndroidDraft: async () => {
      publications++
      throw staleFrame()
    },
    androidScreen: async () => ({ ...draftFrame, frame_id: 'new-publish-frame' }),
    closeAndroid: async () => ({}),
  }
  const view = mount({ run: { id: 'test-run', config: { platforms: ['douyin'] }, platform_results: {} } })
  try {
    await view.button('导入草稿到抖音').props.onClick()
    await settle()
    descendants(view.root).find(item => item.type === 'ElCheckbox').props['onUpdate:modelValue'](true)
    await settle()
    await view.button('发布到抖音').props.onClick()
    await settle()
    assert.equal(publications, 1)
    assert.equal(view.button('发布到抖音').props.disabled, true)
    assert.match(textOf(view.root), /画面已刷新，请重新核对账号、素材和发布信息/)
  }
  finally {
    await view.close()
  }
})

test('closing during stale-frame recovery releases the session and never restores the dismissed screen', async () => {
  const request = deferred()
  const closed = []
  api = {
    openAndroid: async () => frame,
    androidAction: async () => {
      throw staleFrame()
    },
    androidScreen: () => request.promise,
    closeAndroid: async (...args) => closed.push(args),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    const action = view.button('返回').props.onClick()
    await settle()
    assert.equal(view.button('刷新画面').props.loading, true)
    const closing = view.drawer().props.onClose()
    request.resolve({ ...frame, frame_id: 'late-recovery' })
    await Promise.all([action, closing])
    await settle()
    assert.equal(view.drawer().props.modelValue, false)
    assert.deepEqual(closed, [['test-device', 'test-session']])
    assert.equal(view.button('打开抖音').props.disabled, false)
  }
  finally {
    await view.close()
  }
})

test('a completed action refreshes the settled screen once even while the pointer remains inside', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  let reads = 0
  api = {
    openAndroid: async () => frame,
    androidAction: async () => frame,
    androidScreen: async () => {
      reads++
      return { ...frame, frame_id: 'settled-frame' }
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    view.console().props.onPointerenter({ pointerType: 'mouse' })
    await view.button('返回').props.onClick()
    context.mock.timers.tick(699)
    await settle()
    assert.equal(reads, 0)
    context.mock.timers.tick(1)
    await settle()
    assert.equal(reads, 1)
    context.mock.timers.tick(12_000)
    await settle()
    assert.equal(reads, 1)
  }
  finally {
    await view.close()
  }
})

test('consecutive actions coalesce delayed refreshes and focused typing defers the final read until blur', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  let reads = 0
  let actions = 0
  api = {
    openAndroid: async () => frame,
    androidAction: async () => {
      actions++
      return frame
    },
    androidScreen: async () => {
      reads++
      return frame
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    view.console().props.onPointerenter({ pointerType: 'mouse' })
    await view.button('返回').props.onClick()
    context.mock.timers.tick(500)
    await view.button('桌面').props.onClick()
    context.mock.timers.tick(200)
    await settle()
    assert.equal(reads, 0)
    view.input().props.onFocus()
    context.mock.timers.tick(12_000)
    await settle()
    assert.equal(reads, 0)
    assert.equal(actions, 2)
    view.input().props.onBlur()
    context.mock.timers.tick(700)
    await settle()
    assert.equal(reads, 1)
    await view.button('返回').props.onClick()
    await view.drawer().props.onClose()
    context.mock.timers.tick(12_000)
    await settle()
    assert.equal(reads, 1)
  }
  finally {
    await view.close()
  }
})

test('delayed action refresh waits for another in-flight request instead of issuing concurrent reads', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  const verification = deferred()
  let reads = 0
  api = {
    openAndroid: async () => frame,
    androidAction: async () => frame,
    verifyAndroid: () => verification.promise,
    androidScreen: async () => {
      reads++
      return frame
    },
    closeAndroid: async () => ({}),
  }
  const view = mount()
  try {
    await view.button('打开抖音').props.onClick()
    await settle()
    view.console().props.onPointerenter({ pointerType: 'mouse' })
    await view.button('返回').props.onClick()
    const verifying = view.button('检测并绑定登录账号').props.onClick()
    context.mock.timers.tick(700)
    await settle()
    assert.equal(reads, 0)
    verification.resolve(frame)
    await verifying
    context.mock.timers.tick(700)
    await settle()
    assert.equal(reads, 1)
  }
  finally {
    await view.close()
  }
})
