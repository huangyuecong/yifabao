import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import process from 'node:process'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright')
const base = process.env.UI_TEST_URL || 'http://127.0.0.1:8200/content/agent/content'
const output = resolve('docs/verification/ui-theme-2026-09-21')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ channel: 'msedge', headless: true })
const results = []
const errors = []
const writes = []

function check(condition, label, details) {
  results.push({ label, passed: Boolean(condition), details })
  assert.ok(condition, `${label}: ${JSON.stringify(details)}`)
}

async function styles(locator) {
  return locator.evaluate(el => {
    const s = getComputedStyle(el)
    return { background: s.backgroundColor, color: s.color, border: s.borderColor, height: el.getBoundingClientRect().height, opacity: s.opacity }
  })
}

try {
  for (const width of [1920, 1280, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 1000 }, deviceScaleFactor: 1, reducedMotion: 'reduce' })
    page.on('pageerror', error => errors.push(error.message))
    // Exercise the UI without changing real tasks, rules, or generation jobs.
    await page.route('**/content/api/**', async route => {
      if (['GET', 'HEAD', 'OPTIONS'].includes(route.request().method())) return route.continue()
      writes.push({ method: route.request().method(), url: route.request().url() })
      if (route.request().method() === 'DELETE' && route.request().url().includes('/sensitive-rules/')) {
        return route.fulfill({ json: { deleted: true } })
      }
      return route.abort('blockedbyclient')
    })
    const shot = name => page.screenshot({ path: resolve(output, `${name}-${width}.png`), fullPage: true })
    const visit = async suffix => {
      await page.goto(base + suffix, { waitUntil: 'networkidle', timeout: 60000 })
      await page.locator('.content-agent-shell').waitFor()
    }
    const checkLayout = async selector => {
      const metrics = await page.locator(selector).evaluate(el => ({ width: el.clientWidth, scrollWidth: el.scrollWidth, right: el.getBoundingClientRect().right }))
      check(metrics.scrollWidth <= metrics.width + 1 && metrics.right <= width + 1, `${selector} fits without page overflow at ${width}`, metrics)
    }

    await visit('/services')
    const layout = await page.locator('.content-services-view').evaluate(el => ({ width: el.clientWidth, scrollWidth: el.scrollWidth, right: el.getBoundingClientRect().right }))
    check(layout.scrollWidth <= layout.width + 1 && layout.right <= width + 1, `services layout fits without page overflow at ${width}`, layout)
    const root = await page.evaluate(() => {
      const s = getComputedStyle(document.documentElement)
      return Object.fromEntries(['--xi-semantic-color-background-container', '--el-bg-color', '--el-color-warning-light-9', '--el-color-primary-light-3', '--xi-component-content-gap-md'].map(k => [k, s.getPropertyValue(k).trim()]))
    })
    check(Object.values(root).every(Boolean), `root theme variables resolve at ${width}`, root)
    const warning = await styles(page.locator('.service-grid .el-tag--warning').first())
    check(warning.background === 'rgb(254, 250, 246)' && warning.color === 'rgb(168, 115, 70)', `warning tag has specification colors at ${width}`, warning)
    const success = await styles(page.locator('.service-grid .el-tag--success').first())
    check(success.background === 'rgb(242, 251, 246)' && success.color === 'rgb(35, 143, 84)', `success tag has specification colors at ${width}`, success)
    await page.getByRole('button', { name: '刷新服务信息', exact: true }).hover()
    const hover = await styles(page.getByRole('button', { name: '刷新服务信息', exact: true }))
    check(hover.background !== 'rgba(0, 0, 0, 0)' && hover.opacity === '1', `button hover stays opaque at ${width}`, hover)
    await shot('services')

    await visit('/versions')
    await checkLayout('.history-view')
    const archive = await styles(page.locator('.history-list .el-tag--info').first())
    check(archive.background === 'rgb(245, 248, 255)', `history tag retains information color at ${width}`, archive)
    await shot('history')

    await visit('/library')
    await page.getByRole('tab', { name: '关键词、敏感词与禁用词' }).click()
    await checkLayout('.rule-layout')
    check(await page.locator('.rule-layout').getByRole('button', { name: '删除', exact: true }).count() > 0, `rule action reads delete at ${width}`)
    check(await page.locator('.rule-layout').getByRole('button', { name: '停用', exact: true }).count() === 0, `rule action no longer reads disable at ${width}`)
    if (width === 1920) {
      await page.locator('.rule-layout').getByRole('button', { name: '删除', exact: true }).first().click()
      await page.getByText('规则已删除', { exact: true }).waitFor()
    }
    await shot('rules')

    await visit('/create/full')
    await page.getByRole('button', { name: '浏览知识库', exact: true }).click()
    const dialog = page.locator('.content-knowledge-dialog')
    await dialog.waitFor()
    const metrics = await dialog.evaluate(el => {
      const rect = el.getBoundingClientRect()
      const footer = el.querySelector('.el-dialog__footer').getBoundingClientRect()
      const grid = el.querySelector('.knowledge-grid')
      const title = el.querySelector('.el-dialog__title').getBoundingClientRect()
      const titleOnTop = el.contains(document.elementFromPoint(title.left + 4, title.top + title.height / 2))
      return { background: getComputedStyle(el).backgroundColor, width: rect.width, left: rect.left, right: rect.right, bottom: rect.bottom, footerBottom: footer.bottom, gap: getComputedStyle(grid).gap, cardBorder: getComputedStyle(grid.firstElementChild).borderWidth, titleOnTop }
    })
    check(metrics.background === 'rgb(255, 255, 255)' && metrics.gap === '16px' && metrics.cardBorder === '1px', `knowledge dialog has opaque surface and card spacing at ${width}`, metrics)
    check(metrics.left >= 0 && metrics.right <= width + 1 && metrics.footerBottom <= 1000, `knowledge dialog and footer fit viewport at ${width}`, metrics)
    check(metrics.titleOnTop, `knowledge dialog title is not covered by navigation at ${width}`, metrics)
    await dialog.locator('.knowledge-card').first().hover()
    const cardHover = await styles(dialog.locator('.knowledge-card').first())
    check(cardHover.background !== 'rgba(0, 0, 0, 0)', `knowledge card hover stays opaque at ${width}`, cardHover)
    await shot('knowledge')
    await dialog.locator('.knowledge-cascader').click()
    const cascader = page.locator('.el-cascader__dropdown:visible')
    await cascader.waitFor()
    const popup = await styles(cascader)
    check(popup.background === 'rgb(255, 255, 255)' && popup.opacity === '1', `knowledge category popup is opaque at ${width}`, popup)
    await cascader.locator('.el-cascader-node').first().hover()
    const option = await styles(cascader.locator('.el-cascader-node').first())
    check(option.background !== 'rgba(0, 0, 0, 0)', `knowledge category hover retains background at ${width}`, option)
    await dialog.locator('.dialog-description').click()
    await dialog.getByRole('button', { name: '取消', exact: true }).click()

    await visit('/create/video')
    await page.locator('.editor-right').waitFor()
    const dimensions = await page.locator('.editor-right .two-fields').first().evaluate(el => [...el.querySelectorAll('.el-select__wrapper, .el-input-number')].map(control => ({ className: control.className, height: control.getBoundingClientRect().height, width: control.getBoundingClientRect().width, parentWidth: control.closest('.el-form-item').getBoundingClientRect().width })))
    check(dimensions.every(d => d.width <= d.parentWidth + 1) && Math.abs(dimensions[0].height - dimensions[1].height) <= 1, `editor canvas controls align and fit at ${width}`, dimensions)
    await page.locator('.editor-right .el-collapse-item__header').filter({ hasText: /^字幕/ }).click()
    await page.locator('.subtitle-generator-form').waitFor()
    await page.locator('.subtitle-generator-form').scrollIntoViewIfNeeded()
    await shot('editor-subtitle')
    await page.locator('.ai-video-options').scrollIntoViewIfNeeded()
    const options = await page.locator('.ai-video-options').evaluate(el => [...el.querySelectorAll('.el-select__wrapper')].map(control => ({ top: control.getBoundingClientRect().top, width: control.getBoundingClientRect().width, parentWidth: control.closest('.el-form-item').getBoundingClientRect().width })))
    check(options.every(d => d.width <= d.parentWidth + 1) && Math.max(...options.map(d => d.top)) - Math.min(...options.map(d => d.top)) <= 1, `video generation controls align at ${width}`, options)
    const tag = page.locator('.ai-video-transport-status .el-tag')
    const tagMetrics = await tag.evaluate(el => ({ height: el.clientHeight, contentHeight: el.firstElementChild.scrollHeight }))
    check(tagMetrics.height >= tagMetrics.contentHeight, `video transport tag does not clip at ${width}`, tagMetrics)
    await shot('editor-video')
    await page.close()
  }
  check(errors.length === 0, 'no browser runtime errors', errors)
  check(writes.length === 1 && writes[0].method === 'DELETE', 'only the intercepted deletion was attempted', writes)
}
finally {
  await writeFile(resolve(output, 'results.json'), JSON.stringify({ results, errors, writes }, null, 2))
  await browser.close()
}
console.log(`${results.length} UI browser checks passed. Screenshots: ${output}`)
