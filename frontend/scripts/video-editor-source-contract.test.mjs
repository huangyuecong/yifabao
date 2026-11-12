import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const componentUrl = new URL(
  '../src/views/agent/content/video-editor/VideoEditorWorkspace.vue',
  import.meta.url,
)
const componentPath = fileURLToPath(componentUrl)
const source = readFileSync(componentPath, 'utf8')
const apiSource = readFileSync(new URL('../src/api/agent/content.ts', import.meta.url), 'utf8')
const assetMediaSource = readFileSync(
  new URL('../src/views/agent/content/video-editor/assetMedia.ts', import.meta.url),
  'utf8',
)
const timelineClipSource = readFileSync(
  new URL('../src/views/agent/content/video-editor/TimelineClip.vue', import.meta.url),
  'utf8',
)
const timelineClipInteractionSource = readFileSync(
  new URL('../src/views/agent/content/video-editor/timelineClipInteraction.ts', import.meta.url),
  'utf8',
)
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

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function openingTags(name) {
  return [...source.matchAll(new RegExp(`<${name}\\b[\\s\\S]*?>`, 'gu'))].map(match => match[0])
}

function hasModelBinding(tag, model) {
  const escapedModel = escapeRegExp(model)
  return new RegExp(`(?:v-model|:model-value)\\s*=\\s*["']${escapedModel}["']`, 'u').test(tag)
}

function functionSource(name, nextName) {
  const start = source.indexOf(`async function ${name}`)
  const end = source.indexOf(`async function ${nextName}`, start + 1)
  assert.notEqual(start, -1, `找不到 ${name} 函数`)
  assert.notEqual(end, -1, `找不到 ${name} 后的 ${nextName} 函数`)
  return source.slice(start, end)
}

function sourceSlice(startToken, endToken) {
  const start = source.indexOf(startToken)
  const end = source.indexOf(endToken, start + startToken.length)
  assert.notEqual(start, -1, `找不到 ${startToken}`)
  assert.notEqual(end, -1, `找不到 ${startToken} 后的 ${endToken}`)
  return source.slice(start, end)
}

function cssRule(selector) {
  const escapedSelector = escapeRegExp(selector)
  const match = source.match(
    new RegExp(`(?:^|\\n)${escapedSelector}\\s*\\{([\\s\\S]*?)\\n\\}`, 'u'),
  )
  assert.ok(match, `找不到 ${selector} 样式规则`)
  return match[1]
}

