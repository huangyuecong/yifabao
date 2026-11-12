import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { createRenderer, defineComponent, h, nextTick } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const filename = 'ImageReferenceUploader.vue'
const source = readFileSync(new URL(`../src/views/agent/content/${filename}`, import.meta.url), 'utf8')
const { descriptor, errors } = parse(source, { filename })
assert.deepEqual(errors, [])
const dataModule = value => `data:text/javascript;base64,${Buffer.from(value).toString('base64')}`
const moduleUrls = {
  'vue': import.meta.resolve('vue'),
  'element-plus': dataModule('export {}'),
  '@/api/agent/content': dataModule('export const contentApi = { uploadImageReference() { throw new Error("Preview must never upload") }, uploadLibrary() { throw new Error("References must never enter library") } }'),
  './ContentResourcePreview.vue': dataModule('export default { render() { return null } }'),
  './contentWorkflow': dataModule('export const friendlyContentError = () => "上传失败"'),
}
const compiled = compileScript(descriptor, { id: filename, inlineTemplate: true })
const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, specifier) => {
  assert.ok(moduleUrls[specifier], `Unexpected component import: ${specifier}`)
  return `from ${quote}${moduleUrls[specifier]}${quote}`
})
const { outputText, diagnostics } = ts.transpileModule(replaced, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  reportDiagnostics: true,
})
assert.deepEqual((diagnostics ?? []).filter(item => item.category === ts.DiagnosticCategory.Error), [])
// eslint-disable-next-line no-unsanitized/method -- Compiles the local component with controlled test imports.
const { default: ImageReferenceUploader } = await import(dataModule(outputText))

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

class TestElement {
  constructor(selector, thumbnail = null) {
    this.selector = selector
    this.thumbnail = thumbnail
  }

  matches(selector) {
    return this.selector === selector
  }

  querySelector(selector) {
    assert.equal(selector, 'img.el-upload-list__item-thumbnail')
    return this.thumbnail
  }
}

class TestImageElement extends TestElement {
  constructor(url, thumbnail = true) {
    super('img')
    this.src = url
    this.currentSrc = url
    this.classList = { contains: name => thumbnail && name === 'el-upload-list__item-thumbnail' }
  }
}

async function settle() {
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

function file(uid) {
  return {
    uid,
    name: `${uid}.png`,
    url: `blob:local-image/${uid}`,
    raw: { uid, name: `${uid}.png`, type: 'image/png', size: 120 },
  }
}

function mountUploader() {
  const root = node('root')
  const app = renderer.createApp({ render: () => h(ImageReferenceUploader, {
    taskId: 'preview-test-task',
    selectedItems: [],
    remaining: 3,
  }) })
  for (const name of ['ElUpload', 'ElProgress', 'ElButton']) {
    app.component(name, defineComponent({
      setup: (props, { slots, attrs }) => () => h(name, attrs, slots.default?.()),
    }))
  }
  app.component('ElImageViewer', defineComponent({
    emits: ['close'],
    setup: (props, { attrs, emit }) => () => h('image-viewer', {
      ...attrs,
      close: () => emit('close'),
    }),
  }))
  app.mount(root)
  const find = type => descendants(root).find(item => item.type === type)
  return { find, close: () => app.unmount() }
}

async function event(fixture, handler, target, key) {
  const value = {
    target,
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
  fixture.find('ElUpload').props[handler](value)
  await settle()
  return value
}

const originalElement = globalThis.HTMLElement
const originalImage = globalThis.HTMLImageElement
globalThis.HTMLElement = TestElement
globalThis.HTMLImageElement = TestImageElement
const fixture = mountUploader()
try {
  const first = file(1)
  const second = file(2)
  const snapshot = JSON.stringify([first, second])
  const upload = fixture.find('ElUpload')
  assert.equal(upload.props['auto-upload'], false)
  assert.equal(upload.props['list-type'], 'picture')
  assert.equal(upload.props.onPreview, undefined, '不改变默认文件名点击行为')
  upload.props['on-change'](first, [first, second])
  await settle()
  const thumbnail = new TestImageElement(first.url)
  const opened = await event(fixture, 'onDblclick', thumbnail)
  assert.equal(opened.stopped, true)
  assert.deepEqual(fixture.find('image-viewer').props['url-list'], [first.url])
  assert.ok(Object.hasOwn(fixture.find('image-viewer').props, 'teleported'))
  fixture.find('image-viewer').props.close()
  await settle()
  assert.equal(fixture.find('image-viewer'), undefined)
  console.info('PASS 待上传缩略图双击复用已有 URL 打开，并可关闭')

  for (const target of [new TestElement('button'), new TestElement('a'), new TestElement('div'), new TestImageElement(first.url, false), new TestImageElement('blob:unselected')]) {
    const ignored = await event(fixture, 'onDblclick', target)
    assert.equal(ignored.stopped, false)
    assert.equal(fixture.find('image-viewer'), undefined)
  }
  console.info('PASS 上传区、文件名、删除按钮、其他图片与非当前文件均不触发预览')

  for (const key of ['Enter', ' ']) {
    const openedByKeyboard = await event(fixture, 'onKeydown', new TestElement('li.el-upload-list__item', thumbnail), key)
    assert.equal(openedByKeyboard.stopped, true)
    assert.equal(openedByKeyboard.prevented, true)
    assert.ok(fixture.find('image-viewer'))
    fixture.find('image-viewer').props.close()
    await settle()
  }
  for (const [target, key] of [[new TestElement('li.el-upload-list__item', thumbnail), 'Delete'], [new TestElement('div'), 'Enter'], [new TestElement('button'), ' ']]) {
    const ignored = await event(fixture, 'onKeydown', target, key)
    assert.equal(ignored.stopped, false)
    assert.equal(ignored.prevented, false)
    assert.equal(fixture.find('image-viewer'), undefined)
  }
  console.info('PASS 文件行 Enter/Space 可放大，Delete 与上传区键盘操作不受影响')

  await event(fixture, 'onDblclick', thumbnail)
  upload.props['on-remove'](first, [second])
  await settle()
  assert.equal(fixture.find('image-viewer'), undefined)
  await event(fixture, 'onDblclick', thumbnail)
  assert.equal(fixture.find('image-viewer'), undefined)
  await event(fixture, 'onDblclick', new TestImageElement(second.url))
  assert.deepEqual(fixture.find('image-viewer').props['url-list'], [second.url])
  upload.props['on-change'](first, [second, first])
  await settle()
  assert.equal(fixture.find('image-viewer'), undefined)
  assert.equal(JSON.stringify([first, second]), snapshot, '预览不改变文件数据')
  assert.doesNotMatch(source, /URL\.(?:createObjectURL|revokeObjectURL)|document\.addEventListener/u)
  console.info('PASS 移除或变更文件关闭大图；不改文件、不新建 URL、不执行上传')
  console.info('Image reference upload preview: 4 tests passed.')
}
finally {
  fixture.close()
  if (originalElement === undefined) {
    delete globalThis.HTMLElement
  }
  else {
    globalThis.HTMLElement = originalElement
  }
  if (originalImage === undefined) {
    delete globalThis.HTMLImageElement
  }
  else {
    globalThis.HTMLImageElement = originalImage
  }
}
