import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import process from 'node:process'
import postcss from 'postcss'
import { parse } from 'vue/compiler-sfc'

const workspacePath = new URL(
  '../src/views/agent/content/video-editor/VideoEditorWorkspace.vue',
  import.meta.url,
)
const { descriptor, errors } = parse(readFileSync(workspacePath, 'utf8'), {
  filename: 'VideoEditorWorkspace.vue',
})
assert.deepEqual(errors, [])
assert.ok(descriptor.styles.length)
assert.ok(descriptor.styles.every(style => style.scoped), '布局规则只能作用于视频工作台')
const css = postcss.parse(descriptor.styles.map(style => style.content).join('\n'))

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

// This is a source-level CSS contract, not a browser layout simulation. Reading
// parsed rules keeps declarations from unrelated selectors or breakpoints from
// accidentally satisfying the regression checks.
function declarations(parent, selector) {
  const result = {}
  for (const node of parent.nodes || []) {
    if (node.type !== 'rule' || !node.selectors.includes(selector)) {
      continue
    }
    for (const declaration of node.nodes || []) {
      if (declaration.type === 'decl') {
        result[declaration.prop] = declaration.value
      }
    }
  }
  return result
}

function containerName() {
  const root = declarations(css, '.video-editor')
  if (root.container) {
    assert.match(root.container, /\/\s*inline-size$/u)
    return root.container.split('/')[0].trim()
  }
  assert.equal(root['container-type'], 'inline-size')
  assert.ok(root['container-name'], '工作台必须声明具名尺寸容器')
  return root['container-name']
}

function containerQueries() {
  const name = containerName()
  return css.nodes.filter(node => node.type === 'atrule' && node.name === 'container')
    .map((node) => {
      const condition = node.params.match(/^([\w-]+)\s*\(max-width:\s*(\d+)px\)$/u)
      assert.ok(condition, `测试需要明确的工作台宽度断点：${node.params}`)
      assert.equal(condition[1], name)
      return { node, maxWidth: Number(condition[2]) }
    })
}

function declarationsAtWidth(selector, width, viewportWidth = width + 292) {
  const result = {}
  const name = containerName()
  for (const node of css.nodes) {
    if (node.type === 'rule') {
      Object.assign(result, declarations({ nodes: [node] }, selector))
    }
    else if (node.type === 'atrule' && ['container', 'media'].includes(node.name)) {
      const maxWidth = node.params.match(/\(max-width:\s*(\d+)px\)/u)
      if (!maxWidth) {
        continue
      }
      const availableWidth = node.name === 'container' ? width : viewportWidth
      if (node.name === 'container') {
        assert.ok(node.params.startsWith(`${name} `))
      }
      if (availableWidth <= Number(maxWidth[1])) {
        Object.assign(result, declarations(node, selector))
      }
    }
  }
  return result
}

test('根单列和时间线不能以内容最小宽度撑开整个工作台', () => {
  const editor = declarations(css, '.video-editor')
  assert.equal(editor['grid-template-columns'], 'minmax(0, 1fr)')
  assert.equal(editor['min-width'], '0')
  assert.equal(editor.width, '100%')
  assert.equal(editor['overflow-x'], 'hidden')
  assert.equal(editor['overflow-y'], 'auto', '工具栏换行或错误提示增高后，底部时间线必须仍可滚动访问')
  assert.equal(declarations(css, '.timeline-panel')['min-width'], '0')
  assert.equal(declarations(css, '.timeline-panel')['grid-template-columns'], 'minmax(0, 1fr)')
  assert.equal(declarations(css, '.timeline-scroll')['grid-template-columns'], '176px minmax(0, 1fr)')
  assert.equal(declarations(css, '.timeline-scroll').overflow, 'hidden')
  assert.equal(declarations(css, '.tracks-viewport').overflow, 'auto')
  assert.equal(declarationsAtWidth('.timeline-toolbar', 707)['flex-wrap'], 'wrap')
  assert.equal(declarationsAtWidth('.clip-actions', 707)['flex-wrap'], 'wrap')
  assert.equal(declarationsAtWidth('.timeline-zoom', 707)['max-width'], '100%')
  assert.match(editor['grid-template-rows'], /^minmax\(58px, auto\)/u)
  assert.match(declarations(css, '.timeline-panel')['grid-template-rows'], /^minmax\(42px, auto\)/u)
})

test('宽屏仍保留原有左栏286、预览至少360、右栏304的三列', () => {
  assert.equal(
    declarations(css, '.editor-panels')['grid-template-columns'],
    '286px minmax(360px, 1fr) 304px',
  )
  assert.equal(
    declarationsAtWidth('.editor-panels', 1252)['grid-template-columns'],
    '286px minmax(360px, 1fr) 304px',
  )
})

test('响应式按工作台实际宽度触发，不能继续依赖浏览器1280断点', () => {
  const queries = containerQueries()
  assert.deepEqual(queries.map(query => query.maxWidth), [950, 700, 500])
  for (const viewportWidth of [1000, 1280, 1545]) {
    assert.equal(
      declarationsAtWidth('.editor-panels', 707, viewportWidth)['grid-template-columns'],
      '200px minmax(220px, 1fr) 280px',
      '相同的实际容器宽度不能因视口大小而重新变成850px最小三列',
    )
  }
})

