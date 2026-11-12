import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'
import { canKeepModelSecret, modelFormPayload, modelFormValues } from '../src/views/agent/content/aiModelForm.ts'

const configuration = {
  capability: 'text',
  label: '文案',
  provider: 'openai_compatible',
  can_rollback: true,
  values: { llm_base_url: 'https://models.example/v1', llm_model: 'old-model' },
  configured_secrets: ['llm_api_key'],
  adapters: [{ value: 'openai_compatible', label: '兼容接口', defaults: {}, fields: [
    { key: 'llm_base_url', label: '接口地址', kind: 'url', required: true },
    { key: 'llm_api_key', label: 'API Key', kind: 'secret', required: true },
    { key: 'llm_model', label: '模型', kind: 'text', required: true },
  ] }],
}
const state = { configurations: [configuration], revision: 3, history: [] }

test('payload excludes blank keys and unrelated configuration while keeping the selected model', () => {
  const payload = modelFormPayload(configuration, 'openai_compatible', {
    llm_base_url: 'https://models.example/v1',
    llm_model: 'chosen',
    llm_api_key: '',
    unsafe: 'ignored',
  }, 3)
  assert.deepEqual(payload, { provider: 'openai_compatible', expected_revision: 3, values: { llm_base_url: 'https://models.example/v1', llm_model: 'chosen' } })
  assert.deepEqual(modelFormValues({ values: { voices: ['a', 'b'], model: 'test', empty: null } }), { voices: 'a, b', model: 'test', empty: '' })
})

test('form values strip secret fields and only preserve saved keys for the same endpoint', () => {
  const values = { ...configuration.values, llm_api_key: 'server-must-not-return-this', previous_key: 'also-private' }
  assert.deepEqual(modelFormValues({ ...configuration, values, configured_secrets: ['previous_key'] }), configuration.values)
  assert.equal(canKeepModelSecret(configuration, 'openai_compatible', configuration.values, 'llm_api_key'), true)
  assert.equal(canKeepModelSecret(configuration, 'openai_compatible', { ...configuration.values, llm_base_url: 'https://other.example/v1' }, 'llm_api_key'), false)
  assert.equal(canKeepModelSecret(configuration, 'different_provider', configuration.values, 'llm_api_key'), false)
  assert.equal(canKeepModelSecret(configuration, 'openai_compatible', configuration.values, 'unconfigured_key'), false)
})

