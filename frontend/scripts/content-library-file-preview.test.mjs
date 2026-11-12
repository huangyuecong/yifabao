import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import ts from 'typescript'
import { createRenderer, defineComponent, h, nextTick, reactive } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const filename = 'ContentLibraryFilePreview.vue'
const source = readFileSync(new URL(`../src/views/agent/content/${filename}`, import.meta.url), 'utf8')
const { descriptor, errors } = parse(source, { filename })
assert.deepEqual(errors, [])
const dataModule = value => `data:text/javascript;base64,${Buffer.from(value).toString('base64')}`
const uploadSource = readFileSync(new URL('../src/views/agent/content/libraryUpload.ts', import.meta.url), 'utf8')
const moduleUrls = {
  'vue': import.meta.resolve('vue'),
  './libraryUpload': dataModule(ts.transpileModule(uploadSource, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText),
  '@element-plus/icons-vue': dataModule('export const Document = {name: "document-icon"}, Headset = {name: "audio-icon"}, Picture = {name: "image-icon"}, VideoPlay = {name: "video-icon"}'),
}
const compiled = compileScript(descriptor, { id: filename, inlineTemplate: true })
const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, specifier) => {
  assert.ok(moduleUrls[specifier], `Unexpected component import: ${specifier}`)
  return `from ${quote}${moduleUrls[specifier]}${quote}`
})
const { outputText } = ts.transpileModule(replaced, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
})
// eslint-disable-next-line no-unsanitized/method -- Executes the local SFC with controlled imports.
const { default: Preview } = await import(dataModule(outputText))
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

function mountFile(name, options = {}) {
  const state = reactive({ file: new File(['preview'], name), ...options })
  let removed = 0
  const root = node('root')
  const app = renderer.createApp({ render: () => h(Preview, { ...state, onRemove: () => removed++ }) })
  for (const name of ['ElIcon', 'ElButton']) {
    app.component(name, defineComponent({
      setup: (props, { slots, attrs }) => () => h(name, attrs, slots.default?.()),
    }))
  }
  app.config.warnHandler = () => {}
  app.mount(root)
  return { state, removed: () => removed, root, find: type => descendants(root).find(item => item.type === type), close: () => app.unmount() }
}

for (const [file, expected] of [['photo.png', 'img'], ['clip.mp4', 'video'], ['voice.mp3', 'audio'], ['manual.pdf', null]]) {
  test(`${file} shows the corresponding local preview and file information`, async () => {
    const f = mountFile(file)
    try {
      if (expected) {
        const element = f.find(expected)
        assert.ok(element)
        assert.match(element.props.src, /^blob:/)
        if (expected !== 'img') {
          assert.ok(Object.hasOwn(element.props, 'controls'))
          assert.equal(element.props.preload, 'metadata')
        }
      }
      else {
        assert.ok(f.find('ElIcon'))
        assert.equal(f.find('img'), undefined)
      }
      assert.ok(descendants(f.root).some(item => item.text === file))
      f.find('ElButton').props.onClick()
      assert.equal(f.removed(), 1)
    }
    finally {
      f.close()
    }
  })
}

test('changing or removing a local file releases its object URL; failed media keeps a usable remove action', async () => {
  const revoked = []
  const original = URL.revokeObjectURL
  URL.revokeObjectURL = (value) => {
    revoked.push(value)
    original(value)
  }
  const f = mountFile('first.png', { removeLabel: '取消替换' })
  try {
    const firstUrl = f.find('img').props.src
    f.state.file = new File(['next'], 'next.mp4')
    await settle()
    assert.ok(revoked.includes(firstUrl))
    assert.equal(f.find('img'), undefined)
    const secondUrl = f.find('video').props.src
    f.find('video').props.onError()
    await settle()
    assert.equal(f.find('video'), undefined)
    assert.ok(f.find('ElIcon'))
    assert.ok(descendants(f.root).some(item => item.text.includes('取消替换')))
    f.close()
    assert.ok(revoked.includes(secondUrl))
  }
  finally {
    URL.revokeObjectURL = original
  }
})
