import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
// Isolated component regression: no browser sessions, user projects or AI calls.
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileScript, compileTemplate, parse } from 'vue/compiler-sfc'
import { projectAssetDurationSeconds } from '../src/views/agent/content/video-editor/assetMedia.ts'
import * as timelineState from '../src/views/agent/content/video-editor/timelineState.ts'

const componentPath = fileURLToPath(new URL('../src/views/agent/content/video-editor/TimelineClip.vue', import.meta.url))
const source = readFileSync(componentPath, 'utf8')
const { descriptor, errors } = parse(source)
assert.deepEqual(errors, [])
const componentCode = compileScript(descriptor, { id: 'timeline-geometry-test', inlineTemplate: true }).content
const workspaceSource = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
const plain = value => JSON.parse(JSON.stringify(value))
const require = createRequire(componentPath)

function hostNode(type, text = '') {
  const node = { type, text, props: {}, style: {}, children: [], parent: null }
  node.getBoundingClientRect = () => ({
    left: Number.parseFloat(node.style.left) || 0,
    width: Number.parseFloat(node.style.width) || 0,
    height: 31,
    top: 0,
    right: (Number.parseFloat(node.style.left) || 0) + (Number.parseFloat(node.style.width) || 0),
    bottom: 31,
  })
  return node
}

const renderer = Vue.createRenderer({
  createElement: type => hostNode(type),
  createText: text => hostNode('#text', text),
  createComment: text => hostNode('#comment', text),
  setText: (node, text) => {
    node.text = text
  },
  setElementText: (node, text) => {
    node.text = text
    node.children = []
  },
  patchProp(node, key, previous, next) {
    node.props[key] = next
    if (key === 'style') {
      for (const name of Object.keys(previous || {})) {
        if (next?.[name] == null) {
          node.style[name] = ''
        }
      }
      Object.assign(node.style, next || {})
    }
  },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
  insert(node, parent, anchor = null) {
    node.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node)
  },
  remove(node) {
    node.parent?.children.splice(node.parent.children.indexOf(node), 1)
    node.parent = null
  },
})

function compileModule(text, filename, localRequire) {
  const compiled = ts.transpileModule(text, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  }).outputText
  const module = { exports: {} }
  const scope = { module, exports: module.exports, require: localRequire, console, setTimeout, clearTimeout }
  vm.runInNewContext(compiled, scope, { filename })
  return module.exports
}

function item(overrides = {}) {
  return {
    id: 'seven-second-clip',
    label: '七秒片段',
    start_seconds: 3,
    duration_seconds: 7,
    trim_in_seconds: 0,
    trim_out_seconds: 0,
    speed: 1,
    volume: 1,
    enabled: true,
    metadata: {},
    ...overrides,
  }
}

