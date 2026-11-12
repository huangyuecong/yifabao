import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const taskView = readFileSync(
  new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url),
  'utf8',
)

function section(start, end) {
  const startIndex = taskView.indexOf(start)
  const endIndex = taskView.indexOf(end, startIndex)
  assert.notEqual(startIndex, -1, `Missing section start: ${start}`)
  assert.notEqual(endIndex, -1, `Missing section end: ${end}`)
  return taskView.slice(startIndex, endIndex)
}

const recovery = section(
  'function isVersionConflictError',
  'async function refreshServiceState',
)
assert.match(recovery, /版本冲突\|版本已变化\|版本不匹配/u)
assert.match(recovery, /await refreshWorkflowAndArtifacts\(\)/u)
assert.match(recovery, /页面状态已自动同步到最新版本，请重试刚才的操作/u)

const generation = section('async function generateStage', 'async function waitForJob')
assert.match(
  generation,
  /handleWorkflowMutationError\(error, \{ reconcileOnAnyError: true \}\)/u,
)

for (const [start, end] of [
  ['async function confirmCurrentStage', 'async function skipCurrentStage'],
  ['async function skipCurrentStage', 'function navigateToStage'],
]) {
  assert.match(section(start, end), /await handleWorkflowMutationError\(error\)/u)
}

const selectionQueue = section(
  'const artifactSelectionQueue',
  'const activeStageKey',
)
assert.match(selectionQueue, /onError: async \(error\)/u)
assert.match(
  selectionQueue,
  /await handleWorkflowMutationError\(error, \{ reconcileOnAnyError: true \}\)/u,
)

console.info('Content version recovery contract passed.')
