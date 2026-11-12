import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { createRenderer, defineComponent, h, nextTick, reactive } from 'vue'
import { compileScript, compileStyle, parse } from 'vue/compiler-sfc'

const filename = 'ContentLibraryMedia.vue'
const source = readFileSync(new URL(`../src/views/agent/content/${filename}`, import.meta.url), 'utf8')
const { descriptor, errors } = parse(source, { filename })
assert.deepEqual(errors, [])
const dataModule = value => `data:text/javascript;base64,${Buffer.from(value).toString('base64')}`
const moduleUrls = {
  './contentResourceCatalog': new URL('../src/views/agent/content/contentResourceCatalog.ts', import.meta.url).href,
  'vue': import.meta.resolve('vue'),
  '@/api/agent/content': dataModule('export const contentApi = { downloadLibraryItem() { throw new Error("Unexpected download") } }'),
  '@element-plus/icons-vue': dataModule('const icon = { render() { return null } }; export const Headset = icon, Picture = icon, VideoPlay = icon'),
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
// eslint-disable-next-line no-unsanitized/method -- Compiles the local SFC with controlled test-only imports.
const { default: ContentLibraryMedia } = await import(dataModule(outputText))

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

async function settle() {
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

function mediaItem(kind = 'image', id = 'first') {
  return { id, resource_kind: kind, source_url: `https://media.example.test/${id}.${kind === 'image' ? 'png' : kind}` }
}

function mountMedia(kind = 'image') {
  const state = reactive({ item: mediaItem(kind) })
  const root = node('root')
  const app = renderer.createApp({ render: () => h(ContentLibraryMedia, {
    item: state.item,
    alt: '产品参考图',
    compact: true,
    controls: true,
  }) })
  for (const name of ['ElSkeleton', 'ElSkeletonItem', 'ElIcon']) {
    app.component(name, defineComponent({
      setup: (props, { slots, attrs }) => () => h(name, attrs, slots.default?.()),
    }))
  }
  // Keep the real SFC template, lifecycle and event modifiers. The Element Plus
  // viewer itself is represented by its input props and close event.
  app.component('ElImageViewer', defineComponent({
    emits: ['close'],
    setup: (props, { attrs, emit }) => () => h('image-viewer', {
      ...attrs,
      close: () => emit('close'),
    }),
  }))
  app.mount(root)
  return {
    state,
    find: type => descendants(root).find(item => item.type === type),
    close: () => app.unmount(),
  }
}

async function dispatch(target, name, key) {
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
  const handlers = Array.isArray(target.props[name]) ? target.props[name] : [target.props[name]]
  for (const handler of handlers) {
    handler(event)
  }
  await settle()
  return event
}

const tests = []
const test = (name, callback) => tests.push({ name, callback })

for (const kind of ['image', 'logo']) {
  test(`${kind} 双击使用当前原图打开独立预览，关闭后可再次打开`, async () => {
    const fixture = mountMedia(kind)
    try {
      const image = fixture.find('img')
      assert.ok(image)
      assert.equal(fixture.find('image-viewer'), undefined)
      assert.equal(image.props.onClick, undefined, '不得劫持原有单击行为')
      assert.match(image.props.title, /双击查看大图/u)
      const event = await dispatch(image, 'onDblclick')
      assert.equal(event.stopped, true)
      let viewer = fixture.find('image-viewer')
      assert.deepEqual(viewer.props['url-list'], [image.props.src])
      assert.notEqual(viewer.props.teleported, false, '查看器应由 Element Plus 传送到遮罩层')
      viewer.props.close()
      await settle()
      assert.equal(fixture.find('image-viewer'), undefined)
      await dispatch(image, 'onDblclick')
      viewer = fixture.find('image-viewer')
      assert.ok(viewer)
    }
    finally {
      fixture.close()
    }
  })
}

for (const key of ['Enter', ' ']) {
  test(`${JSON.stringify(key)} 键可放大且不触发外层卡片操作`, async () => {
    const fixture = mountMedia()
    try {
      const image = fixture.find('img')
      assert.equal(image.props.role, 'button')
      assert.equal(image.props.tabindex, '0')
      assert.match(image.props['aria-label'], /产品参考图.*查看大图/u)
      const event = await dispatch(image, 'onKeydown', key)
      assert.equal(event.stopped, true)
      assert.equal(event.prevented, true)
      assert.ok(fixture.find('image-viewer'))
    }
    finally {
      fixture.close()
    }
  })
}

test('切换素材会关闭旧大图，并使用新图片再次预览', async () => {
  const fixture = mountMedia()
  try {
    await dispatch(fixture.find('img'), 'onDblclick')
    fixture.state.item = mediaItem('image', 'second')
    await settle()
    assert.equal(fixture.find('image-viewer'), undefined)
    const image = fixture.find('img')
    assert.match(image.props.src, /second\.png$/u)
    await dispatch(image, 'onDblclick')
    assert.deepEqual(fixture.find('image-viewer').props['url-list'], [image.props.src])
    fixture.state.item = undefined
    await settle()
    assert.equal(fixture.find('image-viewer'), undefined)
    assert.equal(fixture.find('img'), undefined)
  }
  finally {
    fixture.close()
  }
})

test('图片加载失败关闭预览，不显示失效大图', async () => {
  const fixture = mountMedia()
  try {
    const image = fixture.find('img')
    await dispatch(image, 'onDblclick')
    image.props.onError()
    await settle()
    assert.equal(fixture.find('image-viewer'), undefined)
    assert.equal(fixture.find('img'), undefined)
    assert.equal(fixture.find('span').text, '暂无可预览文件')
  }
  finally {
    fixture.close()
  }
})

for (const kind of ['video', 'audio']) {
  test(`${kind} 保留原媒体控件，不加入图片放大操作`, () => {
    const fixture = mountMedia(kind)
    try {
      const media = fixture.find(kind)
      assert.ok(media)
      assert.equal(media.props.controls, true)
      assert.equal(media.props.preload, 'metadata')
      assert.equal(media.props.onDblclick, undefined)
      assert.equal(media.props.onKeydown, undefined)
      assert.equal(fixture.find('image-viewer'), undefined)
    }
    finally {
      fixture.close()
    }
  })
}

test('图片容器网格可收缩，完整显示策略和可见键盘焦点保持在本组件', () => {
  const css = descriptor.styles.map(style => style.content).join('\n')
  assert.deepEqual(compileStyle({ source: css, filename, id: 'media-preview-test', scoped: true }).errors, [])
  assert.match(css, /\.library-media\.is-image\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);[^}]*grid-template-rows:\s*minmax\(0, 1fr\)/u)
  assert.match(css, /\.library-media img\s*\{[^}]*min-width:\s*0;[^}]*min-height:\s*0;/u)
  assert.match(css, /object-fit:\s*contain;/u)
  assert.match(css, /\.library-media img:focus-visible\s*\{[^}]*outline:/u)
  assert.doesNotMatch(source, /document\.addEventListener/u)
})

for (const { name, callback } of tests) {
  await callback()
  console.info(`PASS ${name}`)
}
console.info(`Content library media preview: ${tests.length} tests passed.`)