function fixture(t, { trackKind = 'video', itemPatch = {}, pixelsPerSecond = 40, locked = false, sourceDuration } = {}) {
  const events = []
  const interaction = { drag: undefined, resize: undefined, tolerance: 0, unsetCount: 0 }
  const interactable = {
    unset() {
      interaction.unsetCount += 1
      return this
    },
    draggable(config) {
      interaction.drag = config
      return this
    },
    resizable(config) {
      interaction.resize = config
      return this
    },
    pointerMoveTolerance(value) {
      interaction.tolerance = value
      return this
    },
  }
  const interact = () => interactable
  interact.pointerMoveTolerance = (value) => {
    interaction.tolerance = value
    return interact
  }
  interact.snappers = { grid: value => value }
  interact.modifiers = Object.fromEntries(['snap', 'snapSize', 'restrictSize', 'restrictEdges'].map(name => [name, options => ({ name, options })]))
  const moduleCache = new Map()
  function localRequire(specifier, importer = componentPath) {
    if (specifier === 'vue') {
      return Vue
    }
    if (specifier === 'interactjs') {
      return interact
    }
    if (specifier.startsWith('.')) {
      const filename = resolve(dirname(importer), /\.[cm]?[jt]s$/u.test(specifier) ? specifier : `${specifier}.ts`)
      if (!moduleCache.has(filename)) {
        moduleCache.set(filename, compileModule(readFileSync(filename, 'utf8'), filename, next => localRequire(next, filename)))
      }
      return moduleCache.get(filename)
    }
    return require(specifier)
  }
  const component = compileModule(componentCode, componentPath, localRequire).default
  const current = item(itemPatch)
  const props = Vue.reactive({ item: current, items: [current], trackKind, trackName: '测试轨道', pixelsPerSecond, selected: false, locked, sourceDuration })
  const callbacks = Object.fromEntries(['select', 'seek', 'trim', 'resize', 'position', 'reorder', 'remove', 'move'].map(name => [
    `on${name[0].toUpperCase()}${name.slice(1)}`,
    (...args) => {
      events.push([name, ...plain(args)])
      if (name === 'select') {
        props.selected = true
      }
    },
  ]))
  const root = hostNode('root')
  const app = renderer.createApp({ setup: () => () => Vue.h(component, { ...props, ...callbacks }) })
  app.mount(root)
  let mounted = true
  const unmount = () => {
    if (mounted) {
      mounted = false
      app.unmount()
    }
  }
  t.after(unmount)
  const element = root.children.find(node => node.props.class?.includes('timeline-item'))
  assert.ok(element, 'mount the actual TimelineClip root')
  return {
    props,
    element,
    interaction,
    events,
    unmount,
    changes: () => events.filter(([name]) => !['select', 'seek'].includes(name)),
    async drag(delta) {
      interaction.drag.listeners.start({ target: element })
      await Vue.nextTick()
      if (delta) {
        interaction.drag.listeners.move({ target: element, dx: delta })
      }
      interaction.drag.listeners.end({ target: element })
      const endedStyle = { ...element.style }
      await Vue.nextTick()
      return endedStyle
    },
    async resize(edge, delta) {
      const startWidth = Number.parseFloat(element.style.width)
      interaction.resize.listeners.start({ target: element, edges: { [edge]: true } })
      await Vue.nextTick()
      interaction.resize.listeners.move({
        target: element,
        edges: { [edge]: true },
        deltaRect: { left: edge === 'left' ? delta : 0, right: edge === 'right' ? delta : 0 },
        rect: { width: Math.max(1, startWidth + (edge === 'left' ? -delta : delta)), height: 31 },
      })
      await Vue.nextTick()
      const previewStyle = { ...element.style }
      interaction.resize.listeners.end({ target: element })
      await Vue.nextTick()
      return previewStyle
    },
  }
}

function assertSourceSpan(before, patch) {
  const original = before.trim_in_seconds + before.duration_seconds * before.speed + before.trim_out_seconds
  const after = patch.trim_in_seconds + patch.duration_seconds * before.speed + patch.trim_out_seconds
  assert.ok(patch.duration_seconds >= 0.099999999)
  assert.ok(patch.trim_in_seconds >= 0 && patch.trim_out_seconds >= 0)
  assert.ok(Math.abs(after - original) < 1e-7, `trim must preserve source span: ${original} versus ${after}`)
}

test('七秒片段单击只选中，宽度、时间与内容不变', async (t) => {
  const f = fixture(t)
  const before = plain(f.props.item)
  assert.equal(f.element.style.width, '280px')
  f.element.props.onClick({})
  await Vue.nextTick()
  assert.equal(f.props.selected, true)
  assert.equal(f.element.style.width, '280px')
  assert.deepEqual(plain(f.props.item), before)
  assert.deepEqual(f.changes(), [])
})

test('无位移拖动结束恢复七秒宽度，不清空 Vue 绑定的 width', async (t) => {
  const f = fixture(t)
  await f.drag(0)
  assert.equal(f.element.style.width, '280px')
  assert.equal(f.element.style.transform || '', '')
  assert.deepEqual(f.changes(), [])
})

test('轻微手抖不移动非主轨，真正拖动才改变开始位置', async (t) => {
  const f = fixture(t, { trackKind: 'subtitle' })
  await f.drag(1)
  assert.deepEqual(f.changes(), [], 'one-pixel jitter must remain selection, not a timeline edit')
  assert.equal(f.element.style.width, '280px')
  await f.drag(40)
  assert.deepEqual(f.changes(), [['position', 'seven-second-clip', 4]])
})

