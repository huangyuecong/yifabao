import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { createRenderer, defineComponent, h, nextTick, reactive } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const contentDirectory = new URL('../src/views/agent/content/', import.meta.url)
const vueUrl = import.meta.resolve('vue')
const mockApiKey = '__productPreviewSwitchTestApi'
const dataModule = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const mockApiUrl = dataModule(`export const contentApi = {
  downloadLibraryItem(id) { return globalThis.${mockApiKey}.downloadLibraryItem(id) }
}`)
const emptyComponentUrl = dataModule('export default { render() { return null } }')

function compileComponent(filename, componentImports = {}) {
  const url = new URL(filename, contentDirectory)
  const { descriptor, errors } = parse(readFileSync(url, 'utf8'), { filename })
  assert.deepEqual(errors, [])
  const compiled = compileScript(descriptor, { id: filename, inlineTemplate: true })
  const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, source) => {
    const resolved = source === 'vue'
      ? vueUrl
      : source === '@/api/agent/content'
        ? mockApiUrl
        : componentImports[source] || new URL(`${source}.ts`, contentDirectory).href
    return `from ${quote}${resolved}${quote}`
  })
  const { outputText, diagnostics } = ts.transpileModule(replaced, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
    reportDiagnostics: true,
  })
  assert.deepEqual((diagnostics ?? []).filter(item => item.category === ts.DiagnosticCategory.Error), [])
  return dataModule(outputText)
}

// Compile the real SFCs, including their templates and Vue lifecycle hooks.
// Only remote downloads and unrelated knowledge dialogs are replaced by test doubles.
const previewUrl = compileComponent('ContentResourcePreview.vue')
const pickerUrl = compileComponent('ContentProductPicker.vue', {
  './ContentResourcePreview.vue': previewUrl,
})
const formUrl = compileComponent('TaskConfigurationForm.vue', {
  './ContentProductPicker.vue': pickerUrl,
  './ContentKnowledgePicker.vue': emptyComponentUrl,
  './ContentKnowledgeSelectionList.vue': emptyComponentUrl,
})
// eslint-disable-next-line no-unsanitized/method -- Compiled local repository components with controlled module imports.
const { default: TaskConfigurationForm } = await import(formUrl)
// eslint-disable-next-line no-unsanitized/method -- Same compiled local preview component, mounted separately to check its default behavior.
const { default: ContentResourcePreview } = await import(previewUrl)
// Task configuration keeps its selected product in the knowledge list. Mount the
// optional picker preview separately to cover consumers such as the video editor.
// eslint-disable-next-line no-unsanitized/method -- Compiled local component with controlled module imports, as above.
const { default: ContentProductPicker } = await import(pickerUrl)

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
      target.parent = null
    }
  },
})

function descendants(target) {
  return [target, ...target.children.flatMap(descendants)]
}

const resources = ['a', 'b', 'c', 'empty'].flatMap(letter => [
  {
    id: `product-${letter}-item`,
    resource_entry_id: `product-${letter}`,
    resource_kind: 'product',
    category: 'product',
    name: `产品 ${letter.toUpperCase()}`,
    text_content: `产品 ${letter.toUpperCase()} 说明`,
    metadata: { series_name: '产品系列' },
  },
  ...(letter === 'empty'
    ? []
    : [{
        id: `image-${letter}-item`,
        resource_kind: 'image',
        category: 'product_image',
        name: `产品 ${letter.toUpperCase()} 图片`,
        text_content: '',
        parent_id: `product-${letter}`,
        has_asset: true,
        metadata: {},
      }]),
])
resources.push({
  id: 'brand-guide-item',
  resource_entry_id: 'brand-guide',
  resource_kind: 'document',
  category: 'brand',
  name: '品牌资料',
  text_content: '',
  metadata: {},
})

