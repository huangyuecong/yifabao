import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import ts from 'typescript'

const read = relativePath => readFileSync(
  fileURLToPath(new URL(relativePath, import.meta.url)),
  'utf8',
)

const workflow = read('../src/views/agent/content/contentWorkflow.ts')
const workflowExports = {}
vm.runInNewContext(ts.transpileModule(workflow, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText, { exports: workflowExports })
const taskView = read('../src/views/agent/content/ContentTaskView.vue')
const artifactSelectionQueue = read('../src/views/agent/content/artifactSelectionQueue.ts')
const artifactCard = read('../src/views/agent/content/ContentArtifactCard.vue')
const editor = read('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue')
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

test('完整内容创作使用七阶段且第六阶段直接进入视频生成', () => {
  assert.match(
    workflow,
    /full:\s*\['configuration', 'topic', 'copy', 'compliance', 'image', 'video_preview', 'export'\]/u,
  )
  assert.match(workflow, /key: 'video_preview',[\s\S]*?label: '视频生成'/u)
  assert.match(editor, /完整流程 · 第 6 \/ 7 步 · 视频生成/u)
  assert.doesNotMatch(
    workflow,
    /full:\s*\[[^\]]*(?:'storyboard'|'voice_subtitles')[^\]]*\]/u,
  )
})

test('快捷视频只展示连续的三步，完整流程仍保留七步', () => {
  const { modeStageKeys, stageDefinition } = workflowExports
  assert.deepEqual([...modeStageKeys.video], ['configuration', 'video_preview', 'export'])
  assert.deepEqual([...modeStageKeys.video].map(key => stageDefinition(key).label), ['任务配置', '视频生成', '确认导出'])
  assert.equal(modeStageKeys.full.length, 7)
  assert.match(editor, /快捷视频 · 第 2 \/ 3 步 · 视频生成/u)
})

test('旧视频阶段路由回到视频生成，正常导出和其他入口不受影响', () => {
  const route = workflowExports.workflowStageRoute
  for (const mode of ['full', 'video']) {
    for (const stage of ['storyboard', 'voice_subtitles']) {
      assert.equal(route(mode, stage, 'export'), 'video_preview')
    }
    assert.equal(route(mode, 'export', 'video_preview'), 'export')
  }
  assert.equal(route('video', 'image', 'export'), 'video_preview')
  assert.equal(route('full', 'image', 'export'), 'image')
  assert.equal(route('image', 'storyboard', 'export'), 'export')
  assert.equal(route('copy', 'video_preview', 'voice_subtitles'), 'configuration')
})

test('任务页面使用统一阶段兼容路由', () => {
  assert.match(
    taskView,
    /workflowStageRoute\(task\.value\.mode, activeStageKey\.value, workflowStage\)/u,
  )
})