test('AI 视频面板人工选择参考图片与视频并按模型能力分别限额', () => {
  assert.match(source, /AI\s*视频生成/u)
  assert.match(source, /capabilityReferenceLimit\('reference_images'\)/u)
  assert.match(source, /capabilityReferenceLimit\('reference_videos'\)/u)
  assert.match(source, /aiVideoCapabilityParameters\.value\.total_duration_seconds/u)
  assert.match(source, /project_asset_transport_available === true/u)
  assert.doesNotMatch(source, /当前模型暂不能读取普通工程参考视频/u)
  assert.match(
    source,
    /参考图片\s+\{\{\s*aiVideoReferenceImageCount\s*\}\}\/\{\{\s*aiVideoReferenceImageLimit \?\? '未声明'\s*\}\}[\s\S]*?产品自动\s+\{\{\s*aiVideoProductVisualReferences\.length\s*\}\}[\s\S]*?手动\s+\{\{\s*aiVideoForm\.referenceImageAssetIds\.length\s*\}\}[\s\S]*?剩余\s+\{\{\s*aiVideoReferenceImageRemaining \?\? '未声明'\s*\}\}[\s\S]*?参考视频\s+\{\{\s*aiVideoForm\.referenceVideoAssetIds\.length\s*\}\}\/\{\{\s*aiVideoReferenceVideoLimit \?\? '未声明'\s*\}\}/u,
  )
  assert.match(
    source,
    /参考视频总时长\s+\{\{\s*Number\(aiVideoReferenceVideoDuration\.toFixed\(2\)\)\s*\}\}\/\{\{\s*aiVideoReferenceVideoDurationLimit \?\? '未声明'\s*\}\}\s+秒/u,
  )
  assert.match(
    source,
    /@update:model-value="updateAiVideoImageReferences\(\$event as string\[\]\)"/u,
  )
  assert.match(
    source,
    /@update:model-value="updateAiVideoVideoReferences\(\$event as string\[\]\)"/u,
  )
  assert.match(
    source,
    /if \(limit != null && nextCount > limit && nextCount >= currentCount && !isRemoval\)[\s\S]*?已阻止本次添加[\s\S]*?return/u,
  )
  assert.match(
    source,
    /if \(countLimit != null && nextIds\.length > countLimit && !isRemoval\)[\s\S]*?已阻止本次添加[\s\S]*?return/u,
  )
  assert.match(
    source,
    /if \(durationLimit != null && totalDuration > durationLimit && !isRemoval\)[\s\S]*?已阻止本次添加[\s\S]*?return/u,
  )
  assert.match(source, /:disabled="aiVideoReferenceImageLimit == null/u)
  assert.match(source, /const aiVideoReferenceVideoDisabled = computed/u)
  assert.match(source, /:disabled="aiVideoReferenceVideoDisabled"/u)
  assert.match(source, /!aiVideoProjectAssetTransportAvailable/u)
  assert.match(source, /class="ai-video-reference-feedback"/u)
  assert.match(source, /AI_VIDEO_TRANSPORT_RECHECK_MS = 15_000/u)
  assert.match(source, /loadAiVideoCapabilities\(\{ background: true \}\)/u)
  assert.match(source, /scheduleAiVideoTransportRecheck\(\)/u)
  assert.match(source, /\.finally\(scheduleAiVideoTransportRecheck\)/u)
  assert.match(source, /clearAiVideoTransportRecheck\(\)/u)
  assert.match(source, /Transport capability is reflected by the compact status tag below/u)
  assert.match(source, /工程参考视频通道可用/u)
  assert.match(source, /@click="recheckAiVideoCapabilities"/u)
  assert.match(source, /productVisualReferenceResources\(/u)
  assert.match(source, /const aiVideoReferenceImageCount = computed/u)
  assert.match(source, /function aiVideoReferenceImageCanonicalIds/u)
  assert.match(source, /projectAssetVisualReferenceKey\(asset\)/u)
  assert.match(source, /libraryResourceCanonicalId\(item\)/u)
  assert.doesNotMatch(source, /任务产品资料关联的产品图会由服务端自动作为参考图片/u)
  assert.match(source, /class="ai-video-product-reference-list"/u)
  assert.match(source, /<ContentLibraryMedia :item="item"/u)
  assert.match(source, /产品自动引用与手动选择合计/u)
  assert.doesNotMatch(source, /referenceImageAssetIds\s*=\s*nextIds\.slice/u)
  assert.doesNotMatch(source, /referenceVideoAssetIds\s*=\s*nextIds\.slice/u)

  const generation = functionSource('generateVideoCandidate()', 'confirmBrandAsset(')
  assert.match(generation, /validateAiVideoReferences\(\)/u)
  assert.match(
    generation,
    /reference_image_asset_ids:\s*\[\.\.\.aiVideoForm\.referenceImageAssetIds\]/u,
  )
  assert.match(
    generation,
    /reference_video_asset_ids:\s*\[\.\.\.aiVideoForm\.referenceVideoAssetIds\]/u,
  )
  assert.doesNotMatch(generation, /source_project_asset_id/u)
  assert.match(apiSource, /reference_image_asset_ids\?: string\[\]/u)
  assert.match(apiSource, /reference_video_asset_ids\?: string\[\]/u)
})

test('工程媒体通道不可用时仅放行同模型 AI 原生视频引用', () => {
  assert.match(source, /provider_native_reference_supported === true/u)
  assert.match(assetMediaSource, /asset\.metadata\.provider_native_reference/u)
  assert.match(assetMediaSource, /reference\.eligible === true/u)
  assert.match(assetMediaSource, /providerId === String\(currentProviderId \|\| ''\)\.trim\(\)/u)
  assert.match(assetMediaSource, /asset\.source_type !== 'artifact'/u)
  assert.match(assetMediaSource, /asset\.metadata\.source !== 'ai-generated'/u)
  assert.match(source, /projectAssetDurationSeconds/u)
  assert.match(
    assetMediaSource,
    /\[valueMetadata\.duration_seconds, probeDuration, provenanceDuration\]/u,
  )
  assert.match(source, /:disabled="!aiVideoReferenceVideoAssetSelectable\(asset\)"/u)
  assert.doesNotMatch(source, /本地上传及知识库视频需 HTTPS 媒体通道、暂不可选/u)
  assert.match(source, /仅同模型 AI 生成视频可直接引用/u)
  assert.match(
    apiSource,
    /provider_native_reference\?: VideoProjectProviderNativeReference \| null/u,
  )
  const generation = functionSource('generateVideoCandidate()', 'confirmBrandAsset(')
  assert.match(
    generation,
    /reference_video_asset_ids:\s*\[\.\.\.aiVideoForm\.referenceVideoAssetIds\]/u,
  )
})

test('时长、比例和清晰度均由 AI 能力声明驱动并使用下拉选择', () => {
  for (const key of ['durations', 'ratios', 'resolutions']) {
    assert.match(
      source,
      new RegExp(`aiVideoCapabilityParameters\\.value\\.${key}\\b`, 'u'),
      `AI 视频参数应读取 capability.parameters.${key}`,
    )
  }

  const selectTags = openingTags('ElSelect')
  for (const model of [
    'aiVideoForm.durationSeconds',
    'aiVideoForm.ratio',
    'aiVideoForm.resolution',
  ]) {
    assert.ok(
      selectTags.some(tag => hasModelBinding(tag, model)),
      `${model} 应绑定到 ElSelect`,
    )
  }

  const freeFormTags = [...openingTags('ElInput'), ...openingTags('ElInputNumber')]
  for (const model of [
    'aiVideoForm.durationSeconds',
    'aiVideoForm.ratio',
    'aiVideoForm.resolution',
  ]) {
    assert.equal(
      freeFormTags.some(tag => hasModelBinding(tag, model)),
      false,
      `${model} 不应允许自由输入超出模型能力`,
    )
  }
})

test('AI 视频区域不显示任务级外发授权提示', () => {
  assert.doesNotMatch(source, /外发授权|任务级授权|确认发送信息并生成视频/u)
  assert.doesNotMatch(source, /aiVideoEgress/u)
})

test('AI 视频生成通过现有接口直接提交', () => {
  const generation = functionSource('generateVideoCandidate()', 'confirmBrandAsset(')

  assert.match(generation, /contentApi\.generateVideoProjectVideo\(project\.value\.id/u)
  assert.doesNotMatch(generation, /getEgressStatus|approveEgress|ensureAiVideoEgress/u)
})

test('快捷视频配音按服务能力提供音色下拉与语速范围', () => {
  assert.match(source, /item\.capability === 'speech-synthesis'/u)
  assert.match(source, /capabilityParameterOptions\(speechCapability\.value, 'voices'\)/u)
  assert.match(source, /parameterOptionLabel\(key, value as string \| number\)/u)
  assert.match(source, /capabilityParameterRange\(speechCapability\.value, 'speed'\)/u)
  assert.match(source, /contentApi\.serviceStatus\(\)/u)
  assert.match(source, /speechService\.value\?\.status !== 'disabled'/u)
  assert.match(source, /subtitleService\.value\?\.status !== 'disabled'/u)
  assert.doesNotMatch(source, /\['unavailable', 'disabled'\]/u)
  assert.doesNotMatch(source, /title="部分 AI 服务上次调用失败，仍可重试"/u)
  assert.match(source, /async function ensureEditorAiService/u)
  assert.match(source, /await ensureEditorAiService\('voice'\)/u)
  assert.match(source, /await ensureEditorAiService\('subtitle'\)/u)
  assert.match(source, /await ensureEditorAiService\('video'\)/u)
  assert.match(source, /aiServiceAttemptChecking\.value = false/u)
  assert.match(source, /notifiedAiVideoFailureId\.value !== job\.id/u)

  const selectTags = openingTags('ElSelect')
  assert.ok(
    selectTags.some(tag => hasModelBinding(tag, 'voiceForm.voice')),
    '配音音色应绑定到 ElSelect',
  )
  const inputTags = openingTags('ElInput')
  assert.equal(
    inputTags.some(tag => hasModelBinding(tag, 'voiceForm.voice')),
    false,
    '配音音色不应继续使用自由输入框',
  )
  assert.match(source, /v-for="option in speechVoiceOptions"/u)
  assert.match(source, /:disabled="speechVoiceSelectDisabled"/u)
  assert.match(source, /return '当前配音真实能力未启用'/u)

  const numberTags = openingTags('ElInputNumber')
  const speedInput = numberTags.find(tag => hasModelBinding(tag, 'voiceForm.speed'))
  assert.ok(speedInput, '找不到配音语速输入框')
  assert.match(speedInput, /:min="speechSpeedRange\?\.min \?\? 0\.1"/u)
  assert.match(speedInput, /:max="speechSpeedRange\?\.max \?\? 4"/u)
})

test('快捷视频支持配音同步字幕和独立生成字幕并逐条加入字幕轨', () => {
  assert.match(apiSource, /export interface VideoProjectSubtitleGenerationRequest/u)
  assert.match(apiSource, /export interface VideoProjectSubtitleGenerationResponse/u)
  assert.match(apiSource, /generateVideoProjectSubtitles:/u)
  assert.match(apiSource, /\/generate-subtitles/u)
  assert.match(source, /item\.capability === 'subtitle-alignment'/u)
  assert.match(source, /generateSubtitles: true/u)
  assert.match(source, /v-model="voiceForm\.generateSubtitles"/u)
  assert.match(source, /<ElCollapseItem name="subtitle">/u)
  assert.match(source, /@click="generateSubtitleCandidate"/u)
  assert.match(source, /生成字幕并加入轨道/u)
  assert.match(source, /contentApi\.generateVideoProjectSubtitles\(project\.value\.id/u)
  assert.match(source, /subtitleCuesFromResponse\(generated\.segments\)/u)
  assert.match(source, /track\.items\.push\(item\)/u)
  assert.match(source, /currentTrack\.locked/u)
  assert.match(source, /project_asset_id: null/u)
  assert.match(source, /source_project_asset_id: sourceAsset\.id/u)
  assert.match(source, /metadata\.source_project_asset_id === asset\.id/u)
  assert.match(source, /projectAssetReferenceCount\(sourceAsset\)/u)
  assert.match(source, /v-model="subtitleForm\.sourceText"/u)
  assert.match(source, /subtitleTimelineOffset\(sourceProjectAssetId\)/u)
  assert.match(source, /addToTimeline: false/u)
  assert.match(source, /加入配音时会同步加入字幕轨/u)
  assert.match(source, /if \(!\(await manualSave\(\)\)\)/u)
})

test('编辑器工作区和时间线保留可操作的最小高度', () => {
  const editorRule = cssRule('.video-editor')
  const editorPanelsRule = cssRule('.editor-panels')
  const timelineRule = cssRule('.timeline-panel')

  assert.match(editorRule, /min-height:\s*960px\s*;/u)
  assert.match(
    editorRule,
    /grid-template-rows:\s*minmax\(58px,\s*auto\)\s+auto\s+minmax\(480px,\s*1fr\)\s+clamp\(330px,\s*36vh,\s*380px\)\s*;/u,
  )
  assert.match(editorPanelsRule, /min-height:\s*480px\s*;/u)
  assert.match(timelineRule, /min-height:\s*330px\s*;/u)
})

test('AI 背景音乐示例只作为空值占位符', () => {
  assert.match(source, /const musicPrompt = ref\(''\)/u)
  const musicPromptInput = openingTags('ElInput').find(tag => hasModelBinding(tag, 'musicPrompt'))
  assert.ok(musicPromptInput, '找不到 AI 背景音乐提示词输入框')
  assert.match(
    musicPromptInput,
    /placeholder="示例：轻盈克制的东方氛围背景音乐，适合品牌宣传短片，无人声"/u,
  )
  assert.match(source, /const imageForm = reactive\(\{[\s\S]*?size: '',\s*ratio: ''/u)
  assert.match(source, /:placeholder="imageCandidateSettings\.sizeDefaultLabel"/u)
})

test('移除 AI 修改候选与独立音效轨并统一为背景音效', () => {
  assert.doesNotMatch(source, /AI 修改候选|生成前后对比候选/u)
  assert.doesNotMatch(source, /<ElOption label="音效" value="sfx"/u)
  assert.match(source, /<strong>AI 生成背景音乐（无人声）<\/strong>/u)
  assert.match(source, /生成背景音乐候选/u)
  assert.match(source, /环境音、碰杯等音效可导入音频后移到背景音效轨/u)
  assert.match(source, /track => track\.kind !== 'sfx'/u)
  assert.match(source, /if \(kind === 'music'\) \{\s*return '背景音效'/u)
})

test('快捷视频任务名称只作为空值占位符', () => {
  assert.match(source, /const DEFAULT_VIDEO_TASK_NAME = '新建快捷视频'/u)
  assert.match(source, /const taskForm = reactive\(\{\s*name: '',/u)
  assert.match(
    source,
    /name: value\.name === DEFAULT_VIDEO_TASK_NAME \? '' : value\.name \|\| '',/u,
  )
  const taskNameInput = openingTags('ElInput').find(tag => hasModelBinding(tag, 'taskForm.name'))
  assert.ok(taskNameInput, '找不到快捷视频任务名称输入框')
  assert.match(taskNameInput, /:placeholder="DEFAULT_VIDEO_TASK_NAME"/u)
})

test('空白进入快捷视频不创建最近任务', () => {
  const initialization = sourceSlice('async function initialize()', 'function markTaskDirty()')
  assert.match(initialization, /if \(!resolvedTaskId\.value\)/u)
  assert.match(initialization, /尚未创建，首次编辑或导入素材后自动保存/u)
  assert.doesNotMatch(initialization, /ensureTaskAndProject\(\)/u)
})

test('快捷视频编辑器提供返回工作台入口', () => {
  assert.match(source, /editorHome: \[\]/u)
  assert.match(
    source,
    // Always offered now, in every editor mode.
    /<ElButton\s+@click="requestEditorHome"\s*>\s*返回工作台/u,
  )
  assert.match(source, /let leavingToHome = false/u)
  assert.match(source, /if \(!leavingToHome\)\s*\{\s*emit\('taskCreated', createdId\)/u)
})

test('本地素材按工程素材持久化管理', () => {
  assert.match(source, /const localProjectAssets = computed/u)
  assert.match(source, /asset\.source_type === 'asset_ref'/u)
  assert.match(source, /view\.projectAssetId = asset\.id/u)
  assert.match(source, /data-testid="local-project-asset-card"/u)
  assert.match(source, /@click="addProjectAssetToTimeline\(asset\)"/u)
  assert.match(source, /@click="downloadProjectAsset\(asset\)"/u)
  assert.match(source, /@click="renameProjectAsset\(asset\)"/u)
  assert.match(source, /@click="deleteLocalProjectAsset\(asset\)"/u)
  assert.match(source, /imported\.projectAssetId !== asset\.id/u)
})

test('长媒体采用探测时长而不是固定五秒', () => {
  const projectAssetAdd = sourceSlice(
    'function addProjectAssetToTimeline(',
    'async function importAllWorkflowProjectAssets()',
  )
  const enterpriseAdd = sourceSlice(
    'function addEnterpriseLibraryItem(',
    'function importEnterpriseSelection(',
  )
  assert.match(projectAssetAdd, /const metadataDuration = projectAssetDurationSeconds\(asset\)/u)
  assert.match(
    enterpriseAdd,
    /const metadataDuration = mediaMetadataDurationSeconds\(item\.metadata\)/u,
  )
  assert.match(
    assetMediaSource,
    /\[valueMetadata\.duration_seconds, probeDuration, provenanceDuration\]/u,
  )
})

test('字幕与音频片段按时间移动、自由改时长且配音语速可调', () => {
  assert.match(timelineClipSource, /props\.trackKind === 'video'/u)
  assert.match(timelineClipSource, /emit\('position', item\.id, startSeconds\)/u)
  assert.match(timelineClipSource, /emit\('resize', item\.id, patch\)/u)
  assert.match(timelineClipSource, /resizeTimelineClip\(/u)
  assert.match(timelineClipInteractionSource, /\['voice', 'music', 'sfx'\]\.includes\(kind\)/u)
  assert.match(source, /@position="positionTimelineItem"/u)
  assert.match(source, /@resize="resizeTimelineItem"/u)
  assert.match(source, /v-if="selectedTrack\?\.kind === 'voice'" label="配音语速"/u)
  assert.match(source, /@update:model-value="updateSelectedVoiceSpeed"/u)
})

test('候选加入状态覆盖配音和字幕，工具栏只在保存中或失败时反馈', () => {
  assert.match(source, /projectAssetReferenceCount\(asset\) \? '已加入' : '加入'/u)
  assert.match(source, /const showToolbarSaveFeedback = computed/u)
  assert.match(source, /\['loading', 'saving', 'error'\]\.includes\(saveState\.value\)/u)
  assert.doesNotMatch(source, />\s*立即保存\s*</u)
  assert.doesNotMatch(source, />\s*版本 \{\{ projectRevision \}\}\s*</u)
})

test('路由离开前等待刷新最后一次防抖自动保存', () => {
  const flushSource = sourceSlice('async function flushPendingAutosave()', 'onBeforeUnmount(() =>')
  assert.match(
    flushSource,
    /saveTimer \|\| pendingOperations\.value\.length \|\| taskDirty\.value/u,
  )
  assert.match(flushSource, /return manualSave\(\)/u)
  assert.match(
    source,
    /onBeforeRouteLeave\(async \(\) => \{[\s\S]*?await flushPendingAutosave\(\)[\s\S]*?return true/u,
  )
})

test('已归档素材不会被撤销恢复或触发循环自动保存', () => {
  const undoSource = sourceSlice('function undo()', 'function redo()')
  const redoSource = sourceSlice('function redo()', 'function trackId(')
  const saveSource = sourceSlice('async function saveNow()', 'async function manualSave()')
  const manualSaveSource = sourceSlice(
    'async function manualSave()',
    'function videoPositionOperations(',
  )
  const sanitizerSource = sourceSlice(
    'function sanitizeTimelineForActiveProjectAssets(',
    'function purgeProjectAssetFromHistory(',
  )
  const timelineRemovalSource = sourceSlice(
    'function projectAssetTimelineRemoval(',
    'async function removeProjectAsset(',
  )
  const removeSource = sourceSlice(
    'async function removeProjectAsset(',
    'async function deleteLocalProjectAsset(',
  )
  assert.match(undoSource, /sanitizeTimelineForActiveProjectAssets\(previous\)/u)
  assert.match(redoSource, /sanitizeTimelineForActiveProjectAssets\(next\)/u)
  assert.match(sanitizerSource, /project\.value\.assets\.filter\(asset => asset\.active\)/u)
  assert.match(sanitizerSource, /filter\(assetId => !activeIds\.has\(assetId\)\)/u)
  assert.match(timelineRemovalSource, /track\.locked = false/u)
  assert.match(timelineRemovalSource, /track\.locked = true/u)
  assert.match(timelineRemovalSource, /timelineTransitionOperations\(before, unlocked\)/u)
  assert.match(removeSource, /if \(!\(await manualSave\(\)\)\)/u)
  assert.match(removeSource, /purgeProjectAssetFromHistory\(asset\.id\)/u)
  assert.match(saveSource, /isInvalidProjectAssetSaveError\(reason\)/u)
  assert.match(saveSource, /recoverFromInvalidProjectAssetSave\(\)/u)
  assert.match(saveSource, /reportSaveErrorOnce\(saveMessage\.value\)/u)
  assert.match(manualSaveSource, /if \(saveInFlight\.value\)/u)
  assert.match(manualSaveSource, /saveCompletionWaiters\.push\(resolve\)/u)
  assert.match(source, /timelineTransitionOperations\(serverTimeline, timeline\.value\)/u)
  assert.match(source, /grouping: true/u)
})

test('工程工具栏和三栏内容不撑破页面', () => {
  const editorRule = cssRule('.video-editor')
  const toolbarRule = cssRule('.editor-toolbar')
  const editorPanelsRule = cssRule('.editor-panels')
  assert.match(editorRule, /max-width:\s*100%\s*;/u)
  assert.match(editorRule, /min-width:\s*0\s*;/u)
  assert.match(toolbarRule, /min-width:\s*0\s*;/u)
  assert.match(editorPanelsRule, /max-width:\s*100%\s*;/u)
  assert.match(editorPanelsRule, /overflow:\s*hidden\s*;/u)
})

test('撤销和重做禁用状态遵循统一的40%透明度规范', () => {
  const theme = readFileSync(new URL('../src/styles/brand-theme.scss', import.meta.url), 'utf8')
  const legacy = readFileSync(new URL('../src/styles/element-plus.scss', import.meta.url), 'utf8')
  assert.match(theme, /\.el-button\.is-disabled\s*\{\s*opacity:\s*0\.4;/u)
  assert.match(theme, /--el-button-disabled-text-color: var\(--xi-semantic-color-text-secondary\)/u)
  assert.match(theme, /--el-button-disabled-bg-color: var\(--xi-semantic-color-background-container\)/u)
  assert.doesNotMatch(source, /\.toolbar-history :deep\(\.el-button\.is-disabled/u)
  assert.doesNotMatch(legacy, /\.el-button[^{]*is-disabled[^{]*\{[^}]*!important/u)
})

if (failures.length) {
  console.error(`Video editor source contract failed: ${failures.length} check(s) failed.`)
  process.exitCode = 1
}
else {
  console.info('Video editor source contract passed: 19 checks passed.')
}
