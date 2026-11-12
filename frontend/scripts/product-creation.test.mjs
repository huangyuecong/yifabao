import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const dataModule = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
let api = {}
const navigations = []
globalThis.__productionTest = { call: (name, ...args) => api[name](...args), navigate: value => navigations.push(value), resources: [] }
const source = readFileSync(new URL('../src/views/agent/content/ContentProductionView.vue', import.meta.url), 'utf8')
const compiled = compileScript(parse(source).descriptor, { id: 'product-creation', inlineTemplate: true })
const stub = dataModule('export default { render() { return null } }')
const modules = {
  'vue': import.meta.resolve('vue'),
  'vue-router': dataModule('export const useRouter = () => ({push: value => globalThis.__productionTest.navigate(value)})'),
  'element-plus': dataModule('export const ElMessage = { success() {} }'),
  '@/api/agent/automation': dataModule('export const automationApi = new Proxy({}, {get: (_, key) => (...args) => globalThis.__productionTest.call(key, ...args)})'),
  '@/api/agent/content': dataModule('export const contentApi = { listLibrary: async () => globalThis.__productionTest.resources }'),
  './ContentProductPicker.vue': stub,
  './ContentResourcePicker.vue': stub,
  './ContentResourcePreview.vue': stub,
  './contentResourceCatalog': dataModule('export const isProductResource = item => item.resource_kind === "product"; export const relatedMedia = (product, items) => items.filter(item => item.parent_id === product.id)'),
  './localAccess': dataModule('export const useLocalAccess = () => ({hasPermission: () => true, refresh: async () => {}})'),
}
const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, name) => {
  assert.ok(modules[name], name)
  return `from ${quote}${modules[name]}${quote}`
})
// eslint-disable-next-line no-unsanitized/method -- Compiles the local component with controlled test dependencies.
const { default: Production } = await import(dataModule(ts.transpileModule(replaced, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText))
const node = (type, text = '') => ({ type, text, style: {}, props: {}, children: [], parent: null })
const renderer = Vue.createRenderer({
  insertStaticContent(content, parent, anchor) {
    const target = node('#static', content)
    target.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) {
      parent.children.push(target)
    }
    else {
      parent.children.splice(index, 0, target)
    }
    return [target, target]
  },
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

function mount() {
  const root = node('root')
  const app = renderer.createApp({ render: () => Vue.h(Vue.KeepAlive, null, { default: () => Vue.h(Production) }) })
  for (const name of ['ElButton', 'ElTabs', 'ElTabPane', 'ElRadioGroup', 'ElRadioButton', 'ElRadio', 'ElForm', 'ElFormItem', 'ElSelect', 'ElOption', 'ElInput', 'ElInputNumber', 'ElAlert', 'ElTag', 'ElEmpty', 'ElCollapse', 'ElCollapseItem']) {
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
function fixture(changes = {}) {
  const run = { id: 'run', config: { name: '产品创作', purpose: 'creation', platforms: [] }, status: 'ready', draft: { title: '宣传标题', description: '宣传正文', listing_title: '商品标题', listing_description: '商品描述', category: '家居', image_count: 1, has_video: true, review: { revision: 2, state: 'pending' }, ...changes } }
  const calls = []
  navigations.length = 0
  globalThis.__productionTest.resources = []
  api = {
    runs: async () => ({ items: [structuredClone(run)] }),
    approvedContents: async () => ({ items: run.draft.approved_content_id ? [{ id: 'package', product_name: '产品', title: '宣传标题', description: '正文', image_ids: ['image'], original_image_ids: ['original'], video_id: 'video', revision: 2 }] : [] }),
    image: async () => ({ blob: new Blob(['image'], { type: 'image/jpeg' }) }),
    video: async () => ({ blob: new Blob(['video'], { type: 'video/mp4' }) }),
    approve: async (id, version) => {
      calls.push(['approve', id, version])
      run.draft.review.state = 'approved'
      run.draft.approved_content_id = 'package'
      return structuredClone(run)
    },
    saveDraft: async (id, data) => {
      calls.push(['save', id, data])
      Object.assign(run.draft, data, { review: { revision: 3, state: 'pending' }, video_stale: true })
      return structuredClone(run)
    },
    revise: async (...args) => {
      calls.push(['revise', ...args])
      run.status = 'queued'
      return structuredClone(run)
    },
  }
  return { run, calls }
}
async function openReview(view) {
  await settle()
  await view.all('button').find(item => item.props.class?.includes('creation-run')).props.onClick()
  await settle()
}

test('generation is independent of platforms and only explicit human approval adds a library package', async () => {
  const { calls } = fixture()
  const view = mount()
  try {
    await openReview(view)
    assert.equal(view.field('平台与账号'), undefined)
    assert.deepEqual(calls, [])
    assert.equal(view.button('审核通过并入库').props.disabled, false)
    await view.button('审核通过并入库').props.onClick()
    await settle()
    assert.deepEqual(calls, [['approve', 'run', 2]])
    assert.equal(view.button('审核通过并入库').props.disabled, true)
    view.button('用于内容宣传').props.onClick()
    view.button('用于商品上架').props.onClick()
    assert.deepEqual(navigations, [
      { path: '/agent/content/promotion', query: { approvedContent: 'package' } },
      { path: '/agent/content/listing', query: { approvedContent: 'package' } },
    ])
  }
  finally {
    view.close()
  }
})

test('unsaved copy and stale video prevent approval; edit sends the current revision', async () => {
  const { calls } = fixture()
  const view = mount()
  try {
    await openReview(view)
    descendants(view.field('宣传标题')).find(item => item.type === 'ElInput').props['onUpdate:modelValue']('新版标题')
    await settle()
    assert.equal(view.button('审核通过并入库').props.disabled, true)
    await view.button('审核通过并入库').props.onClick()
    assert.deepEqual(calls, [])
    await view.button('保存手动修改').props.onClick()
    await settle()
    assert.equal(calls[0][2].expected_revision, 2)
    assert.equal(view.button('审核通过并入库').props.disabled, true)
  }
  finally {
    view.close()
  }
})

test('failed image generation preserves visible copy but cannot be approved', async () => {
  const { calls } = fixture({ generation_error: { stage: 'images', message: '生图服务不可用' }, image_count: 0, has_video: false })
  const view = mount()
  try {
    await openReview(view)
    assert.equal(descendants(view.field('宣传正文')).find(item => item.type === 'ElInput').props.modelValue, '宣传正文')
    assert.equal(view.button('审核通过并入库').props.disabled, true)
    await view.button('审核通过并入库').props.onClick()
    assert.deepEqual(calls, [])
    view.all('ElInput').find(item => item.props['aria-label'] === '审核修改意见').props['onUpdate:modelValue']('使用原图继续')
    view.all('ElSelect').find(item => item.props['aria-label'] === '修改范围').props['onUpdate:modelValue']('originals')
    await settle()
    await view.button('交给 AI 修改').props.onClick()
    await settle()
    assert.deepEqual(calls, [['revise', 'run', 2, '使用原图继续', 'originals']])
    assert.equal(view.button('审核通过并入库').props.disabled, true)
  }
  finally {
    view.close()
  }
})

test('multiple products submit as one idempotent batch with separate source images', async () => {
  const { calls, run } = fixture()
  globalThis.__productionTest.resources = [
    { id: 'one', name: '第一个产品', resource_kind: 'product' },
    { id: 'two', name: '第二个产品', resource_kind: 'product' },
    { id: 'image-one', resource_kind: 'image', parent_id: 'one' },
    { id: 'image-two', resource_kind: 'image', parent_id: 'two' },
  ]
  let attempts = 0
  api.createBatch = async (items, key) => {
    calls.push(['batch', JSON.parse(JSON.stringify(items)), key])
    if (!attempts++) {
      throw new Error('网络超时')
    }
    return { items: items.map((config, index) => ({ ...structuredClone(run), config, id: `batch-${index}`, status: 'queued', draft: {} })), total: items.length }
  }
  const view = mount()
  try {
    await settle()
    view.button('全选产品').props.onClick()
    await settle()
    await view.button('为 2 个产品开始创作').props.onClick()
    await settle()
    assert.equal(calls.length, 1, view.all('ElAlert').map(item => item.props.title).join('; '))
    assert.deepEqual(calls[0][1].map(item => [item.product_id, item.image_ids, item.platforms]), [
      ['one', ['image-one'], []],
      ['two', ['image-two'], []],
    ])
    await view.button('继续创建任务').props.onClick()
    await settle()
    assert.equal(calls[0][2], calls[1][2])
    assert.deepEqual(calls[0][1], calls[1][1])
    assert.equal(view.all('button').filter(item => item.props.class?.includes('creation-run')).length, 3)
  }
  finally {
    view.close()
  }
})