test('完整流程视频工作台展示当前文案且由服务端解析权威版本', () => {
  assert.match(editor, /当前标题与文案/u)
  assert.match(editor, /优先展示前序阶段当前使用版本/u)
  assert.match(editor, /currentTopicArtifact/u)
  assert.match(editor, /currentCopyArtifact/u)
  assert.match(
    editor,
    /const currentTopicArtifact = computed[\s\S]*?stage\?\.state === 'confirmed'[\s\S]*?stage\.confirmed_artifact_ids\.topic/u,
  )
  assert.match(
    editor,
    /const currentCopyArtifact = computed[\s\S]*?\['not_started', 'stale', 'skipped'\]\.includes\(stage\.state\)[\s\S]*?stage\.selected_artifact_ids\.copy/u,
  )
  assert.doesNotMatch(
    editor,
    /\['not_started', 'stale', 'skipped', 'failed'\]\.includes\(stage\.state\)/u,
  )
  assert.match(editor, /ids\.length !== 1/u)
  assert.match(editor, /artifact\.content\.legacy_mock !== true/u)
  assert.match(
    editor,
    /currentArtifactTitle\(displayedTopicArtifact\)[\s\S]*?\|\| currentArtifactTitle\(displayedCopyArtifact\)/u,
  )
  assert.doesNotMatch(editor, /已确认标题与文案|当前确认内容/u)
  assert.doesNotMatch(editor, /useConfirmedText\(/u)
  assert.match(editor, /source_copy_artifact_id:\s*null/u)
  assert.doesNotMatch(editor, /sourceCopyArtifactId/u)
  assert.match(editor, /本任务已确认素材/u)
  assert.match(editor, /全部导入时间线/u)
  assert.match(editor, /修改文案/u)
  assert.match(editor, /返回图片阶段/u)
  assert.match(taskView, /@workflow-edit-copy="handleWorkflowCopyEdit"/u)
  assert.match(taskView, /async function handleWorkflowCopyEdit\(\)[\s\S]*?navigateToStage\('copy'\)/u)
  assert.match(taskView, /navigateToStage\(currentMode\.value === 'full' \? 'image'/u)
})

test('旧任务没有 current 映射时只读回显最近合法历史文案', () => {
  const fallbackStart = editor.indexOf('function isUsableTextArtifact')
  const fallbackEnd = editor.indexOf('const visibleImportJobs', fallbackStart)
  const fallback = editor.slice(fallbackStart, fallbackEnd)
  assert.match(fallback, /!\['legacy_mock', 'stale', 'discarded'\]\.includes\(artifact\.status\)/u)
  assert.match(fallback, /artifact\.content\.legacy_mock !== true/u)
  assert.match(fallback, /compareContentDateTimeDescending\(left\.created_at, right\.created_at\)/u)
  assert.match(
    fallback,
    /historicalCopyArtifact\.value\?\.content\.source_title_artifact_id/u,
  )
  assert.match(
    fallback,
    /artifact\.id === sourceTitleId && isUsableTextArtifact\(artifact, 'topic'\)/u,
  )
  assert.match(
    fallback,
    /if \(historicalCopyArtifact\.value\)[\s\S]*?return historicalCopyTopicArtifact\.value/u,
  )
  assert.match(editor, /historicalCopyArtifact\.value \? '最近文案' : '最近标题'/u)
  assert.doesNotMatch(editor, /`正在显示\$\{historicalTextLabel\}（历史版本）`/u)
  assert.doesNotMatch(editor, /type="warning"[\s\S]*?此历史内容仅用于回显/u)
  assert.match(editor, /`\$\{historicalTextLabel\} · 历史版本`/u)
  assert.match(editor, /<p>\{\{ currentArtifactBody\(displayedCopyArtifact\) \|\| '未填写文案正文' \}\}<\/p>/u)
  assert.doesNotMatch(fallback, /selectStageArtifacts|updateTask|patchArtifact/u)
})

test('当前文案与已确认素材只在完整流程第六步展示', () => {
  assert.match(
    editor,
    /const showFullWorkflowContext = computed\([\s\S]*?props\.workflowMode && resolvedTaskMode\.value === 'full'/u,
  )
  assert.match(
    editor,
    /v-if="showFullWorkflowContext"[\s\S]*?class="workflow-current-content"/u,
  )
  assert.match(
    editor,
    /v-if="showFullWorkflowContext"[\s\S]*?class="workflow-project-assets"/u,
  )
})

test('第六步生成视频不会隐式改写任务配置并使已确认阶段失效', () => {
  const generationStart = editor.indexOf('async function generateVideoCandidate()')
  const generationEnd = editor.indexOf('async function confirmBrandAsset', generationStart)
  assert.notEqual(generationStart, -1)
  assert.notEqual(generationEnd, -1)
  const generation = editor.slice(generationStart, generationEnd)
  assert.doesNotMatch(generation, /taskDirty\.value\s*=\s*true/u)
  assert.doesNotMatch(generation, /taskKnowledgeResourceIds\.value\s*=/u)
  assert.ok(
    generation.indexOf('await manualSave()')
    < generation.indexOf('contentApi.generateVideoProjectVideo'),
  )
  assert.match(generation, /source_copy_artifact_id:\s*null/u)
  assert.match(editor, /JSON\.stringify\(payload\) === savedTaskPayload\.value/u)
  assert.match(editor, /taskKeywords\.value = \[\.\.\.value\.keywords\]/u)
})

test('返回文案阶段可切换版本、人工修改或 AI 改写且无需人工确认', () => {
  assert.match(taskView, /nextArtifactSelection\([\s\S]*?activeStageKey\.value/u)
  assert.match(
    artifactSelectionQueue,
    /if \(stageKey === 'copy'\)[\s\S]*?selectedIds\.includes\(artifact\.id\)[\s\S]*?\[artifact\.id\]/u,
  )
  assert.match(taskView, /async function saveArtifactEdit\(\)/u)
  assert.match(taskView, /async function transformCopy\(\)/u)
  assert.match(artifactCard, /\['topic', 'copy'\]\.includes\(artifact\.kind\)/u)
  assert.match(artifactCard, /改用此候选/u)
  assert.match(artifactCard, /\? '当前文案'/u)
  assert.match(taskView, /@click="continueFromCurrentCopy"[\s\S]*?使用当前文案并继续/u)
  const continuationStart = taskView.indexOf('async function continueFromCurrentCopy()')
  const continuationEnd = taskView.indexOf('async function confirmCurrentStage()', continuationStart)
  const continuation = taskView.slice(continuationStart, continuationEnd)
  assert.match(continuation, /await flushArtifactSelection\(\)/u)
  assert.match(continuation, /if \(!await flushArtifactSelection\(\)\)[\s\S]*?return/u)
  assert.match(continuation, /navigateToNextStage\(\)/u)
  assert.doesNotMatch(continuation, /confirmStage/u)
})

test('视频编辑器修改任务配置后立即刷新当前文案映射', () => {
  const saveStart = editor.indexOf('async function saveTaskInformation()')
  const saveEnd = editor.indexOf('function isInvalidProjectAssetSaveError', saveStart)
  const save = editor.slice(saveStart, saveEnd)
  assert.match(save, /contentApi\.updateTask/u)
  assert.match(save, /contentApi\.getWorkflow\(resolvedTaskId\.value\)/u)
  assert.match(save, /contentApi\.listArtifacts\(resolvedTaskId\.value\)/u)
  assert.match(save, /taskWorkflow\.value = latestWorkflow/u)
  assert.match(save, /taskArtifacts\.value = latestArtifacts/u)
  assert.match(save, /taskDirty\.value = false[\s\S]*?try \{/u)
  assert.match(save, /任务信息已保存，当前文案将在刷新后同步/u)
  assert.ok(
    save.indexOf('taskDirty.value = false') < save.indexOf('contentApi.getWorkflow'),
  )
})

test('只有阶段当前绑定版本显示已确认并阻止删除', () => {
  assert.match(
    taskView,
    /currentStageConfirmedArtifactIds[\s\S]*?currentStage\.value\?\.confirmed_artifact_ids/u,
  )
  assert.match(
    taskView,
    /:currently-confirmed="currentStageConfirmedArtifactIds\.has\(artifact\.id\)"/u,
  )
  assert.match(artifactCard, /currentlyConfirmed: boolean/u)
  assert.match(artifactCard, /computed\(\(\) => props\.currentlyConfirmed\)/u)
  assert.doesNotMatch(artifactCard, /\?\? props\.artifact\.status/u)
  assert.match(artifactCard, /\['confirmed', 'selected'\]\.includes\(props\.artifact\.status\)[\s\S]*?return 'draft'/u)
  const removeStart = taskView.indexOf('async function removeArtifact')
  const removeEnd = taskView.indexOf('async function downloadArtifact', removeStart)
  assert.notEqual(removeStart, -1)
  assert.notEqual(removeEnd, -1)
  const removal = taskView.slice(removeStart, removeEnd)
  assert.match(removal, /currentStageConfirmedArtifactIds\.value\.has\(artifact\.id\)/u)
  assert.match(removal, /selectedArtifactIds\.value\.includes\(artifact\.id\)/u)
  assert.doesNotMatch(removal, /artifact\.status !== 'draft'/u)
})

test('完成操作先保存和最终渲染，成功后才通知工作流进入导出', () => {
  const start = editor.indexOf('async function completeWorkflowVideo()')
  const end = editor.indexOf('function openExportDrawer()', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const completion = editor.slice(start, end)
  assert.match(completion, /await manualSave\(\)/u)
  assert.match(completion, /const profile = finalRenderProfile\(\)/u)
  assert.match(completion, /const fps = previewFps\(Number\(timeline\.value\.canvas\.fps\)\)/u)
  assert.match(completion, /subtitleMode/u)
  assert.match(completion, /includeAudio \? 'audio' : 'silent'/u)
  assert.match(completion, /idempotency_key:\s*\[/u)
  assert.match(completion, /completed\.status !== 'succeeded'/u)
  assert.match(completion, /completed\.result\.artifact_id/u)
  assert.match(completion, /contentApi\.getWorkflow\(resolvedTaskId\.value\)/u)
  assert.match(completion, /const baseline = workflowStageBaseline\(videoStage\)/u)
  assert.match(completion, /emit\('workflowCompleted', \{ artifactId, baseline \}\)/u)
  assert.ok(
    completion.indexOf('completed.status !== \'succeeded\'')
    < completion.indexOf('emit(\'workflowCompleted\', { artifactId, baseline })'),
  )
})

test('第七阶段用紧凑操作区提供逐项导出与完整包并仅统计可见阶段', () => {
  assert.match(taskView, /一键下载当前成品包（ZIP）/u)
  assert.match(taskView, /class="full-export-controls"/u)
  assert.match(taskView, /id="full-export-artifact"/u)
  assert.match(taskView, /v-for="artifact in fullExportArtifacts"/u)
  assert.match(taskView, /:label="fullExportArtifactLabel\(artifact\)"/u)
  assert.match(taskView, /id="full-export-format"/u)
  assert.match(taskView, /v-for="option in selectedFullExportFormatOptions"/u)
  assert.match(taskView, /@click="downloadFullArtifact"/u)
  assert.doesNotMatch(taskView, /full-single-export/u)
  assert.match(taskView, /确认并开放导出/u)
  assert.match(taskView, /:disabled="!exportStageConfirmed"/u)
  assert.match(taskView, /fullSingleExportFormatOptions\(artifact\)/u)
  assert.match(taskView, /Word（\.docx）/u)
  assert.match(taskView, /Markdown（\.md）/u)
  assert.match(taskView, /WebP（\.webp）/u)
  assert.match(taskView, /artifact\.kind === 'video' \? 'MP4 视频（\.mp4）'/u)
  assert.match(taskView, /downloadArtifact\(artifact, fullSingleExportFormat\(artifact\)\)/u)
  assert.match(
    taskView,
    /@media \(max-width: 900px\)[\s\S]*?\.full-export-controls \{[\s\S]*?grid-template-columns: minmax\(0, 1fr\)/u,
  )
  assert.match(
    taskView,
    /item\.state === 'confirmed' && visibleStageKeys\.includes\(item\.key\)/u,
  )
  assert.match(taskView, /const serverExportArtifactIds = computed\(\(\) => workflow\.value\?\.export_artifact_ids\)/u)
  assert.match(
    taskView,
    /serverExportArtifactIds\.value === undefined[\s\S]*?confirmedArtifactIds\.value[\s\S]*?: serverExportArtifactIds\.value/u,
  )
  const fullExportStart = taskView.indexOf('const fullExportArtifacts = computed')
  const fullExportEnd = taskView.indexOf('const exportFormatOptions', fullExportStart)
  const fullExport = taskView.slice(fullExportStart, fullExportEnd)
  assert.match(fullExport, /exportArtifactIds\.value\.has\(item\.id\)/u)
  assert.match(
    fullExport,
    /serverExportArtifactIds\.value !== undefined \|\| item\.status === 'confirmed'/u,
  )
  assert.match(taskView, /stageHasExportArtifact\(key\) \? 'archived' : 'archived_unincluded'/u)
  assert.match(taskView, /return '未纳入导出'/u)
})

test('快捷视频完成后进入确认导出且导出页不再被编辑器吞掉', () => {
  assert.match(taskView, /currentMode\.value === 'video' && activeStageKey\.value !== 'export'/u)
  assert.match(taskView, /!\['full', 'video'\]\.includes\(currentMode\.value\)/u)
  assert.match(taskView, /下载当前成品包（ZIP）/u)
  assert.match(editor, /完成并进入确认导出/u)
})

test('最终成片确认使用渲染前基线且不会覆盖渲染期间的其他选择', () => {
  const start = taskView.indexOf('async function handleWorkflowVideoCompleted')
  const end = taskView.indexOf('async function runCompliance', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const confirmation = taskView.slice(start, end)
  assert.match(confirmation, /workflowFinalizationDecision\(stage, payload\.baseline, payload\.artifactId\)/u)
  assert.match(confirmation, /decision === 'select_and_confirm'/u)
  assert.match(confirmation, /finalConfirmationRetryDecision\(latestStage, payload\.artifactId\)/u)
  assert.match(confirmation, /视频渲染期间阶段选择或确认结果已发生变化/u)
  assert.match(confirmation, /系统未覆盖现有选择/u)
  const confirmCallStart = confirmation.indexOf('await contentApi.confirmStage')
  const confirmCallEnd = confirmation.indexOf('await refreshWorkflowAndArtifacts()', confirmCallStart)
  assert.notEqual(confirmCallStart, -1)
  assert.notEqual(confirmCallEnd, -1)
  const confirmCall = confirmation.slice(confirmCallStart, confirmCallEnd)
  assert.match(confirmCall, /expected_version: expectedVersion/u)
  assert.doesNotMatch(confirmCall, /artifact_ids/u)
})

if (failures.length) {
  process.exitCode = 1
}
else {
  console.info('Full content video flow contract passed: 13 checks passed.')
}
