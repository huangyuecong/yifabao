import assert from 'node:assert/strict'
import process from 'node:process'

import {
  isProviderNativeVideoReference,
  mediaMetadataDurationSeconds,
  projectAssetDurationSeconds,
} from '../src/views/agent/content/video-editor/assetMedia.ts'

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

test('历史 AI 视频从 provenance request 读取参考视频时长', () => {
  assert.equal(
    projectAssetDurationSeconds({
      metadata: {
        source: 'ai-generated',
        media_kind: 'video',
        provenance: { request: { duration_seconds: 4 } },
      },
    }),
    4,
  )
})

test('新字段和媒体探测时长优先于历史 provenance', () => {
  assert.equal(
    projectAssetDurationSeconds({
      metadata: {
        duration_seconds: 5,
        probe: { duration_seconds: 6 },
        provenance: { request: { duration_seconds: 4 } },
      },
    }),
    5,
  )
  assert.equal(
    projectAssetDurationSeconds({
      metadata: {
        probe: { duration_seconds: 6 },
        provenance: { request: { duration_seconds: 4 } },
      },
    }),
    6,
  )
})

test('企业知识库媒体同样读取探测与历史生成时长', () => {
  assert.equal(mediaMetadataDurationSeconds({ probe: { duration_seconds: 37.5 } }), 37.5)
  assert.equal(
    mediaMetadataDurationSeconds({ provenance: { request: { duration_seconds: 12 } } }),
    12,
  )
  assert.equal(
    mediaMetadataDurationSeconds({
      duration_seconds: 0,
      probe: { duration_seconds: 37.5 },
    }),
    37.5,
  )
  assert.equal(mediaMetadataDurationSeconds({ probe: { duration_seconds: 0 } }), undefined)
})

test('仅放行当前模型同 provider 的 AI 原生视频引用', () => {
  const asset = {
    source_type: 'artifact',
    metadata: {
      source: 'ai-generated',
      provider_native_reference: {
        eligible: true,
        provider_id: 'ark-video',
        transport: 'provider-managed-output-https',
        reference_kind: 'content-generation-task-output',
      },
    },
  }
  assert.equal(isProviderNativeVideoReference(asset, true, 'ark-video'), true)
  assert.equal(isProviderNativeVideoReference(asset, false, 'ark-video'), false)
  assert.equal(isProviderNativeVideoReference(asset, true, 'another-provider'), false)
  assert.equal(
    isProviderNativeVideoReference(
      { ...asset, metadata: { ...asset.metadata, source: 'local-upload' } },
      true,
      'ark-video',
    ),
    false,
  )
})

if (failures.length) {
  process.exitCode = 1
}
else {
  console.info('Video editor asset media tests passed: 4 checks passed.')
}
