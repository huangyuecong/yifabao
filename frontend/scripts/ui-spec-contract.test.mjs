import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import test from 'node:test'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const source = name => readFileSync(new URL('../' + name, import.meta.url), 'utf8')
const adapter = source('src/styles/brand-theme.scss')
const legacy = source('src/styles/element-plus.scss')
const theme = source('src/views/agent/content/content-theme.css')
const tokens = source('src/views/agent/content/xi-design-tokens.css')

test('every literal XI token reference in the adapter and changed layouts is defined', () => {
  const layouts = source('src/views/agent/content/video-editor/VideoEditorWorkspace.vue') + source('src/styles/index.scss')
  const all = tokens + adapter + theme + layouts
  const defined = new Set([...all.matchAll(/(--xi-[\w-]+)\s*:/g)].map(match => match[1]))
  for (const match of (adapter + theme + layouts).matchAll(/var\((--xi-[\w-]+)\s*\)/g)) {
    assert.ok(defined.has(match[1]), 'Undefined design token: ' + match[1])
  }
})
test('Web sizes and visual variants are preserved', () => {
  for (const size of ['--el-component-size: 32px', '--el-component-size-large: 40px', '--el-component-size-small: 28px']) {
    assert.ok(adapter.includes(size))
  }
  assert.match(adapter, /opacity: 0\.4/)
  assert.match(adapter, /is-round/)
  assert.match(adapter, /is-circle/)
  assert.doesNotMatch(legacy, /border: none !important|background-color: #[a-f0-9]+ !important/i)
  assert.doesNotMatch(source('src/styles/var.css'), /##[a-f0-9]+/i)
})
test('popups stay inside the viewport and use library stacking', () => {
  assert.ok(theme.includes('max-width: min(960px, calc(100vw - 32px))'))
  assert.doesNotMatch(theme, /z-index: 4000|pointer-events: auto/)
  assert.match(adapter, /focus-visible/)
  assert.match(adapter, /prefers-reduced-motion/)
})
test('entry loads the semantic adapter after the company library', () => {
  const main = source('src/main.ts')
  assert.ok(main.indexOf("import '@/styles/brand-theme.scss'") > main.indexOf("import '@gzxj/ui/dist/style.css'"))
})
test('business Vue templates compile, including empty and loading branches', async () => {
  const { parse, compileTemplate } = await import('vue/compiler-sfc')
  function check(dir) {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const file = join(dir, entry.name)
      if (entry.isDirectory()) check(file)
      else if (file.endsWith('.vue')) {
        const { descriptor, errors } = parse(readFileSync(file, 'utf8'))
        assert.deepEqual(errors, [], file)
        const result = compileTemplate({ source: descriptor.template.content, filename: file, id: file })
        assert.deepEqual(result.errors, [], file)
      }
    }
  }
  check(fileURLToPath(new URL('../src/views/agent/', import.meta.url)))
})
