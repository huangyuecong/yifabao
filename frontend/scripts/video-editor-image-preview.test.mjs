import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import process from 'node:process'
import ts from 'typescript'
import { compileTemplate, parse } from 'vue/compiler-sfc'

const filename = 'VideoEditorWorkspace.vue'
const source = readFileSync(
  new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url),
  'utf8',
)
const { descriptor, errors } = parse(source, { filename })
assert.deepEqual(errors, [])
const compiledTemplate = compileTemplate({
  id: 'video-editor-image-preview-test',
  filename,
  source: descriptor.template.content,
})
assert.deepEqual(compiledTemplate.errors, [])
const script = ts.createSourceFile(
  filename,
  descriptor.scriptSetup.content,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS,
)
const declarations = ['openImagePreview', 'handleGlobalKeydown'].map((name) => {
  const declaration = script.statements.find(statement =>
    ts.isFunctionDeclaration(statement) && statement.name?.text === name,
  )
  assert.ok(declaration, `应测试组件中的真实函数 ${name}`)
  return declaration.getText(script)
})
const { outputText } = ts.transpileModule(`
  export function createHarness(context: Record<string, unknown>) {
    const { imagePreviewUrl, workflowFinalizing, undo, redo, manualSave, togglePlayback, deleteSelected } = context as any
    ${declarations.join('\n')}
    return { openImagePreview, handleGlobalKeydown }
  }
`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
})
// eslint-disable-next-line no-unsanitized/method -- Only local component declarations and controlled test bindings are loaded.
const { createHarness } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`,
)

function setup() {
  const actions = []
  const imagePreviewUrl = { value: '' }
  const context = {
    imagePreviewUrl,
    workflowFinalizing: { value: false },
    undo: () => actions.push('undo'),
    redo: () => actions.push('redo'),
    manualSave: () => actions.push('save'),
    togglePlayback: () => actions.push('playback'),
    deleteSelected: () => actions.push('delete'),
  }
  return { actions, imagePreviewUrl, ...createHarness(context) }
}

const template = descriptor.template.content
const imageTags = [...template.matchAll(/<img\b[^>]*>/gu)].map(match => match[0])

function test(name, callback) {
  try {
    callback()
    console.info(`PASS ${name}`)
  }
  catch (error) {
    console.error(`FAIL ${name}`, error)
    process.exitCode = 1
  }
}

test('预览只记录所点原始图片地址，连续打开不会沿用上一张图，也不触发工程操作', () => {
  const harness = setup()
  harness.openImagePreview('blob:first-image')
  assert.equal(harness.imagePreviewUrl.value, 'blob:first-image')
  harness.openImagePreview('blob:second-image')
  assert.equal(harness.imagePreviewUrl.value, 'blob:second-image')
  harness.openImagePreview('')
  assert.equal(harness.imagePreviewUrl.value, 'blob:second-image')
  assert.deepEqual(harness.actions, [])
})

test('已确认、本地、企业素材图均有独立双击与回车/空格完整预览，不占用单击操作', () => {
  const thumbnails = imageTags.filter(tag => tag.includes(':src="projectAssetUrl(asset)"'))
  assert.equal(thumbnails.length, 3)
  for (const tag of thumbnails) {
    assert.match(tag, /role="button"/u)
    assert.match(tag, /tabindex="0"/u)
    assert.match(tag, /title="双击查看完整图片"/u)
    assert.match(tag, /:aria-label="[^"\n]*查看完整图片/u)
    for (const event of ['dblclick.stop', 'keydown.enter.stop.prevent', 'keydown.space.stop.prevent']) {
      assert.ok(tag.includes(`@${event}="openImagePreview(projectAssetUrl(asset))"`))
    }
    assert.doesNotMatch(tag, /@click=/u)
  }
})

test('主画布图片支持原图预览，不更改视频播放事件', () => {
  const image = imageTags.find(tag => tag.includes(':src="resolveItemUrl(layer.item)"'))
  assert.ok(image)
  assert.match(image, /@dblclick\.stop="openImagePreview\(resolveItemUrl\(layer\.item\)\)"/u)
  assert.match(image, /@keydown\.enter\.stop\.prevent="openImagePreview\(resolveItemUrl\(layer\.item\)\)"/u)
  assert.match(image, /@keydown\.space\.stop\.prevent="openImagePreview\(resolveItemUrl\(layer\.item\)\)"/u)
  assert.doesNotMatch(image, /togglePlayback|@click=/u)
  const video = [...template.matchAll(/<video\b(?:[^>"']|"[^"]*"|'[^']*')*>/gu)]
    .map(match => match[0])
    .find(tag => tag.includes('setPreviewVideoElement(layer.item.id, element)'))
  assert.ok(video)
  assert.match(video, /@click="togglePlayback"/u)
  assert.doesNotMatch(video, /ImagePreview|dblclick/u)
})

test('图片预览使用一个可退出的全屏查看器，支持 Esc 且不嵌在滚动面板中', () => {
  const viewers = [...template.matchAll(/<ElImageViewer\b[^>]*\/>/gu)].map(match => match[0])
  assert.equal(viewers.length, 1)
  assert.match(viewers[0], /v-if="imagePreviewUrl"/u)
  assert.match(viewers[0], /:url-list="\[imagePreviewUrl\]"/u)
  assert.match(viewers[0], /\bteleported\b/u)
  assert.match(viewers[0], /:close-on-press-escape="true"/u)
  assert.match(viewers[0], /@close="imagePreviewUrl = ''"/u)
})

test('查看大图时按空格、删除和工程快捷键不会操作背后的时间线', () => {
  const harness = setup()
  harness.openImagePreview('blob:product')
  for (const event of [
    { key: ' ', code: 'Space' },
    { key: 'Delete' },
    { key: 'z', ctrlKey: true },
    { key: 'y', metaKey: true },
    { key: 's', ctrlKey: true },
    { key: 'Escape' },
  ]) {
    harness.handleGlobalKeydown({
      ...event,
      preventDefault() {
        assert.fail('查看器应自行管理键盘，不能触发工程快捷键')
      },
    })
  }
  assert.deepEqual(harness.actions, [])
})

test('关闭大图后，原空格播放、删除和保存快捷键保持不变', () => {
  const harness = setup()
  harness.openImagePreview('blob:product')
  harness.imagePreviewUrl.value = ''
  for (const event of [
    { key: ' ', code: 'Space' },
    { key: 'Delete' },
    { key: 's', ctrlKey: true },
  ]) {
    harness.handleGlobalKeydown({ ...event, preventDefault() {} })
  }
  assert.deepEqual(harness.actions, ['playback', 'delete', 'save'])
})

test('产品和资料等共享组件打开的大图同样隔离时间线快捷键', () => {
  const harness = setup()
  assert.equal(harness.imagePreviewUrl.value, '')
  const viewer = { className: 'el-image-viewer__wrapper' }
  for (const event of [
    { key: ' ', code: 'Space' },
    { key: 'Delete' },
    { key: 'z', ctrlKey: true },
    { key: 'y', metaKey: true },
    { key: 's', ctrlKey: true },
    { key: 'Escape' },
  ]) {
    harness.handleGlobalKeydown({
      ...event,
      target: {
        closest(selector) {
          assert.equal(selector, '.el-image-viewer__wrapper')
          return viewer
        },
        matches() {
          assert.fail('查看器内事件不应进入工程编辑快捷键分支')
        },
      },
      preventDefault() {
        assert.fail('共享查看器应自行管理键盘')
      },
    })
  }
  assert.deepEqual(harness.actions, [])
  harness.handleGlobalKeydown({
    key: 'Delete',
    target: { closest: () => null, matches: () => false },
    preventDefault() {},
  })
  assert.deepEqual(harness.actions, ['delete'])
})

test('图片缩略图完整包含，视频缩略图与叠加层穿透及中心定位保持正确', () => {
  const styles = descriptor.styles.map(style => style.content).join('\n')
  assert.match(styles, /\.generated-asset-preview img\s*\{\s*object-fit: contain;/u)
  assert.match(styles, /\.generated-asset-preview img,\s*\.generated-asset-preview video\s*\{[^}]*object-fit: cover;/u)
  assert.match(styles, /\.image-preview-trigger:focus-visible\s*\{[^}]*outline:/u)
  assert.match(styles, /\.preview-overlays,\s*\.preview-subtitles\s*\{[^}]*pointer-events: none;/u)
  assert.match(template, /<div class="preview-overlays" aria-hidden="true">/u)
  const overlay = imageTags.find(tag => tag.includes(':src="resolveItemUrl(item)"'))
  assert.ok(overlay)
  assert.doesNotMatch(overlay, /@|role=|tabindex=/u)
  assert.match(template, /:style="overlayPreviewStyle\(item, timeline\.canvas, previewStageSize\.width\)"/u)
})
