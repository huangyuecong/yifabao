import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import process from 'node:process'
import {
  artifactText,
  candidateText,
  createIdempotencyKey,
  hasMaterialTransformChange,
} from '../src/views/agent/content/contentWorkflow.ts'

const form = readFileSync(
  new URL('../src/views/agent/content/TaskConfigurationForm.vue', import.meta.url),
  'utf8',
)
const taskView = readFileSync(
  new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url),
  'utf8',
)
const servicesView = readFileSync(
  new URL('../src/views/agent/content/ContentServicesView.vue', import.meta.url),
  'utf8',
)
const workbench = readFileSync(
  new URL('../src/views/agent/ContentWorkbench.vue', import.meta.url),
  'utf8',
)
const picker = readFileSync(
  new URL('../src/views/agent/content/ContentKnowledgePicker.vue', import.meta.url),
  'utf8',
)
const catalog = readFileSync(
  new URL('../src/views/agent/content/contentResourceCatalog.ts', import.meta.url),
  'utf8',
)
const libraryCatalog = readFileSync(
  new URL('../src/views/agent/content/ContentLibraryCatalog.vue', import.meta.url),
  'utf8',
)
const productPicker = readFileSync(
  new URL('../src/views/agent/content/ContentProductPicker.vue', import.meta.url),
  'utf8',
)
const selectionList = readFileSync(
  new URL('../src/views/agent/content/ContentKnowledgeSelectionList.vue', import.meta.url),
  'utf8',
)
const resourcePicker = readFileSync(
  new URL('../src/views/agent/content/ContentResourcePicker.vue', import.meta.url),
  'utf8',
)
const workflow = readFileSync(
  new URL('../src/views/agent/content/contentWorkflow.ts', import.meta.url),
  'utf8',
)
const history = readFileSync(
  new URL('../src/views/agent/content/ContentVersionsView.vue', import.meta.url),
  'utf8',
)
const home = readFileSync(
  new URL('../src/views/agent/content/ContentHomeView.vue', import.meta.url),
  'utf8',
)
const artifactCard = readFileSync(
  new URL('../src/views/agent/content/ContentArtifactCard.vue', import.meta.url),
  'utf8',
)
const artifactEditor = readFileSync(
  new URL('../src/views/agent/content/ContentArtifactEditor.vue', import.meta.url),
  'utf8',
)
const imageControls = readFileSync(
  new URL('../src/views/agent/content/ImageGenerationControls.vue', import.meta.url),
  'utf8',
)
const imageReferenceUploader = readFileSync(
  new URL('../src/views/agent/content/ImageReferenceUploader.vue', import.meta.url),
  'utf8',
)
const imageReferenceLimits = readFileSync(
  new URL('../src/views/agent/content/imageReferenceLimits.ts', import.meta.url),
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

test('内容长度使用短中长下拉选择', () => {
  const lengthField
    = form.match(/<ElFormItem v-if="showLength" label="内容长度">([\s\S]*?)<\/ElFormItem>/u)?.[1]
      ?? ''
  assert.match(lengthField, /<ElSelect/u)
  assert.match(form, /\{ label: '短', value: '短' \}/u)
  assert.match(form, /\{ label: '中', value: '中' \}/u)
  assert.match(form, /\{ label: '长', value: '长' \}/u)
  assert.doesNotMatch(form, /ElRadio(?:Group|Button)/u)
})

test('图片生成配置隐藏内容长度', () => {
  assert.match(form, /showLength\?: boolean/u)
  const bindings = taskView.match(/:show-length="currentMode !== 'image'"/gu) ?? []
  assert.equal(bindings.length, 2)
})

test('AI 服务入口始终可发起且失败后保留重试入口', () => {
  assert.match(taskView, /service\?\.status === 'disabled'/u)
  assert.match(taskView, /capability\.mode === 'mock'/u)
  assert.match(taskView, /async function ensureGenerationAvailable/u)
  assert.match(taskView, /contentApi\.capabilities\(\)/u)
  assert.match(taskView, /contentApi\.serviceStatus\(\)/u)
  assert.match(taskView, /ElMessage\.error\(unavailableMessage\(operation\)\)/u)
  assert.doesNotMatch(taskView, /service\?\.status === 'unavailable'/u)
  assert.doesNotMatch(taskView, /:disabled="generationUnavailable"/u)
  assert.doesNotMatch(servicesView, /调用异常时页面会提示|之后可再次尝试|serviceMessage/u)
  assert.match(servicesView, /查看各项创作功能与支持的参数/u)
  assert.doesNotMatch(servicesView, /service\.message/u)
  assert.match(workflow, /available: '可使用'/u)
  assert.match(workflow, /degraded: '部分服务受限'/u)
  assert.match(workflow, /unavailable: '暂不可用'/u)
  assert.match(workflow, /disabled: '未启用'/u)
})

test('工作台如实展示服务状态且不把首次状态当作不可用', () => {
  assert.match(workbench, /serviceStatusPresentation\(overallStatus\.value\)/u)
  assert.match(workbench, /overallStatus = ref[^\n]+\('available'\)/u)
  assert.match(workbench, /overallStatus\.value = undefined/u)
  assert.match(workbench, /statusPresentation\.value\.type/u)
  assert.match(workbench, /<ElButton[^>]*v-if="statusError"[^>]*@click="loadStatus"/u)
  assert.match(home, /serviceStatus\.value\?\.services\.length \?\? 0/u)
  assert.doesNotMatch(home, /filter\(item => item\.status === 'available'\)/u)
  assert.match(home, /可直接开始创作；调用异常时页面会提示，可再次尝试/u)
  assert.doesNotMatch(servicesView, /待验证|尚待完成实际调用验证/u)
})

test('知识库入口位于第三列且已选数量显示在可多选标题中', () => {
  assert.match(form, /<ElFormItem class="knowledge-field">/u)
  assert.match(form, /\.knowledge-field\s*\{\s*grid-column: 3;/u)
  assert.match(
    form,
    /可多选，已选 \{\{ selectedKnowledgeCount \}\} \/ \{\{ knowledgeSelectionLimit \}\} 项/u,
  )
  assert.doesNotMatch(picker, /不选择时由服务自动检索相关资料/u)
  assert.doesNotMatch(picker, /knowledge-picker-action[\s\S]*?已选 \{\{ selectedCount/u)
})

test('已选知识库资料脱离第三列并按整行三列排列', () => {
  assert.match(form, /:show-selection-summary="false"/u)
  assert.match(form, /class="selected-knowledge-row"/u)
  assert.match(form, /\.selected-knowledge-row\s*\{\s*grid-column: 1 \/ -1;/u)
  assert.match(selectionList, /grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/u)
  assert.match(selectionList, /v-for="item in selectedResources"/u)
  assert.doesNotMatch(selectionList, /selectedResources\.slice/u)
})

test('知识库筛选合并为单个悬停级联选择器', () => {
  assert.match(picker, /<ElCascader/u)
  assert.match(picker, /checkStrictly: true/u)
  assert.match(picker, /expandTrigger: 'hover'/u)
  assert.match(picker, /emitPath: false/u)
  assert.doesNotMatch(picker, /v-model="resourceKind"/u)
  assert.doesNotMatch(picker, /v-model="category"/u)
})

test('级联一级四大类可直接选择并支持产品系列层级', () => {
  for (const label of ['产品资料', '企业文档', '图片素材', '视频素材']) {
    assert.match(picker, new RegExp(`label: '${label}'`, 'u'))
  }
  assert.match(picker, /value: groupFilterValue\(group\)/u)
  assert.doesNotMatch(picker, /label: `全部\$\{group\.label\}/u)
  assert.match(picker, /productGroupLabel\(item, catalogResources\.value\)/u)
  assert.match(catalog, /fact\.startsWith\('产品系列：'\)/u)
  assert.match(catalog, /tag\.endsWith\('系列'\)/u)
})

test('知识库资料使用三列独立卡片并合并重复类型信息', () => {
  assert.match(picker, /grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/u)
  assert.match(picker, /grid-template-rows: 140px 32px auto/u)
  assert.match(picker, /border: var\(--xi-component-content-border-strong\)/u)
  assert.match(
    picker,
    /<ElTag size="small" effect="plain">\{\{ categoryLabel\(item\.category, item\) \}\}<\/ElTag>/u,
  )
  assert.doesNotMatch(
    picker,
    /<ElTag size="small" effect="plain">\{\{ resourceKindLabel\(item\.resource_kind\) \}\}<\/ElTag>/u,
  )
})

test('知识库卡片标题使用固定单行省略避免文字重叠', () => {
  assert.match(
    picker,
    /\.knowledge-card-heading\s*\{[\s\S]*?height: 32px;[\s\S]*?overflow: hidden;[\s\S]*?line-height: 32px;/u,
  )
  assert.match(
    picker,
    /\.knowledge-card-heading strong\s*\{[\s\S]*?text-overflow: ellipsis;[\s\S]*?white-space: nowrap;/u,
  )
  assert.doesNotMatch(picker, /\.knowledge-card-heading strong\s*\{[^}]*-webkit-line-clamp: 2;/u)
})

test('产品关联图片在资料库、知识库和产品目录使用同一解析规则', () => {
  assert.match(
    catalog,
    /export function previewResource\(resource: LibraryItem, resources: LibraryItem\[\]\)/u,
  )
  assert.match(catalog, /\['image', 'logo', 'video', 'audio'\]\.includes\(item\.resource_kind\)/u)
  assert.match(libraryCatalog, /previewResource as resolvePreviewResource/u)
  assert.match(libraryCatalog, /relatedMedia as resolveRelatedMedia/u)
  assert.match(picker, /previewResource as resolvePreviewResource/u)
  assert.match(picker, /\{\{ cardRelatedMediaCount\(item\) \}\} 项关联素材/u)
  assert.match(productPicker, /previewResource\(selectedProduct\.value, props\.resources\)/u)
  assert.doesNotMatch(picker, /function isImageResource/u)
})

test('产品名称使用资料库分类与名称级联选择', () => {
  assert.match(form, /<ContentProductPicker/u)
  assert.match(form, /:product-resource-id="modelValue\.product_resource_id"/u)
  assert.match(productPicker, /<ElCascader/u)
  assert.match(productPicker, /placeholder="请选择产品分类 \/ 产品名称"/u)
  assert.match(productPicker, /productGroupLabel\(item, props\.resources\)/u)
  assert.match(productPicker, /\[item\.resource_entry_id, item\.id\]\.includes\(resourceId\)/u)
  assert.match(productPicker, /Array\.isArray\(value\) \? value\.at\(-1\) : value/u)
  assert.match(form, /@change="updateProduct"/u)
  assert.match(form, /replaceProductKnowledgeSelection\(/u)
  assert.match(form, /product: name,[\s\S]*?product_resource_id: resourceId,[\s\S]*?knowledge_resource_ids: nextKnowledgeResourceIds/u)
  assert.doesNotMatch(
    form,
    /update\('product', name\)[\s\S]*?update\('product_resource_id', resourceId\)/u,
  )
})

test('完整、文案和图片入口在第二阶段填写要求，快捷视频入口保留首屏补充要求', () => {
  assert.match(form, /showRequirements\?: boolean/u)
  assert.match(form, /<ElFormItem v-if="showRequirements" label="要求" class="full-field">/u)
  assert.match(form, /placeholder="可粘贴已有标题\/文案，或直接输入需求"/u)
  assert.equal(
    [...taskView.matchAll(/:show-requirements="currentMode === 'video'"/gu)].length,
    2,
  )
  assert.match(taskView, /<span>阶段补充要求<\/span>/u)
  assert.match(taskView, /requirements: value\.requirements/u)
  assert.ok([...taskView.matchAll(/requirements: taskForm\.requirements/gu)].length >= 2)
  assert.doesNotMatch(taskView, /已有文案（可选）/u)
})

test('文案流程包含智能标题且文案阶段不再重复配置', () => {
  assert.match(workflow, /label: '智能标题'/u)
  assert.match(workflow, /copy: \['configuration', 'topic', 'copy', 'compliance', 'export'\]/u)
  assert.doesNotMatch(taskView, /智能文案将自动读取任务配置与已确认标题/u)
  assert.doesNotMatch(taskView, /本次生成指令/u)
  assert.doesNotMatch(taskView, /generationForm\.instruction/u)
  assert.match(taskView, /v-if="activeStageKey !== 'copy'" class="generation-status"/u)
  assert.match(artifactCard, /class="topic-title-button"/u)
  assert.match(artifactCard, /@click="emit\('edit', artifact\)"/u)
})

test('智能标题查看与修改只保留标题', () => {
  assert.match(artifactEditor, /<ElFormItem v-if="artifact\.kind === 'topic'" label="标题">/u)
  assert.doesNotMatch(artifactEditor, /label="内容方向"/u)
  assert.doesNotMatch(artifactEditor, /label="摘要"/u)
  assert.doesNotMatch(artifactEditor, /下列字段是业务人员最终查看和确认的内容/u)
  assert.doesNotMatch(artifactCard, /topicDescription/u)
  assert.doesNotMatch(artifactCard, /点击修改标题，或告诉 AI 如何调整/u)
})

test('AI 修改必须改变目标字段且正文改写可以保留标题', () => {
  const source = {
    candidates: [{ title: '原始标题', body: '原始正文' }],
    selected_candidate_index: 0,
  }
  assert.equal(
    hasMaterialTransformChange(
      'copy',
      source,
      { candidates: [{ title: '原始标题', body: '正文已扩写' }] },
      '扩写正文',
    ),
    true,
  )
  assert.equal(
    hasMaterialTransformChange(
      'copy',
      source,
      { candidates: [{ title: '原始标题', body: '正文已改写' }] },
      '标题更简洁，正文更生动',
    ),
    false,
  )
  assert.equal(
    hasMaterialTransformChange(
      'copy',
      source,
      { candidates: [{ title: '原始标题', body: '正文已改写' }] },
      '换个标题，并扩写正文',
    ),
    false,
  )
  assert.equal(
    hasMaterialTransformChange(
      'topic',
      source,
      { candidates: [{ title: '原始标题', body: '正文已改写' }] },
    ),
    false,
  )
  assert.match(taskView, /hasMaterialTransformChange\(/u)
  assert.match(taskView, /AI 未按要求修改内容/u)
  assert.match(artifactEditor, /const editingSelected = selectedIndex === index/u)
  assert.match(artifactEditor, /selected_candidate: next\[index\]/u)
})

test('生成结果卡不展示内部模型与数据元信息', () => {
  assert.doesNotMatch(artifactCard, /aria-label="生成信息"/u)
  assert.doesNotMatch(artifactCard, /模型：/u)
  assert.doesNotMatch(artifactCard, /引用资料：/u)
  assert.doesNotMatch(artifactCard, /数据等级：/u)
  assert.match(artifactCard, /v-else-if="!canPreviewMedia\(artifact\.kind\)"/u)
  assert.doesNotMatch(resourcePicker, /可选，不选择则仅按当前内容生成/u)
})

test('内容首页主视觉文案和按钮位于左侧且移除人工确认说明', () => {
  const hero = home.match(/<section class="home-intro">([\s\S]*?)<\/section>/u)?.[1] ?? ''
  const heroStyle = home.match(/\.home-intro\s*\{([\s\S]*?)\}/u)?.[1] ?? ''
  assert.match(hero, /从一个想法，到一套可确认、可追溯的内容资产/u)
  assert.match(hero, /开始完整内容创作/u)
  assert.doesNotMatch(hero, /生成过程中的选题、文案、图片、分镜、配音字幕和视频均由人工查看、修改与确认/u)
  assert.match(heroStyle, /align-items: flex-start;/u)
  assert.doesNotMatch(heroStyle, /align-items: flex-end;/u)
})

test('文案结果卡固定使用确认标题并只展示标题和正文', () => {
  const candidate = {
    title: '候选标题',
    summary: '底层摘要',
    body: '正文文案',
    direction: '创作方向',
  }
  assert.equal(candidateText(candidate, 'copy'), '候选标题\n正文文案')
  assert.equal(candidate.summary, '底层摘要')
  assert.equal(candidateText(candidate), '候选标题\n正文文案\n创作方向')
  assert.equal(candidateText(candidate, 'storyboard'), candidateText(candidate))
  assert.equal(
    artifactText({ kind: 'copy', content: { candidates: [candidate] } }),
    '候选标题\n正文文案',
  )
  assert.equal(
    artifactText({
      kind: 'copy',
      content: { title: '直接标题', summary: '直接摘要', text: '旧版正文' },
    }),
    '直接标题\n旧版正文',
  )
  assert.match(artifactCard, /fixedTitle \|\| '请先确认标题'/u)
  assert.match(artifactCard, /copyBody\(candidate\)/u)
  assert.match(
    workflow,
    /artifactKind === 'copy'\s*\? \[candidate\.title, candidate\.body \|\| candidate\.text\]\s*:\s*\[candidate\.title, candidate\.body, candidate\.direction\]/u,
  )
  assert.match(workflow, /\.map\(candidate => candidateText\(candidate, artifact\.kind\)\)/u)
  assert.match(history, /historyArtifactSections/u)
  assert.match(artifactEditor, /label="已确认标题"/u)
  assert.match(artifactEditor, /fixedTitle \|\| '请返回上一步确认标题'/u)
  assert.doesNotMatch(artifactEditor, /candidateValue\(candidate, 'summary'\)/u)
  assert.match(taskView, /const confirmedTopicTitle = computed/u)
  assert.match(taskView, /copyContentWithConfirmedTitle/u)
})

test('AI 修改使用当前草稿预览，不隐式保存标题或正文', () => {
  const transformCopy
    = taskView.match(/async function transformCopy\(\)[\s\S]*?(?=\nasync function removeArtifact)/u)?.[0]
      ?? ''
  assert.doesNotMatch(transformCopy, /contentApi\.patchArtifact\(|refreshWorkflowAndArtifacts\(|flushArtifactSelection\(/u)
  assert.match(transformCopy, /artifactDraft\.value/u)
  assert.match(transformCopy, /const preview = await contentApi\.previewTransformCopy/u)
  assert.match(transformCopy, /draftContent,\s*source\.version/u)
  assert.match(transformCopy, /source\.kind,\s*draftContent,\s*preview\.content/u)
})

test('图片分辨率直接采用服务能力并覆盖 2K 3K 4K', () => {
  assert.match(imageControls, /props\.sizeOptions\.map/u)
  assert.match(imageControls, /\^\[234\]K\$/u)
  assert.match(imageControls, /输出分辨率（可选）/u)
  assert.doesNotMatch(imageControls, /pixelSizePresets/u)
  assert.match(workflow, /return '模型自动'/u)
})

test('图片比例和分辨率直接位于常用设置且移除旧参考入口', () => {
  assert.match(
    imageControls,
    /<strong>常用画面设置<\/strong>[\s\S]*?<ElFormItem label="画面比例（可选）">[\s\S]*?<ElFormItem label="输出分辨率（可选）">[\s\S]*?<ElFormItem label="本地参考图片（可选）"/u,
  )
  for (const removedCopy of ['更多画面设置', '当前任务参考图片', '企业素材参考图片']) {
    assert.doesNotMatch(imageControls, new RegExp(removedCopy, 'u'))
  }
})

test('图片制作本地多图与知识库视觉资料共用模型参考图上限', () => {
  assert.match(taskView, /parameterRange\(activeCapability\.value, 'reference_images'\)\?\.max/u)
  assert.match(taskView, /:reference-limit="imageReferenceLimit"/u)
  assert.match(imageControls, /referenceLimit\?: number/u)
  assert.match(
    imageControls,
    /参考图片 \{\{ referenceImageCount \}\}\/\{\{ referenceSelectionLimit \?\? '未声明' \}\}/u,
  )
  assert.match(imageControls, /:disabled="referenceSelectionLimit == null/u)
  assert.match(imageControls, /nextCount > referenceSelectionLimit\.value/u)
  assert.doesNotMatch(imageControls, /reference_(?:asset|artifact)_ids[^\n]*\.slice\(0,/u)

  assert.match(
    imageControls,
    /import ImageReferenceUploader from '\.\/ImageReferenceUploader\.vue'/u,
  )
  assert.match(imageReferenceUploader, /<ElUpload/u)
  assert.match(imageReferenceUploader, /\n\s+multiple\n/u)
  assert.match(imageReferenceUploader, /const uploaded: LibraryItem\[\] = \[\]/u)
  assert.match(imageReferenceUploader, /emit\('uploaded', uploaded\)/u)
  assert.match(imageControls, /\.\.\.items\.map\(item => item\.id\)/u)
  assert.match(imageControls, /update\('reference_asset_ids', \[\.\.\.new Set\(ids\)\]\)/u)
  assert.match(
    imageControls,
    /\(referenceSelectionLimit\.value \?\? 0\) - referenceImageCount\.value/u,
  )
  assert.match(imageControls, /:remaining="referenceRemainingCount"/u)

  assert.match(imageReferenceLimits, /item\.resource_entry_id \|\| item\.id/u)
  assert.match(imageReferenceLimits, /\['image', 'logo'\]\.includes\(item\.resource_kind\)/u)
  assert.match(imageReferenceLimits, /for \(const id of selection\.knowledgeResourceIds\)/u)
  assert.match(
    imageReferenceLimits,
    /import \{ relatedMedia \} from '\.\/contentResourceCatalog'/u,
  )
  assert.match(imageReferenceLimits, /export function productVisualReferenceResources/u)
  assert.match(imageReferenceLimits, /relatedMedia\(product, resources\)/u)
  assert.match(
    imageReferenceLimits,
    /productVisualReferenceResources\(resources, selection\.knowledgeResourceIds\)/u,
  )
  assert.match(imageReferenceLimits, /for \(const id of selection\.libraryAssetIds\)/u)
  assert.match(
    imageReferenceLimits,
    /ids\.add\(`library:\$\{libraryResourceCanonicalId\(item\)\}`\)/u,
  )
  assert.match(taskView, /updateImageKnowledgeReferences/u)
  assert.match(taskView, /:selection-validator="validateImageKnowledgeReferenceSelection"/u)
  assert.match(picker, /selectionValidator\?: \(ids: string\[\]\) => string/u)
  assert.match(picker, /class="knowledge-selection-error"/u)
  assert.match(taskView, /nextCount > limit/u)
  assert.match(taskView, /referenceCount > imageReferenceLimit\.value/u)
  assert.match(imageControls, /产品资料关联的产品图会自动作为参考图/u)
  assert.match(imageControls, /selectedProductVisualReferences\.length/u)
  assert.match(imageControls, /<ContentLibraryMedia :item="item"/u)
  assert.match(taskView, /source\.content\.knowledge_resource_ids/u)
  assert.match(
    taskView,
    /knowledge_resource_ids: imageRevisionKnowledgeResourceIds\(source\)/u,
  )
  const imageRevision
    = taskView.match(/async function generateImageRevision\(\)[\s\S]*?(?=\nasync function saveArtifactEdit)/u)?.[0]
      ?? ''
  assert.doesNotMatch(imageRevision, /knowledge_resource_ids:\s*\[\]/u)
})

test('图片修改请求的幂等键显式包含本次修改指令', () => {
  const first = createIdempotencyKey('task-1', 'image', '背景改成夜景')
  const second = createIdempotencyKey('task-1', 'image', '背景改成白天')
  const repeated = createIdempotencyKey('task-1', 'image', '背景改成夜景')
  assert.equal(first.split(':')[2], repeated.split(':')[2])
  assert.notEqual(first.split(':')[2], second.split(':')[2])
  assert.notEqual(first, second)
  assert.ok(first.length <= 128)
  assert.ok(second.length <= 128)
})

test('历史内容支持时间搜索、状态筛选、分页与多层版本查看', () => {
  assert.match(history, /<h2>历史任务<\/h2>/u)
  assert.match(history, /type="daterange"/u)
  assert.match(history, /搜索任务、主题、产品或内容类型/u)
  assert.match(history, /v-for="task in paginatedTasks"/u)
  assert.match(history, /<ElPagination/u)
  assert.match(history, /layout="total, sizes, prev, pager, next, jumper"/u)
  assert.match(history, /:total="filteredTasks\.length"/u)
  assert.match(history, /<h4>当前内容<\/h4>/u)
  assert.match(history, /<h4>全部版本记录<\/h4>/u)
  assert.match(history, /title="版本记录详情"/u)
  assert.match(history, /contentApi\.listTasks\(\{ includeArchived: true \}\)/u)
  assert.match(history, /继续配置/u)
  assert.match(history, /deleteActiveTask/u)
  assert.match(history, /contentApi\.deleteTask\(task\.id\)/u)
  assert.match(history, /删除任务/u)
  assert.match(history, /内容说明/u)
  assert.match(history, /导出此版本/u)
  assert.match(history, /导出已确认素材包/u)
})

if (failures.length) {
  console.error(`Content configuration UI contract failed: ${failures.length} check(s) failed.`)
  process.exitCode = 1
}
else {
  console.info('Content configuration UI contract passed: 24 checks passed.')
}
