import assert from 'node:assert/strict'
import process from 'node:process'

import {
  finalConfirmationRetryDecision,
  stableWorkflowArtifactIds,
  workflowFinalizationDecision,
  workflowStageBaseline,
} from '../src/views/agent/content/video-editor/workflowFinalization.ts'

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

function stage(version, selected = [], confirmed = []) {
  return {
    version,
    selected_artifact_ids: selected.length ? { video: selected } : {},
    confirmed_artifact_ids: confirmed.length ? { video: confirmed } : {},
  }
}

test('工作流素材 ID 去重排序后形成稳定 CAS 基线', () => {
  assert.deepEqual(
    stableWorkflowArtifactIds({ video: ['clip-b', 'clip-a'], legacy: ['clip-a'] }),
    ['clip-a', 'clip-b'],
  )
  assert.deepEqual(workflowStageBaseline(stage(7, ['ai-clip'])), {
    version: 7,
    selectedArtifactIds: ['ai-clip'],
    confirmedArtifactIds: [],
  })
})

test('已有 AI clip 的基线未变化时允许最终成片替换并确认', () => {
  const beforeRender = stage(11, ['ai-clip'])
  const baseline = workflowStageBaseline(beforeRender)
  assert.equal(
    workflowFinalizationDecision(stage(11, ['ai-clip']), baseline, 'final-video'),
    'select_and_confirm',
  )
})

test('渲染期间版本或选择发生变化时绝不覆盖', () => {
  const baseline = workflowStageBaseline(stage(11, ['ai-clip']))
  assert.equal(
    workflowFinalizationDecision(stage(12, ['ai-clip']), baseline, 'final-video'),
    'conflict',
  )
  assert.equal(
    workflowFinalizationDecision(stage(11, ['other-clip']), baseline, 'final-video'),
    'conflict',
  )
})

test('最终成片已确认视为成功，确认冲突只允许同一最终选择重试', () => {
  const baseline = workflowStageBaseline(stage(3))
  assert.equal(
    workflowFinalizationDecision(stage(4, ['final-video'], ['final-video']), baseline, 'final-video'),
    'already_confirmed',
  )
  assert.equal(
    workflowFinalizationDecision(
      stage(4, ['final-video'], ['final-video', 'other-video']),
      baseline,
      'final-video',
    ),
    'conflict',
  )
  const mixedConfirmed = stage(4, ['final-video'], ['final-video', 'other-video'])
  assert.equal(
    workflowFinalizationDecision(
      mixedConfirmed,
      workflowStageBaseline(mixedConfirmed),
      'final-video',
    ),
    'conflict',
  )
  assert.equal(finalConfirmationRetryDecision(stage(5, ['final-video']), 'final-video'), 'confirm_selected')
  assert.equal(finalConfirmationRetryDecision(stage(5, ['other-video']), 'final-video'), 'conflict')
  assert.equal(
    finalConfirmationRetryDecision(stage(5, ['final-video'], ['other-video']), 'final-video'),
    'conflict',
  )
})

if (failures.length) {
  process.exitCode = 1
}
else {
  console.info('Workflow finalization tests passed: 4 checks passed.')
}
