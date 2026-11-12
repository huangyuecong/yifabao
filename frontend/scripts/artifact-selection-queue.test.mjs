import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import process from 'node:process'

import {
  createLatestSelectionQueue,
  nextArtifactSelection,
} from '../src/views/agent/content/artifactSelectionQueue.ts'

const failures = []

async function test(name, callback) {
  try {
    await callback()
    console.info(`PASS ${name}`)
  }
  catch (error) {
    failures.push({ name, error })
    console.error(`FAIL ${name}`)
    console.error(error)
  }
}

await test('rapid image selection keeps every click and only persists the latest queued value', async () => {
  let releaseFirst
  const firstGate = new Promise(resolve => releaseFirst = resolve)
  const writes = []
  let concurrentWrites = 0
  let maxConcurrentWrites = 0
  const queue = createLatestSelectionQueue(async (ids) => {
    writes.push([...ids])
    concurrentWrites += 1
    maxConcurrentWrites = Math.max(maxConcurrentWrites, concurrentWrites)
    if (writes.length === 1) {
      await firstGate
    }
    concurrentWrites -= 1
  })

  queue.enqueue(['image-1'])
  await Promise.resolve()
  queue.enqueue(['image-1', 'image-2'])
  queue.enqueue(['image-1', 'image-2', 'image-3'])
  queue.enqueue(['image-1', 'image-2', 'image-3', 'image-4'])
  releaseFirst()

  assert.equal(await queue.flush(), true)
  assert.deepEqual(writes, [
    ['image-1'],
    ['image-1', 'image-2', 'image-3', 'image-4'],
  ])
  assert.equal(maxConcurrentWrites, 1)
})

await test('a flush reports an in-flight failure but a reconciled later action can continue', async () => {
  let shouldFail = true
  let handledErrors = 0
  const queue = createLatestSelectionQueue(
    async () => {
      if (shouldFail) {
        shouldFail = false
        throw new Error('version conflict')
      }
    },
    { onError: () => handledErrors += 1 },
  )

  queue.enqueue(['image-1'])
  assert.equal(await queue.flush(), false)
  assert.equal(handledErrors, 1)
  queue.enqueue(['image-1', 'image-2'])
  assert.equal(await queue.flush(), true)
})

await test('flush stays failed when called after persistence fails but while reconciliation is running', async () => {
  let releaseRecovery
  let markRecoveryStarted
  const recoveryStarted = new Promise(resolve => markRecoveryStarted = resolve)
  const recoveryGate = new Promise(resolve => releaseRecovery = resolve)
  const queue = createLatestSelectionQueue(
    async () => {
      throw new Error('version conflict')
    },
    {
      onError: async () => {
        markRecoveryStarted()
        await recoveryGate
      },
    },
  )

  queue.enqueue(['image-1'])
  await recoveryStarted
  const flushed = queue.flush()
  releaseRecovery()
  assert.equal(await flushed, false)
})

await test('stage selection semantics keep images multi-select and text stages single-select', () => {
  const images = [
    { id: 'image-1', kind: 'image' },
    { id: 'image-2', kind: 'image' },
  ]
  assert.deepEqual(nextArtifactSelection(['image-1'], images[1], 'image', images), [
    'image-1',
    'image-2',
  ])
  assert.deepEqual(nextArtifactSelection(['topic-1'], { id: 'topic-2', kind: 'topic' }, 'topic', []), [
    'topic-2',
  ])
})

await test('copy selection always keeps exactly one authoritative current version', () => {
  const copies = [
    { id: 'copy-1', kind: 'copy' },
    { id: 'copy-2', kind: 'copy' },
  ]
  assert.deepEqual(nextArtifactSelection(['copy-1'], copies[0], 'copy', copies), ['copy-1'])
  assert.deepEqual(nextArtifactSelection(['copy-1'], copies[1], 'copy', copies), ['copy-2'])
})

await test('voice and subtitle choices remain one-to-one and source paired', () => {
  const artifacts = [
    { id: 'voice-1', kind: 'voice', content: {} },
    { id: 'voice-2', kind: 'voice', content: {} },
    { id: 'subtitle-1', kind: 'subtitle', content: { source_artifact_id: 'voice-1' } },
    { id: 'subtitle-2', kind: 'subtitle', content: { source_artifact_id: 'voice-2' } },
  ]
  assert.deepEqual(
    nextArtifactSelection(['voice-1', 'subtitle-1'], artifacts[1], 'voice_subtitles', artifacts),
    ['voice-2'],
  )
  assert.deepEqual(
    nextArtifactSelection(['voice-1', 'subtitle-1'], artifacts[3], 'voice_subtitles', artifacts),
    ['voice-2', 'subtitle-2'],
  )
  assert.deepEqual(
    nextArtifactSelection(['voice-1', 'subtitle-1'], artifacts[2], 'voice_subtitles', artifacts),
    ['voice-1'],
  )
  assert.deepEqual(
    nextArtifactSelection(['voice-1', 'subtitle-1'], artifacts[0], 'voice_subtitles', artifacts),
    [],
  )
  assert.deepEqual(
    nextArtifactSelection(
      ['voice-1', 'subtitle-1'],
      { id: 'orphan-subtitle', kind: 'subtitle', content: { source_artifact_id: 'missing' } },
      'voice_subtitles',
      artifacts,
    ),
    ['voice-1', 'subtitle-1'],
  )
})

await test('task view flushes selection before confirmation and no longer reloads after every click', () => {
  const source = readFileSync(
    new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url),
    'utf8',
  )
  const toggleBlock = source.slice(
    source.indexOf('function toggleArtifact'),
    source.indexOf('async function chooseCandidate'),
  )
  const confirmBlock = source.slice(
    source.indexOf('async function confirmCurrentStage'),
    source.indexOf('async function skipCurrentStage'),
  )
  assert.match(toggleBlock, /artifactSelectionQueue\.enqueue/u)
  assert.match(
    toggleBlock,
    /if \(next\.length === previous\.length[\s\S]*?return[\s\S]*?artifactSelectionQueue\.enqueue/u,
  )
  assert.doesNotMatch(toggleBlock, /refreshWorkflowAndArtifacts/u)
  assert.match(confirmBlock, /await flushArtifactSelection\(\)/u)
  assert.match(source, /selectionSaving \? '正在保存选择…'/u)
  for (const [start, end] of [
    ['async function chooseCandidate', 'async function confirmCurrentStage'],
    ['async function runCompliance', 'function openArtifactEditor'],
    ['async function generateImageRevision', 'async function saveArtifactEdit'],
    ['async function saveArtifactEdit', 'async function transformCopy'],
    ['async function removeArtifact', 'async function downloadArtifact'],
    ['async function saveStoryboard', 'async function exportDirectFiles'],
  ]) {
    const block = source.slice(source.indexOf(start), source.indexOf(end))
    assert.match(block, /await flushArtifactSelection\(\)/u, `${start} must flush selection first`)
  }
})

if (failures.length) {
  process.exitCode = 1
}
else {
  console.info('Artifact selection queue tests passed: 7 checks passed.')
}