test('轻微手抖和零位移不能重排主轨', async (t) => {
  const f = fixture(t)
  const following = item({ id: 'following', start_seconds: 3, duration_seconds: 7 })
  f.props.items = [f.props.item, following]
  await f.drag(1)
  assert.deepEqual(f.changes(), [])
  assert.equal(f.element.style.width, '280px')
})

for (const edge of ['left', 'right']) {
  test(`${edge === 'left' ? '左' : '右'}侧大幅裁切不越过源素材边界，最短保留0.1秒`, async (t) => {
    const f = fixture(t, { itemPatch: { speed: 2, trim_in_seconds: 2, trim_out_seconds: 3 } })
    const before = plain(f.props.item)
    await f.resize(edge, edge === 'left' ? 2000 : -2000)
    const change = f.changes().find(([name]) => name === 'trim')
    assert.ok(change, 'large trim should clamp and emit a valid trim')
    assertSourceSpan(before, change[2])
    assert.equal(Number(change[2].duration_seconds.toFixed(3)), 0.1)
    assert.equal(f.element.style.width, '280px', 'until props update, transient geometry returns to the persisted item')
  })
}

test('未裁剪素材向外拉伸不会制造负裁剪或留下错误的临时宽度', async (t) => {
  for (const edge of ['left', 'right']) {
    const f = fixture(t)
    await f.resize(edge, edge === 'left' ? -400 : 400)
    for (const [name, , patch] of f.changes()) {
      assert.equal(name, 'trim')
      assertSourceSpan(f.props.item, patch)
    }
    assert.equal(f.element.style.width, '280px')
    assert.equal(f.element.style.transform || '', '')
  }
})

test('短片段真实像素宽度和缩放成比例，不强行占用48像素时间', async (t) => {
  const f = fixture(t, { itemPatch: { duration_seconds: 0.5 }, pixelsPerSecond: 40 })
  assert.equal(f.element.style.width, '20px')
  f.props.pixelsPerSecond = 80
  await Vue.nextTick()
  assert.equal(f.element.style.width, '40px')
})

test('源视频右侧连续7→3→5→7秒可恢复，宽度与裁剪参数同步', async (t) => {
  const f = fixture(t, { sourceDuration: 7, itemPatch: { metadata: { media_kind: 'video', source_duration_seconds: 7 } } })
  const original = plain(f.props.item)
  for (const [delta, expectedDuration, expectedTrimOut] of [[-160, 3, 4], [80, 5, 2], [80, 7, 0]]) {
    const previousCount = f.changes().length
    const preview = await f.resize('right', delta)
    const change = f.changes()[previousCount]
    assert.equal(change?.[0], 'trim')
    assert.equal(change[2].duration_seconds, expectedDuration)
    assert.equal(change[2].trim_out_seconds, expectedTrimOut)
    assertSourceSpan(original, change[2])
    assert.equal(preview.width, `${expectedDuration * 40}px`)
    f.props.item = { ...f.props.item, ...change[2] }
    f.props.items = [f.props.item]
    await Vue.nextTick()
    assert.equal(f.element.style.width, `${expectedDuration * 40}px`)
  }
})

test('主轨静态图片可独立延长展示时长，不受视频源时长限制', async (t) => {
  const f = fixture(t, { itemPatch: { metadata: { media_kind: 'image', mime_type: 'image/png' } } })
  const preview = await f.resize('right', 120)
  const change = f.changes().find(([name]) => ['trim', 'resize'].includes(name))
  assert.ok(change, 'main-track image resize must be emitted')
  assert.equal(change[2].duration_seconds, 10)
  assert.equal(change[2].trim_in_seconds, 0)
  assert.equal(change[2].trim_out_seconds, 0)
  assert.equal(preview.width, '400px')
  f.props.item = { ...f.props.item, ...change[2] }
  await Vue.nextTick()
  assert.equal(f.element.style.width, '400px')
})