async function settle() {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

function mountForm({ standalonePreview = false } = {}) {
  const downloads = []
  const createdUrls = []
  const revokedUrls = []
  globalThis[mockApiKey] = {
    downloadLibraryItem(id) {
      return new Promise((resolve, reject) => {
        downloads.push({ id, resolve: () => resolve({ blob: { id } }), reject })
      })
    },
  }
  URL.createObjectURL = (blob) => {
    const value = `blob:test/${blob.id}/${createdUrls.length + 1}`
    createdUrls.push(value)
    return value
  }
  URL.revokeObjectURL = value => revokedUrls.push(value)
  const state = reactive({ resources: structuredClone(resources), form: {
    name: '产品切换测试',
    topic: '团圆',
    product: '产品 A',
    product_resource_id: 'product-a',
    knowledge_resource_ids: ['brand-guide', 'product-a'],
    keywords: '',
    audience: '成年人',
    content_type: '品牌宣传',
    style: '自然',
    length: '中',
    requirements: '',
  } })
  const root = node('root')
  const app = renderer.createApp({
    render: () => standalonePreview
      ? h(ContentResourcePreview, { item: state.resources.find(item => item.id === 'image-a-item') })
      : h('div', [h(TaskConfigurationForm, {
          'modelValue': state.form,
          'resources': state.resources,
          'contentTypeOptions': [],
          'styleOptions': [],
          'onUpdate:modelValue': (value) => {
            state.form = value
          },
        }), h(ContentProductPicker, {
          modelValue: state.form.product,
          productResourceId: state.form.product_resource_id,
          resources: state.resources,
        })]),
  })
  for (const name of ['ElForm', 'ElFormItem', 'ElInput', 'ElSelect', 'ElOption', 'ElButton', 'ElTag', 'ElEmpty']) {
    app.component(name, defineComponent({
      setup: (props, { slots, attrs }) => () => h(name, attrs, slots.default?.()),
    }))
  }
  app.component('ElDialog', defineComponent({ setup: () => () => null }))
  app.component('ElCascader', defineComponent({
    emits: ['change'],
    setup: (props, { attrs, emit }) => () => h('cascader-control', {
      ...attrs,
      choose: value => emit('change', value),
    }),
  }))
  app.component('ElImage', defineComponent({
    setup: (props, { attrs }) => () => h('image-preview', attrs),
  }))
  app.component('ElImageViewer', defineComponent({
    emits: ['close'],
    setup: (props, { attrs, emit }) => () => h('image-viewer', {
      ...attrs,
      close: () => emit('close'),
    }),
  }))
  app.mount(root)
  return {
    app,
    state,
    downloads,
    createdUrls,
    revokedUrls,
    async choose(letter) {
      const picker = descendants(root).find(item => item.type === 'cascader-control')
      assert.ok(picker, '真实产品选择框应存在')
      picker.props.choose(letter ? `product-${letter}` : '')
      await settle()
    },
    images: () => descendants(root).filter(item => item.type === 'image-preview').map(item => item.props.src),
    viewers: () => descendants(root).filter(item => item.type === 'image-viewer').map(item => item.props['url-list'] || item.props.urlList),
    text: () => descendants(root).map(item => item.text).join(' '),
    async imageEvent(handlerName, key = '') {
      const image = descendants(root).find(item => item.type === 'image-preview')
      assert.ok(image, '当前产品图片应存在')
      const event = {
        key,
        stopped: false,
        prevented: false,
        stopPropagation() {
          this.stopped = true
        },
        preventDefault() {
          this.prevented = true
        },
      }
      const handler = image.props[handlerName]
      assert.ok(handler, `图片应绑定 ${handlerName}`)
      for (const callback of Array.isArray(handler) ? handler : [handler]) {
        callback(event)
      }
      await settle()
      return event
    },
    async closeViewer() {
      const viewer = descendants(root).find(item => item.type === 'image-viewer')
      assert.ok(viewer, '应显示大图查看器')
      viewer.props.close()
      await settle()
    },
    async resolve(index) {
      assert.ok(downloads[index], `下载请求 ${index} 应存在`)
      downloads[index].resolve()
      await settle()
    },
    async reject(index) {
      downloads[index].reject(new Error('预览下载失败'))
      await settle()
    },
    close() {
      app.unmount()
    },
  }
}

const tests = []
const test = (name, callback, options = {}) => tests.push({ name, callback, options })

test('不点叉从 A 直接换 B，名称、图片和产品引用同步，独立知识引用保留', async (fixture) => {
  await fixture.resolve(0)
  assert.match(fixture.images()[0], /image-a-item/u)
  await fixture.choose('b')
  assert.deepEqual(fixture.images(), [])
  assert.equal(fixture.state.form.product, '产品 B')
  assert.equal(fixture.state.form.product_resource_id, 'product-b')
  assert.deepEqual(fixture.state.form.knowledge_resource_ids, ['brand-guide', 'product-b'])
  assert.match(fixture.text(), /产品 B 说明/u)
  assert.deepEqual(fixture.downloads.map(item => item.id), ['image-a-item', 'image-b-item'])
  assert.deepEqual(fixture.revokedUrls, [fixture.createdUrls[0]])
  await fixture.resolve(1)
  assert.match(fixture.images()[0], /image-b-item/u)
})

test('快速 A→B→C 后响应乱序，只显示 C，旧请求不创建 ObjectURL', async (fixture) => {
  await fixture.choose('b')
  await fixture.choose('c')
  await fixture.resolve(2)
  await fixture.resolve(0)
  await fixture.resolve(1)
  assert.deepEqual(fixture.createdUrls, ['blob:test/image-c-item/1'])
  assert.deepEqual(fixture.images(), fixture.createdUrls)
  assert.equal(fixture.state.form.product, '产品 C')
  assert.deepEqual(fixture.state.form.knowledge_resource_ids, ['brand-guide', 'product-c'])
})

test('清空后再选正常加载，清空不会保留产品或图片', async (fixture) => {
  await fixture.resolve(0)
  await fixture.choose('')
  assert.equal(fixture.state.form.product, '')
  assert.equal(fixture.state.form.product_resource_id, '')
  assert.deepEqual(fixture.state.form.knowledge_resource_ids, ['brand-guide'])
  assert.deepEqual(fixture.images(), [])
  await fixture.choose('b')
  await fixture.resolve(1)
  assert.match(fixture.images()[0], /image-b-item/u)
})

test('旧请求在清空后返回，不创建或泄漏 ObjectURL', async (fixture) => {
  await fixture.choose('')
  await fixture.resolve(0)
  assert.deepEqual(fixture.createdUrls, [])
  assert.deepEqual(fixture.images(), [])
})

test('B 图片下载失败时显示不可用，不继续显示 A', async (fixture) => {
  await fixture.resolve(0)
  await fixture.choose('b')
  await fixture.reject(1)
  assert.deepEqual(fixture.images(), [])
  assert.match(fixture.text(), /预览暂不可用/u)
  assert.equal(fixture.state.form.product, '产品 B')
  assert.deepEqual(fixture.createdUrls, fixture.revokedUrls)
})

test('切换到无封面产品，不沿用旧产品图片或发起错误下载', async (fixture) => {
  await fixture.resolve(0)
  await fixture.choose('empty')
  assert.deepEqual(fixture.images(), [])
  assert.equal(fixture.downloads.length, 1)
  assert.equal(fixture.state.form.product, '产品 EMPTY')
  assert.deepEqual(fixture.state.form.knowledge_resource_ids, ['brand-guide', 'product-empty'])
  assert.deepEqual(fixture.createdUrls, fixture.revokedUrls)
})

test('页面卸载后请求成功或失败均不回写、不创建 URL', async (fixture) => {
  await fixture.choose('b')
  fixture.close()
  await fixture.resolve(0)
  await fixture.reject(1)
  assert.deepEqual(fixture.createdUrls, [])
})

test('产品封面单击不打开大图，也不修改产品或知识选择', async (fixture) => {
  await fixture.resolve(0)
  const formBefore = JSON.stringify(fixture.state.form)
  await fixture.imageEvent('onClick')
  assert.deepEqual(fixture.viewers(), [])
  assert.equal(JSON.stringify(fixture.state.form), formBefore)
})

test('产品封面双击打开当前大图，关闭、回车和空格均可操作且不影响表单', async (fixture) => {
  await fixture.resolve(0)
  const formBefore = JSON.stringify(fixture.state.form)
  const doubleClick = await fixture.imageEvent('onDblclick')
  assert.equal(doubleClick.stopped, true)
  assert.deepEqual(fixture.viewers(), [[fixture.images()[0]]])
  await fixture.closeViewer()
  assert.deepEqual(fixture.viewers(), [])
  for (const key of ['Enter', ' ']) {
    const event = await fixture.imageEvent('onKeydown', key)
    assert.equal(event.stopped, true)
    assert.equal(event.prevented, true)
    assert.deepEqual(fixture.viewers(), [[fixture.images()[0]]])
    await fixture.closeViewer()
  }
  assert.equal(JSON.stringify(fixture.state.form), formBefore)
})

test('打开 A 大图后换 B，旧查看器关闭并释放 A，新查看器只显示 B', async (fixture) => {
  await fixture.resolve(0)
  await fixture.imageEvent('onDblclick')
  const oldUrl = fixture.images()[0]
  await fixture.choose('b')
  assert.deepEqual(fixture.viewers(), [])
  assert.deepEqual(fixture.images(), [])
  assert.ok(fixture.revokedUrls.includes(oldUrl))
  await fixture.resolve(1)
  await fixture.imageEvent('onDblclick')
  assert.deepEqual(fixture.viewers(), [[fixture.images()[0]]])
  assert.match(fixture.viewers()[0][0], /image-b-item/u)
  assert.ok(!fixture.viewers().flat().includes(oldUrl))
})

test('大图打开后清空产品或换成无封面产品，不残留旧查看器', async (fixture) => {
  await fixture.resolve(0)
  await fixture.imageEvent('onDblclick')
  await fixture.choose('')
  assert.deepEqual(fixture.viewers(), [])
  await fixture.choose('b')
  await fixture.resolve(1)
  await fixture.imageEvent('onDblclick')
  await fixture.choose('empty')
  assert.deepEqual(fixture.viewers(), [])
  assert.deepEqual(fixture.images(), [])
})

test('预览从原地址更新为下载地址后关闭旧大图，重新打开使用最新地址', async (fixture) => {
  const originalUrl = 'https://example.invalid/product-a.png'
  fixture.state.resources.find(item => item.id === 'image-a-item').metadata.original_url = originalUrl
  await settle()
  await fixture.imageEvent('onDblclick')
  assert.deepEqual(fixture.viewers(), [[originalUrl]])
  await fixture.resolve(0)
  assert.deepEqual(fixture.viewers(), [])
  await fixture.imageEvent('onDblclick')
  assert.deepEqual(fixture.viewers(), [[fixture.images()[0]]])
  assert.match(fixture.viewers()[0][0], /^blob:test\//u)
})

test('其他资源 preview 默认为 true 时，原有单击查看行为保留', async (fixture) => {
  await fixture.resolve(0)
  await fixture.imageEvent('onClick')
  assert.deepEqual(fixture.viewers(), [[fixture.images()[0]]])
  await fixture.closeViewer()
  await fixture.imageEvent('onDblclick')
  assert.deepEqual(fixture.viewers(), [[fixture.images()[0]]])
}, { standalonePreview: true })

test('查看与再生成图片面板的大图入口、当前版本 URL 和关闭契约正确', () => {
  const taskView = readFileSync(new URL('ContentTaskView.vue', contentDirectory), 'utf8')
  const revisionPanel = taskView.slice(
    taskView.indexOf('<section v-if="editingArtifact?.kind === \'image\'"'),
    taskView.indexOf('<div class="image-revision-form">'),
  )
  assert.match(revisionPanel, /@dblclick\.stop="imageRevisionPreviewOpen = true"/u)
  assert.match(revisionPanel, /@keydown\.enter\.stop\.prevent="imageRevisionPreviewOpen = true"/u)
  assert.match(revisionPanel, /@keydown\.space\.stop\.prevent="imageRevisionPreviewOpen = true"/u)
  assert.match(revisionPanel, /<ElImageViewer[\s\S]*?:url-list="\[mediaUrls\[editingArtifact\.id\]\]"/u)
  assert.match(revisionPanel, /@close="imageRevisionPreviewOpen = false"/u)
  assert.match(taskView, /@closed="imageRevisionPreviewOpen = false"/u)
})

const originalCreateObjectURL = URL.createObjectURL
const originalRevokeObjectURL = URL.revokeObjectURL
try {
  for (const { name, callback, options } of tests) {
    const fixture = mountForm(options)
    try {
      await callback(fixture)
      console.info(`PASS ${name}`)
    }
    finally {
      fixture.close()
      assert.deepEqual([...fixture.createdUrls].sort(), [...fixture.revokedUrls].sort(), '卸载后所有已创建 URL 应释放')
    }
  }
  console.info(`Product preview switching: ${tests.length} tests passed.`)
}
finally {
  URL.createObjectURL = originalCreateObjectURL
  URL.revokeObjectURL = originalRevokeObjectURL
  delete globalThis[mockApiKey]
}
