import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { createRequire } from 'node:module'
import process from 'node:process'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright')
const base = process.env.UI_TEST_URL || 'http://127.0.0.1:8200/content/agent/content'
const browser = await chromium.launch({ channel: 'msedge', headless: true })
const page = await browser.newPage({ reducedMotion: 'reduce' })
page.setDefaultNavigationTimeout(60000)
let catalog = []
let failCatalog = false
let reads = 0
const writes = []
const errors = []
const resource = (id, name, kind = 'document') => ({
  id,
  resource_entry_id: id,
  name,
  resource_kind: kind,
  category: '测试资料',
  text_content: '用于界面回归测试的说明',
  active: true,
  version: 1,
  data_level: 'internal',
  metadata: {},
  created_at: '2026-09-21T00:00:00Z',
  updated_at: '2026-09-21T00:00:00Z',
})
page.on('pageerror', error => errors.push(error.message))
await page.route('**/content/api/**', async (route) => {
  const request = route.request()
  const path = new URL(request.url()).pathname
  if (request.method() === 'GET' && path.endsWith('/api/v1/library/items')) {
    reads++
    return failCatalog
      ? route.fulfill({ status: 503, json: { detail: '测试知识库读取失败' } })
      : route.fulfill({ json: catalog })
  }
  if (['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
    return route.continue()
  }
  writes.push({ method: request.method(), path })
  if (request.method() === 'POST' && /\/tasks\/[^/]+\/image-references$/.test(path)) {
    return route.fulfill({ json: {
      ...resource('local-reference-test', '独立参考图.png', 'image'),
      source_type: 'task-image-reference',
      metadata: { source_type: 'task-image-reference' },
    } })
  }
  return route.abort('blockedbyclient')
})

try {
  await page.goto(`${base}/create/full`, { waitUntil: 'networkidle' })
  const button = page.getByRole('button', { name: '浏览知识库', exact: true })
  assert.ok(await button.isEnabled(), '空知识库仍允许打开并重新读取')
  catalog = [resource('new-doc', '刚刚新增的企业资料')]
  const before = reads
  await button.click()
  const dialog = page.locator('.content-knowledge-dialog')
  await dialog.getByText('刚刚新增的企业资料', { exact: true }).waitFor()
  assert.equal(reads, before + 1, '每次打开重新读取列表')
  await dialog.locator('.knowledge-card').filter({ hasText: '刚刚新增的企业资料' }).click()
  await dialog.getByRole('button', { name: '确认选择' }).click()
  await page.locator('.selected-knowledge-row').getByText('刚刚新增的企业资料', { exact: true }).waitFor()

  catalog.push(resource('second-doc', '另一个页面新增的资料'))
  await button.click()
  await dialog.getByText('另一个页面新增的资料', { exact: true }).waitFor()
  assert.ok(await dialog.getByRole('checkbox', { name: '选择刚刚新增的企业资料', exact: true }).isChecked())
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  failCatalog = true
  await button.click()
  await dialog.getByRole('button', { name: '重新加载' }).waitFor()
  assert.ok(await dialog.getByRole('button', { name: '确认选择' }).isDisabled())
  failCatalog = false
  await dialog.getByRole('button', { name: '重新加载' }).click()
  await dialog.getByText('另一个页面新增的资料', { exact: true }).waitFor()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()

  // Read an editable image task; all writes above are intercepted, including uploads.
  const tasksResponse = await page.request.get(`${new URL(base).origin}/content/api/v1/tasks`)
  const tasks = await tasksResponse.json()
  const task = tasks.find(item => item.mode === 'image' && item.current_stage === 'image' && item.status === 'draft')
  assert.ok(task, '需要一个已有可编辑图片任务作为只读页面样本')
  await page.goto(`${base}/tasks/${task.id}/stages/image`, { waitUntil: 'networkidle' })
  const uploader = page.locator('.image-reference-uploader')
  await uploader.locator('input[type=file]').setInputFiles({
    name: '独立参考图.png',
    mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=', 'base64'),
  })
  await uploader.getByRole('button', { name: '上传并作为参考图' }).click()
  await uploader.getByText('独立参考图.png', { exact: true }).waitFor()
  await page.waitForLoadState('networkidle')
  assert.deepEqual(writes, [{ method: 'POST', path: `/content/api/v1/tasks/${task.id}/image-references` }])
  await page.getByRole('button', { name: '调整本次知识库引用', exact: true }).click()
  await dialog.getByText('刚刚新增的企业资料', { exact: true }).waitFor()
  assert.equal(await dialog.getByText('独立参考图.png', { exact: true }).count(), 0)
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await uploader.getByText('独立参考图.png', { exact: true }).waitFor()
  assert.deepEqual(errors, [])
  console.info('PASS 空库重新读取、新资料即时可选、已选项保留、失败重试、参考图独立上传、刷新不丢参考图；真实写请求全部拦截。')
}
finally {
  await browser.close()
}