const source = readFileSync(new URL('../src/views/agent/content/ContentModelSettings.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'model-settings-test', inlineTemplate: true })
const dataModule = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`
const formSource = readFileSync(new URL('../src/views/agent/content/aiModelForm.ts', import.meta.url), 'utf8')
let api = {}
const bridge = { call: (method, ...args) => api[method](...args) }
globalThis.__modelSettingsTest = bridge
const moduleUrls = {
  'vue': import.meta.resolve('vue'),
  'element-plus': dataModule('export const ElMessage = { success() {} }; export const ElMessageBox = { confirm: async () => true };'),
  '@/api/agent/aiModels': dataModule('export const aiModelsApi = Object.fromEntries(["access", "get", "models", "save", "rollback"].map(method => [method, (...args) => globalThis.__modelSettingsTest.call(method, ...args)]));'),
  './aiModelForm': dataModule(ts.transpileModule(formSource, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText),
}
const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, specifier) => {
  assert.ok(moduleUrls[specifier], `Unexpected import: ${specifier}`)
  return `from ${quote}${moduleUrls[specifier]}${quote}`
})
// eslint-disable-next-line no-unsanitized/method -- Compiled local component, controlled dependency stubs.
const { default: Settings } = await import(dataModule(ts.transpileModule(replaced, {
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
  for (let index = 0; index < 4; index++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
function mount(onChanged) {
  const root = node('root')
  const app = renderer.createApp(Settings, { onChanged })
  for (const name of ['ElButton', 'ElDrawer', 'ElForm', 'ElFormItem', 'ElSelect', 'ElOption', 'ElInput', 'ElCollapse', 'ElCollapseItem', 'ElAlert']) {
    app.component(name, Vue.defineComponent({
      setup: (props, { slots, attrs }) => () => Vue.h(name, attrs, name === 'ElDrawer' && !attrs.modelValue ? [] : [...(slots.default?.() ?? []), ...(slots.footer?.() ?? [])]),
    }))
  }
  app.config.warnHandler = () => {}
  const instance = app.mount(root)
  return {
    root,
    instance,
    all: type => descendants(root).filter(item => item.type === type),
    button: label => descendants(root).find(item => item.type === 'ElButton' && textOf(item).trim() === label),
    close: () => app.unmount(),
  }
}

test('without verified permission there is no management button', async () => {
  api = { access: async () => ({ can_manage: false, message: '当前账号没有此智能体的管理权限' }) }
  const view = mount()
  await settle()
  assert.equal(view.button('修改模型'), undefined)
  assert.ok(textOf(view.root).includes('当前账号没有此智能体的管理权限'))
  assert.ok(view.button('重新检查权限'))
  view.close()
})

test('permission lookup failure is visible and can be retried after authorization', async () => {
  api = { access: async () => {
    throw new Error('暂时无法读取模型配置权限')
  } }
  const view = mount()
  await settle()
  assert.equal(view.button('修改模型'), undefined)
  assert.ok(textOf(view.root).includes('暂时无法读取模型配置权限'))
  api.access = async () => ({ can_manage: true, message: '' })
  await view.button('重新检查权限').props.onClick()
  await settle()
  assert.ok(view.button('修改模型'))
  assert.equal(view.button('重新检查权限'), undefined)
  view.close()
})

test('authorized user can fetch, select and save a model; errors preserve inputs and success clears secret', async () => {
  const calls = []
  let fail = true
  api = {
    access: async () => ({ can_manage: true }),
    get: async () => structuredClone(state),
    models: async (capability, payload) => {
      calls.push(['models', capability, payload])
      return { models: [{ id: 'new-model', name: '新模型' }], message: '列表读取成功' }
    },
    save: async (capability, payload) => {
      calls.push(['save', capability, payload])
      if (fail) {
        throw new Error('配置已更新，请重新加载')
      }
      return {
        ...structuredClone(state),
        revision: 4,
        configurations: [{ ...structuredClone(configuration), values: { ...configuration.values, llm_model: payload.values.llm_model } }],
      }
    },
  }
  const view = mount()
  await settle()
  await view.button('修改模型').props.onClick()
  await settle()
  const secret = () => view.all('ElInput').find(item => item.props.type === 'password')
  assert.equal(secret().props.modelValue, undefined)
  secret().props['onUpdate:modelValue']('test-new-key')
  await settle()
  await view.button('获取模型列表').props.onClick()
  await settle()
  assert.equal(calls[0][2].values.llm_api_key, 'test-new-key')
  assert.ok(view.all('ElOption').some(item => item.props.value === 'new-model'))
  view.all('ElSelect').find(item => item.props.placeholder?.includes('模型 ID')).props['onUpdate:modelValue']('new-model')
  await settle()
  await view.button('保存并生效').props.onClick()
  await settle()
  assert.equal(secret().props.modelValue, 'test-new-key')
  assert.ok(view.all('ElAlert').some(item => item.props.title.includes('重新加载')))
  fail = false
  await view.button('保存并生效').props.onClick()
  await settle()
  assert.equal(calls.at(-1)[2].values.llm_model, 'new-model')
  assert.equal(calls.at(-1)[2].expected_revision, 3)
  assert.equal(secret().props.modelValue, undefined)
  view.close()
})

test('lost model permission on save clears cached configuration and typed secrets', async () => {
  api = {
    access: async () => ({ can_manage: true }),
    get: async () => structuredClone(state),
    save: async () => {
      throw Object.assign(new Error('模型管理权限已撤销'), { status: 403 })
    },
  }
  const view = mount()
  await settle()
  await view.button('修改模型').props.onClick()
  await settle()
  view.all('ElInput').find(item => item.props.type === 'password').props['onUpdate:modelValue']('unsaved-secret')
  await view.button('保存并生效').props.onClick()
  await settle()
  assert.equal(view.button('修改模型'), undefined)
  assert.equal(view.all('ElDrawer')[0].props.modelValue, false)
  assert.equal(view.all('ElInput').find(item => item.props.type === 'password'), undefined)
  assert.ok(textOf(view.root).includes('模型管理权限已撤销'))
  view.close()
})

test('changing endpoint requires a fresh key and saved keys are never rendered', async () => {
  const calls = []
  api = {
    access: async () => ({ can_manage: true }),
    get: async () => ({ ...structuredClone(state), configurations: [{ ...structuredClone(configuration), values: { ...configuration.values, llm_api_key: 'server-secret-must-stay-hidden' } }] }),
    save: async (capability, payload) => {
      calls.push([capability, payload])
      return structuredClone(state)
    },
  }
  const view = mount()
  await settle()
  await view.instance.show('text')
  await settle()
  const secret = () => view.all('ElInput').find(item => item.props.type === 'password')
  assert.equal(secret().props.modelValue, undefined)
  assert.ok(secret().props.placeholder.includes('留空保留'))
  const endpoint = view.all('ElInput').find(item => item.props.modelValue === configuration.values.llm_base_url)
  endpoint.props['onUpdate:modelValue']('https://new-endpoint.example/v1')
  await settle()
  assert.ok(!secret().props.placeholder.includes('留空保留'))
  await view.button('保存并生效').props.onClick()
  await settle()
  assert.equal(calls.length, 0)
  assert.ok(view.all('ElAlert').some(item => item.props.title === '请填写API Key'))
  secret().props['onUpdate:modelValue']('key-for-new-endpoint')
  await view.button('保存并生效').props.onClick()
  await settle()
  assert.equal(calls.length, 1)
  assert.equal(calls[0][1].values.llm_base_url, 'https://new-endpoint.example/v1')
  assert.equal(calls[0][1].values.llm_api_key, 'key-for-new-endpoint')
  assert.equal(secret().props.modelValue, undefined)
  view.close()
})

test('a save finishing after unmount does not restore form state or announce a change', async () => {
  let finishSave
  let changed = 0
  api = {
    access: async () => ({ can_manage: true }),
    get: async () => structuredClone(state),
    save: () => new Promise((resolve) => {
      finishSave = resolve
    }),
  }
  const view = mount(() => {
    changed++
  })
  await settle()
  await view.instance.show('text')
  await settle()
  view.all('ElInput').find(item => item.props.type === 'password').props['onUpdate:modelValue']('unsaved-secret')
  const pendingSave = view.button('保存并生效').props.onClick()
  view.close()
  finishSave(structuredClone(state))
  await pendingSave
  await settle()
  assert.equal(changed, 0)
  assert.equal(view.all('ElInput').length, 0)
})