test('已复现的707px工作台不再保留最小850px三列', () => {
  for (const width of [701, 707, 870, 950]) {
    const compact = declarationsAtWidth('.editor-panels', width)
    assert.equal(compact['grid-template-columns'], '200px minmax(220px, 1fr) 280px')
    const columns = compact['grid-template-columns'].match(/^(\d+)px minmax\((\d+)px, 1fr\) (\d+)px$/u)
    const minimumWidth = columns.slice(1).reduce((total, value) => total + Number(value), 0)
    assert.ok(minimumWidth <= width, `${width}px容器放不下最小${minimumWidth}px的三列`)
  }
})

test('700px及以下预览在上、两侧栏在下，500px及以下变成可滚动单列', () => {
  for (const width of [501, 650, 700]) {
    assert.equal(declarationsAtWidth('.editor-panels', width)['grid-template-columns'], 'repeat(2, minmax(0, 1fr))')
    assert.equal(declarationsAtWidth('.preview-panel', width)['grid-column'], '1 / -1')
    assert.equal(declarationsAtWidth('.preview-panel', width)['grid-row'], '1')
    assert.equal(declarationsAtWidth('.editor-left', width)['grid-column'], '1')
    assert.equal(declarationsAtWidth('.editor-left', width)['grid-row'], '2')
    assert.equal(declarationsAtWidth('.editor-right', width)['grid-column'], '2')
    assert.equal(declarationsAtWidth('.editor-right', width)['grid-row'], '2')
  }
  for (const width of [320, 499, 500]) {
    const panels = declarationsAtWidth('.editor-panels', width)
    assert.equal(panels['grid-template-columns'], 'minmax(0, 1fr)')
    assert.equal(panels.overflow, 'auto', '单列内容必须仍可滚动访问，不能只截掉后面的面板')
    assert.equal(declarationsAtWidth('.editor-left', width)['grid-column'], '1')
    assert.equal(declarationsAtWidth('.editor-left', width)['grid-row'], '1')
    assert.equal(declarationsAtWidth('.preview-panel', width)['grid-row'], '2')
    assert.equal(declarationsAtWidth('.editor-right', width)['grid-column'], '1')
    assert.equal(declarationsAtWidth('.editor-right', width)['grid-row'], '3')
  }
})

test('右侧状态标签和候选操作允许换行，长文案不会制造横向滚动', () => {
  assert.equal(declarations(css, '.ai-video-transport-status')['flex-wrap'], 'wrap')
  assert.equal(declarations(css, '.ai-candidate-actions')['flex-wrap'], 'wrap')
  assert.equal(declarations(css, '.ai-candidate-actions')['max-width'], '144px')
  assert.equal(declarations(css, '.ai-video-transport-status :deep(.el-tag)')['max-width'], '100%')
  assert.equal(declarations(css, '.ai-video-transport-status :deep(.el-tag)')['white-space'], 'normal')
  assert.equal(declarations(css, '.ai-video-transport-status :deep(.el-tag__content)')['overflow-wrap'], 'anywhere')
  assert.equal(declarations(css, '.ai-video-transport-status > .el-button').flex, 'none')
  assert.equal(declarations(css, '.ai-candidate-results article')['min-width'], '0')
  assert.equal(declarations(css, '.ai-candidate-results strong')['text-overflow'], 'ellipsis')
  assert.equal(declarations(css, '.ai-candidate-results small')['text-overflow'], 'ellipsis')
})

test('AI视频成功结果的原生播放器、说明与按钮同样限制在右栏内', () => {
  const result = declarations(css, '.ai-video-completed-result')
  assert.equal(result['min-width'], '0')
  assert.equal(result['grid-template-columns'], 'minmax(0, 1fr)')
  const video = declarations(css, '.ai-video-completed-result video')
  assert.equal(video.width, '100%')
  assert.equal(video['min-width'], '0')
  assert.equal(video['max-width'], '100%')
  assert.equal(declarations(css, '.ai-video-result-copy')['flex-wrap'], 'wrap')
  assert.equal(declarations(css, '.ai-video-result-copy > span')['flex-wrap'], 'wrap')
  assert.equal(declarations(css, '.ai-video-result-copy > span')['max-width'], '100%')
})

test('独立视频和完整创作仍共用唯一工作台，不新增另一套响应式页面', () => {
  const taskSource = readFileSync(
    new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url),
    'utf8',
  )
  const task = parse(taskSource, { filename: 'ContentTaskView.vue' })
  assert.deepEqual(task.errors, [])
  assert.match(task.descriptor.scriptSetup.content, /import VideoEditorWorkspace from '\.\/video-editor\/VideoEditorWorkspace\.vue'/u)
  const visibility = task.descriptor.scriptSetup.content.match(/const showVideoEditor = computed\(([\s\S]*?)\n\)/u)
  assert.ok(visibility)
  assert.match(visibility[1], /currentMode\.value === 'video'/u)
  assert.match(visibility[1], /currentMode\.value === 'full'[\s\S]*activeStageKey\.value === 'video_preview'/u)
  const editors = [...task.descriptor.template.content.matchAll(/<VideoEditorWorkspace\b[^>]*>/gu)]
  assert.equal(editors.length, 1)
  assert.match(editors[0][0], /v-if="showVideoEditor"/u)
})
