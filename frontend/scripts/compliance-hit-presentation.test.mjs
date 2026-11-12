import assert from 'node:assert/strict'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import { complianceFieldLabel, complianceHitPresentation } from '../src/views/agent/content/contentWorkflow.ts'

test('合规字段路径显示为可读位置', () => {
  assert.equal(complianceFieldLabel('content.candidates[0].body'), '候选文案 1 · 文案正文')
  assert.equal(complianceFieldLabel('content.selected_candidate.body'), '当前选用文案 · 文案正文')
  assert.equal(complianceFieldLabel('body'), '文案正文')
})

test('旧命中只有 context 时仍能标出禁用词', () => {
  const presented = complianceHitPresentation({
    rule_id: 'rule-1',
    phrase: '雅酿',
    rule_type: 'forbidden',
    level: 'forbidden',
    field: 'content.candidates[0].body',
    context: '晚辈的举杯间透着敬意。53°雅酿·小坛酒（1.5L）大容量。',
  })
  assert.equal(presented.typeLabel, '禁用词')
  assert.equal(presented.fieldLabel, '候选文案 1 · 文案正文')
  assert.equal(presented.match, '雅酿')
  assert.match(presented.prefix, /举杯间透着敬意/u)
  assert.match(presented.suffix, /小坛酒/u)
})
