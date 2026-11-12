/* eslint-disable no-new-func, test/no-import-node-test */
// Compile the production Vue template; only Element Plus rendering is stubbed.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as Vue from 'vue'
import { compileTemplate, parse } from 'vue/compiler-sfc'
import { renderToString } from 'vue/server-renderer'
import { serviceStatusPresentation } from '../src/views/agent/content/serviceStatusPresentation.ts'

const source = readFileSync(new URL('../src/views/agent/content/ContentServicesView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const presentation = `${descriptor.template.content.split('<section class="capabilities-panel" aria-labelledby="capability-title">')[0]}</div>`
const compiled = compileTemplate({ source: presentation, filename: 'ContentServiceStatus.vue', id: 'service-status-test', compilerOptions: { mode: 'function' } })
assert.deepEqual(compiled.errors, [])
const render = new Function('Vue', compiled.code)(Vue)

async function renderStatus(loadError = '') {
  const app = Vue.createSSRApp(Vue.defineComponent({
    render,
    setup: () => ({
      loading: false,
      loadError,
      serviceRows: ['available', 'unavailable', 'unknown'].map((status, index) => ({
        capability: `capability-${index}`,
        provider_id: `provider-${index}`,
        status,
        checked_at: '2026-09-06T10:00:00Z',
        message: 'ProviderCallError: internal diagnostic',
      })),
      capabilityLabels: { 'capability-0': '图片生成', 'capability-1': 'AI 视频生成', 'capability-2': '背景音乐' },
      serviceInfo: () => ({ name: '测试服务', model: '模型/服务 ID：test-model' }),
      serviceProvider: () => ({ model: 'test-model', external: true, mode: 'external' }),
      modelConfiguration: () => undefined,
      capabilityIcon: () => 'span',
      modeLabel: () => '云端模型',
      modelSettings: null,
      serviceStatusPresentation,
      overallPresentation: serviceStatusPresentation(loadError ? undefined : 'degraded'),
      formatDate: () => '2026/09/06 18:00:00',
      load() {},
    }),
  }))
  for (const name of ['ElTag', 'ElButton', 'ElEmpty', 'ContentModelSettings', 'ContentChatModels', 'Connection']) {
    app.component(name, { setup: (_, { slots }) => () => Vue.h('span', {}, slots.default?.()) })
  }
  app.component('ElAlert', {
    props: ['title', 'description'],
    setup: props => () => Vue.h('div', { role: 'alert' }, `${props.title}：${props.description}`),
  })
  return renderToString(app)
}

test('服务卡片仅展示名称、状态与检查时间，不重复显示异常重试说明', async () => {
  const html = await renderStatus()
  for (const label of ['图片生成', 'AI 视频生成', '背景音乐', '刷新服务信息']) {
    assert.ok(html.includes(label))
  }
  // The last check time is on each card, as the footer tooltip.
  assert.equal((html.match(/title="2026\/09\/06 18:00:00"/g) ?? []).length, 3)
  assert.doesNotMatch(html, /调用异常|再次尝试|ProviderCallError|internal diagnostic/u)
  assert.doesNotMatch(source, /serviceMessage/u)
})

test('删除常驻说明不隐藏真正的服务读取错误', async () => {
  const html = await renderStatus('暂时无法连接内容服务，请稍后重试')
  assert.match(html, /role="alert"/u)
  assert.match(html, /部分服务状态无法读取/u)
  assert.match(html, /暂时无法连接内容服务，请稍后重试/u)
})

test('不可用、降级、停用和未知状态不伪装成功', async () => {
  assert.deepEqual(serviceStatusPresentation('available'), { label: '可使用', type: 'success', neutral: false })
  assert.equal(serviceStatusPresentation('unavailable').type, 'danger')
  assert.equal(serviceStatusPresentation('degraded').type, 'warning')
  assert.equal(serviceStatusPresentation('disabled').neutral, true)
  assert.equal(serviceStatusPresentation('unknown').label, '状态待确认')
  const html = await renderStatus()
  assert.match(html, /暂不可用/u)
  assert.match(html, /部分服务受限/u)
  assert.match(html, /状态待确认/u)
})
