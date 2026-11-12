import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import process from 'node:process'

import {
  imageGenerationFormState,
  latestGenerationRequestedCount,
  recoverImageGenerationDraft,
} from '../src/views/agent/content/imageGenerationDraft.ts'

const failures = []

function test(name, callback) {
  try {
    callback()
    console.info(`PASS ${name}`)
  }
  catch (error) {
    failures.push({ name, error })
    console.error(`FAIL ${name}`)
    console.error(error)
  }
}

test('latest image revision inherits scene and style from the previous successful run', () => {
  const recovered = recoverImageGenerationDraft([
    {
      created_at: '2026-09-06T12:30:00Z',
      requested_count: 1,
      parameters: {
        prompt: '把背景改成夜间宴会',
        ratio: '16:9',
        reference_artifact_ids: ['image-source'],
        reference_asset_ids: ['old-product-image'],
        knowledge_resource_ids: ['old-product'],
        form_state: { count: 4 },
      },
    },
    {
      created_at: '2026-09-06T12:00:00Z',
      requested_count: 4,
      parameters: {
        prompt: '中秋团圆宴',
        scene: '商务宴请',
        style: '写实摄影',
        ratio: '1:1',
        size: '2K',
      },
    },
  ])

  assert.equal(recovered.image_prompt, '把背景改成夜间宴会')
  assert.equal(recovered.image_scene, '商务宴请')
  assert.equal(recovered.image_style, '写实摄影')
  assert.equal(recovered.image_ratio, '16:9')
  assert.equal(recovered.image_size, '2K')
  assert.equal(recovered.count, 4)
  assert.equal('reference_artifact_ids' in recovered, false)
  assert.equal('reference_asset_ids' in recovered, false)
  assert.equal('knowledge_resource_ids' in recovered, false)
})

test('an explicit empty form snapshot does not resurrect an older scene or style', () => {
  const recovered = recoverImageGenerationDraft([
    {
      created_at: '2026-09-06T12:30:00Z',
      parameters: {
        form_state: {
          image_prompt: '',
          image_scene: '',
          image_style: '',
          image_ratio: '',
          image_size: '',
          reference_artifact_ids: [],
          reference_asset_ids: [],
          knowledge_resource_ids: [],
        },
      },
    },
    {
      created_at: '2026-09-06T12:00:00Z',
      parameters: { scene: '节日礼赠', style: '国风雅致' },
    },
  ])

  assert.equal(recovered.image_scene, '')
  assert.equal(recovered.image_style, '')
  assert.equal('reference_asset_ids' in recovered, false)
})

test('artifact metadata restores durable fields when generation-run history is unavailable', () => {
  const recovered = recoverImageGenerationDraft([], [
    {
      kind: 'image',
      version: 3,
      content: {
        scene: '品牌文化空间',
        style: '简约高级',
        ratio: '9:16',
        size: '4K',
        reference_asset_ids: ['asset-a', 'asset-a', 'asset-b'],
      },
    },
  ])

  assert.equal(recovered.image_scene, '品牌文化空间')
  assert.equal(recovered.image_style, '简约高级')
  assert.equal('reference_asset_ids' in recovered, false)
  assert.equal('knowledge_resource_ids' in recovered, false)
  assert.equal('image_prompt' in recovered, false)
})

test('legacy single-image revision falls back to the latest main batch count', () => {
  const recovered = recoverImageGenerationDraft([
    {
      requested_count: 1,
      parameters: {
        prompt: '把背景换成夜景',
        reference_artifact_ids: ['image-source'],
      },
    },
    { requested_count: 4, parameters: { scene: '产品静物展示' } },
  ])
  assert.equal(recovered.count, 4)
})

test('other generation stages restore the newest successful requested count', () => {
  assert.equal(latestGenerationRequestedCount([
    { requested_count: 5 },
    { requested_count: 2 },
  ]), 5)
  assert.equal(latestGenerationRequestedCount([{ requested_count: 0 }]), undefined)
})

test('form snapshots clone arrays before sending them to generation history', () => {
  const value = {
    count: 4,
    image_prompt: '产品海报',
    image_scene: '产品静物展示',
    image_style: '品牌宣传',
    image_ratio: '1:1',
    image_size: '2K',
    reference_artifact_ids: ['artifact-a'],
    reference_asset_ids: ['asset-a'],
    knowledge_resource_ids: ['knowledge-a'],
  }
  const snapshot = imageGenerationFormState(value)
  value.reference_asset_ids.push('asset-b')
  assert.equal(snapshot.count, 4)
  assert.deepEqual(snapshot.reference_asset_ids, ['asset-a'])
})

test('task view loads successful image runs and records the exact form state', () => {
  const taskView = readFileSync(
    new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url),
    'utf8',
  )
  const api = readFileSync(new URL('../src/api/agent/content.ts', import.meta.url), 'utf8')
  assert.match(taskView, /contentApi\.listGenerationRuns\(props\.taskId/u)
  assert.match(taskView, /status: 'succeeded',[\s\S]*?pageSize: activeStageKey\.value === 'image' \? 10 : 1/u)
  assert.match(taskView, /latestGenerationRequestedCount\(generationRuns\)/u)
  assert.match(taskView, /generationForm\.count = latestRequestedCount/u)
  assert.match(taskView, /recoverImageGenerationDraft\(/u)
  assert.match(taskView, /form_state: imageGenerationFormState\(generationForm\)/u)
  assert.match(api, /\/generation-runs`/u)
  assert.match(api, /status: options\.status/u)
  assert.match(api, /page_size: options\.pageSize/u)
})

if (failures.length) {
  process.exitCode = 1
}
else {
  console.info('Image generation draft tests passed: 7 checks passed.')
}