function workspaceFixture(initial, assets = [], probeDuration) {
  const { descriptor: workspace } = parse(workspaceSource)
  const ast = ts.createSourceFile('Workspace.ts', workspace.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const production = ['projectAssetFor', 'trimTimelineItem'].map((name) => {
    const statement = ast.statements.find(candidate => ts.isFunctionDeclaration(candidate) && candidate.name?.text === name)
    assert.ok(statement)
    return statement.getText(ast)
  }).join('\n')
  const timeline = Vue.ref(timelineState.createTimeline())
  const track = timelineState.findTrack(timeline.value, 'video')
  track.items = [initial]
  const operations = []
  const scope = {
    ...timelineState,
    timeline,
    project: Vue.ref({ assets }),
    previewVideoElements: new Map(probeDuration ? [[initial.id, { duration: probeDuration }]] : []),
    projectAssetDurationSeconds,
    selectedItemId: Vue.ref(''),
    operationId: () => 'trim-test',
    commitTimeline(next, operation) {
      timeline.value = next
      operations.push(plain(operation))
    },
  }
  vm.createContext(scope)
  vm.runInContext(ts.transpileModule(production, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, scope)
  return { scope, operations, timeline }
}

test('实际工作区向保存队列提交归一化后的裁剪值，不提交原始越界值', () => {
  const { scope, operations, timeline } = workspaceFixture(item({ start_seconds: 0, metadata: { media_kind: 'video', source_duration_seconds: 7 } }))
  scope.trimTimelineItem('seven-second-clip', { duration_seconds: 7, trim_in_seconds: 20, trim_out_seconds: 0 })
  assert.equal(operations.length, 1)
  const saved = timelineState.findItem(timeline.value, 'seven-second-clip').item
  for (const key of ['duration_seconds', 'trim_in_seconds', 'trim_out_seconds']) {
    assert.equal(operations[0][key], saved[key], `${key} sent to the server must match the normalized timeline`)
  }
  assertSourceSpan(item(), saved)
  assert.equal(operations[0].type, 'trim')
})

for (const source of ['asset', 'probe']) {
  test(`源时长仅来自${source === 'asset' ? '工程素材' : '播放器探测'}时，旧一秒草稿左裁半秒不会在二次归一化时丢失七秒上限`, () => {
    const initial = item({ project_asset_id: 'source-video', duration_seconds: 1, trim_in_seconds: 0, trim_out_seconds: 0, metadata: { media_kind: 'video' } })
    const assets = source === 'asset' ? [{ id: 'source-video', metadata: { duration_seconds: 7 } }] : []
    const { scope, timeline, operations } = workspaceFixture(initial, assets, source === 'probe' ? 7 : undefined)
    scope.trimTimelineItem(initial.id, { duration_seconds: 0.5, trim_in_seconds: 0.5, trim_out_seconds: 6 })
    assert.equal(operations.length, 1)
    const saved = timelineState.findItem(timeline.value, initial.id).item
    for (const [key, expected] of [['duration_seconds', 0.5], ['trim_in_seconds', 0.5], ['trim_out_seconds', 6]]) {
      assert.equal(saved[key], expected)
      assert.equal(operations[0][key], expected)
    }
    assertSourceSpan(item(), saved)
  })
}

test('实际时间线模板渲染无工程素材ID的内联字幕，不因读取sourceDuration崩溃', (t) => {
  const { descriptor: workspace } = parse(workspaceSource)
  const tag = workspace.template.content.match(/<TimelineClip\b[\s\S]*?\/>/u)?.[0]
  assert.ok(tag)
  const compiled = compileTemplate({ source: tag, filename: 'Workspace.vue', id: 'inline-subtitle-geometry' })
  assert.deepEqual(compiled.errors, [])
  const { render } = compileModule(compiled.code, 'Workspace.template.ts', specifier => specifier === 'vue' ? Vue : require(specifier))
  const subtitle = item({ id: 'inline-subtitle', content: '内联字幕', metadata: { media_kind: 'subtitle' } })
  const { scope } = workspaceFixture(subtitle)
  const rendered = []
  const stub = Vue.defineComponent({
    props: ['item', 'sourceDuration'],
    setup: props => () => {
      rendered.push({ item: props.item, sourceDuration: props.sourceDuration })
      return Vue.h('div', props.item.content)
    },
  })
  const root = hostNode('root')
  const handlers = Object.fromEntries(['selectItem', 'setPlayhead', 'reorderVideoItem', 'trimTimelineItem', 'positionTimelineItem', 'resizeTimelineItem', 'removeTimelineItem', 'moveTimelineItem'].map(name => [name, () => {}]))
  const app = renderer.createApp({
    components: { TimelineClip: stub },
    setup: () => ({
      ...handlers,
      track: { kind: 'subtitle', name: '字幕', items: [subtitle], locked: false },
      timelineTrackName: (kind, name) => name,
      pixelsPerSecond: 40,
      selectedItemId: '',
      projectAssetFor: scope.projectAssetFor,
      projectAssetDurationSeconds,
    }),
    render,
  })
  assert.doesNotThrow(() => app.mount(root))
  t.after(() => app.unmount())
  assert.equal(rendered.length, 1)
  assert.equal(rendered[0].item.id, 'inline-subtitle')
  assert.equal(rendered[0].sourceDuration, undefined)
})

for (const kind of ['drag', 'resize']) {
  for (const cancel of ['Escape', 'pointercancel']) {
    test(`${kind === 'drag' ? '移动' : '拉伸'}中通过${cancel}取消会还原几何，后续旧end不提交`, async (t) => {
      const f = fixture(t, { trackKind: 'subtitle' })
      const listeners = f.interaction[kind].listeners
      listeners.start({ target: f.element })
      listeners.move({ target: f.element, dx: 40, deltaRect: { left: 0, right: -80 }, rect: { width: 200, height: 31 } })
      await Vue.nextTick()
      if (kind === 'drag') {
        assert.equal(f.element.style.transform, 'translateX(40px)')
      }
      else {
        assert.equal(f.element.style.width, '200px')
      }
      if (cancel === 'Escape') {
        let prevented = false
        f.element.props.onKeydown({ key: 'Escape', preventDefault: () => {
          prevented = true
        } })
        assert.equal(prevented, true)
      }
      else {
        assert.equal(typeof f.element.props.onPointercancel, 'function')
        f.element.props.onPointercancel({})
      }
      await Vue.nextTick()
      assert.equal(f.element.style.width, '280px')
      assert.equal(f.element.style.transform || '', '')
      listeners.end({ target: f.element })
      await Vue.nextTick()
      assert.deepEqual(f.changes(), [])
      await f.resize('right', -40)
      assert.equal(f.changes()[0]?.[2].duration_seconds, 6, 'cancel must not disable future editing')
    })
  }
}

test('同一次拉伸多次move累计-40、-40、+40，不重复套用临时结果或漏掉反向位移', async (t) => {
  const f = fixture(t)
  const listeners = f.interaction.resize.listeners
  listeners.start({ target: f.element })
  for (const [delta, duration] of [[-40, 6], [-40, 5], [40, 6]]) {
    listeners.move({ target: f.element, deltaRect: { left: 0, right: delta }, rect: { width: duration * 40, height: 31 } })
    await Vue.nextTick()
    assert.equal(f.element.style.width, `${duration * 40}px`)
    assert.equal(f.props.item.duration_seconds, 7, 'live preview must not mutate the source item')
    assert.deepEqual(f.changes(), [])
  }
  listeners.end({ target: f.element })
  await Vue.nextTick()
  assert.deepEqual(f.changes(), [['trim', 'seven-second-clip', { duration_seconds: 6, trim_in_seconds: 0, trim_out_seconds: 1 }]])
})

test('旧草稿只缩短duration未写trim_out时，仍能按七秒源素材恢复3→5→7秒', async (t) => {
  const f = fixture(t, {
    sourceDuration: 7,
    itemPatch: { duration_seconds: 3, trim_out_seconds: 0, metadata: { media_kind: 'video' } },
  })
  for (const duration of [5, 7]) {
    const previousCount = f.changes().length
    const preview = await f.resize('right', 80)
    const patch = f.changes()[previousCount]?.[2]
    assert.ok(patch)
    assert.equal(patch.duration_seconds, duration)
    assert.equal(patch.trim_out_seconds, 7 - duration)
    assertSourceSpan(item(), patch)
    assert.equal(preview.width, `${duration * 40}px`)
    f.props.item = { ...f.props.item, ...patch }
    await Vue.nextTick()
    assert.equal(f.element.style.width, `${duration * 40}px`)
  }
})

function allNodes(node) {
  return [node, ...node.children.flatMap(allNodes)]
}

for (const kind of ['video', 'voice']) {
  for (const edge of ['left', 'right']) {
    test(`${kind} 二倍速片段${edge === 'left' ? '左' : '右'}边界，键盘一秒与鼠标一秒得到相同参数`, async (t) => {
      const options = { trackKind: kind, itemPatch: { speed: 2, trim_in_seconds: 2, trim_out_seconds: 3 } }
      const mouse = fixture(t, options)
      const keyboard = fixture(t, options)
      await mouse.resize(edge, 40)
      const handle = allNodes(keyboard.element).find(node => node.props.class?.includes(`clip-trim-handle-${edge}`))
      assert.ok(handle?.props.onKeydown, 'invoke the actual accessible handle event')
      let prevented = 0
      let stopped = 0
      handle.props.onKeydown({
        key: 'ArrowRight',
        shiftKey: true,
        preventDefault: () => {
          prevented += 1
        },
        stopPropagation: () => {
          stopped += 1
        },
      })
      await Vue.nextTick()
      assert.equal(prevented, 1)
      assert.equal(stopped, 1)
      assert.deepEqual(keyboard.changes(), mouse.changes())
      if (kind === 'video') {
        assertSourceSpan(mouse.props.item, mouse.changes()[0][2])
      }
    })
  }
}

for (const change of ['zoom', 'lock', 'item', 'unmount']) {
  test(`拉伸中${{ zoom: '调整缩放', lock: '锁定轨道', item: '切换片段', unmount: '卸载取消' }[change]}会废弃临时几何，旧结束事件不能保存`, async (t) => {
    const f = fixture(t)
    const listeners = f.interaction.resize.listeners
    listeners.start({ target: f.element })
    listeners.move({ target: f.element, deltaRect: { left: 0, right: -80 }, rect: { width: 200, height: 31 } })
    await Vue.nextTick()
    assert.equal(f.element.style.width, '200px')
    const before = plain(f.props.item)
    if (change === 'zoom') {
      f.props.pixelsPerSecond = 80
    }
    else if (change === 'lock') {
      f.props.locked = true
    }
    else if (change === 'item') {
      f.props.item = item({ id: 'next-clip', duration_seconds: 4 })
    }
    else {
      f.unmount()
    }
    await Vue.nextTick()
    if (change !== 'unmount') {
      const expected = change === 'zoom' ? '560px' : change === 'item' ? '160px' : '280px'
      assert.equal(f.element.style.width, expected)
      assert.equal(f.element.style.transform || '', '')
    }
    listeners.end({ target: f.element })
    await Vue.nextTick()
    assert.deepEqual(f.changes(), [])
    if (change !== 'item') {
      assert.deepEqual(plain(f.props.item), before)
    }
  })
}

for (const kind of ['voice', 'music', 'sfx']) {
  test(`${kind} 极端拉伸始终保留同一音频跨度，速度限制0.1–8倍且左端不越零`, async (t) => {
    for (const [edge, delta] of [['left', -40000], ['left', 40000], ['right', -40000], ['right', 40000]]) {
      const f = fixture(t, { trackKind: kind })
      const before = plain(f.props.item)
      const preview = await f.resize(edge, delta)
      const patch = f.changes().find(([name]) => name === 'resize')?.[2]
      assert.ok(patch)
      assert.ok(patch.start_seconds >= 0)
      assert.ok(patch.duration_seconds >= 0.1)
      assert.ok(patch.duration_seconds <= 600)
      assert.ok(patch.speed >= 0.1 && patch.speed <= 8)
      assert.ok(Math.abs(patch.duration_seconds * patch.speed - before.duration_seconds * before.speed) < 1e-7)
      assert.equal(patch.trim_in_seconds, before.trim_in_seconds)
      assert.equal(patch.trim_out_seconds, before.trim_out_seconds)
      if (edge === 'left') {
        assert.equal(patch.start_seconds + patch.duration_seconds, before.start_seconds + before.duration_seconds)
      }
      assert.ok(Math.abs(Number.parseFloat(preview.width) - patch.duration_seconds * f.props.pixelsPerSecond) < 1e-7)
    }
  })
}

test('字幕缩到0.1秒时宽度与实时秒数保持一致，不出现48像素假长度', async (t) => {
  const f = fixture(t, { trackKind: 'subtitle' })
  const listeners = f.interaction.resize.listeners
  listeners.start({ target: f.element })
  listeners.move({ target: f.element, deltaRect: { left: 0, right: -40000 }, rect: { width: 1, height: 31 } })
  await Vue.nextTick()
  assert.ok(Math.abs(Number.parseFloat(f.element.style.width) - 4) < 1e-7)
  assert.ok(allNodes(f.element).some(node => node.text?.includes('0.1s')))
  listeners.end({ target: f.element })
  await Vue.nextTick()
  const patch = f.changes()[0][2]
  assert.equal(Number(patch.duration_seconds.toFixed(7)), 0.1)
  assert.equal(f.element.style.width, '280px', 'without parent commit, persisted width is restored')
})

test('字幕左边界停在零秒且不翻转，右边界至少保留0.1秒', async (t) => {
  const left = fixture(t, { trackKind: 'subtitle' })
  await left.resize('left', -400)
  const leftPatch = left.changes().find(([name]) => name === 'resize')?.[2]
  assert.ok(leftPatch)
  assert.equal(leftPatch.start_seconds, 0)
  assert.equal(leftPatch.start_seconds + leftPatch.duration_seconds, 10)
  const right = fixture(t, { trackKind: 'subtitle' })
  await right.resize('right', -2000)
  const rightPatch = right.changes().find(([name]) => name === 'resize')?.[2]
  assert.ok(rightPatch)
  assert.ok(rightPatch.duration_seconds >= 0.099999999)
  assert.equal(rightPatch.start_seconds, 3)
})

test('锁定轨道不启用拖动和拉伸，仍可以查看选择', async (t) => {
  const f = fixture(t, { locked: true })
  assert.equal(f.interaction.drag, undefined)
  assert.equal(f.interaction.resize, undefined)
  f.element.props.onClick({})
  await Vue.nextTick()
  assert.deepEqual(f.changes(), [])
  assert.equal(f.element.style.width, '280px')
})

test('时间线几何与普通按钮样式隔离，选中态不重新定义宽度', (t) => {
  const f = fixture(t)
  const allStyles = `${descriptor.styles.map(style => style.content).join('\n')}\n${workspaceSource.split('<style scoped>')[1] || ''}`
  const baseRules = [...allStyles.matchAll(/(?:^|\n)\.timeline-item\s*\{([^}]+)\}/gu)].map(match => match[1]).join('\n')
  assert.ok(f.element.type !== 'button' || /max-width:\s*none\s*!important/u.test(baseRules), 'timeline must not inherit generic button max-width constraints')
  assert.match(baseRules, /box-sizing:\s*border-box/u, 'declared width includes borders, matching drag geometry')
  assert.doesNotMatch(baseRules, /min-width:\s*48px/u, 'CSS must not enlarge short clips beyond their timeline span')
  for (const match of allStyles.matchAll(/\.timeline-item\.selected\s*\{([^}]+)\}/gu)) {
    assert.doesNotMatch(match[1], /(?:^|;)\s*(?:width|min-width|max-width|padding|border-width)\s*:/u)
  }
})
