import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import process from 'node:process'
import ts from 'typescript'
import { createIdempotencyKey } from '../src/views/agent/content/contentWorkflow.ts'
import { imageGenerationFormState } from '../src/views/agent/content/imageGenerationDraft.ts'

const taskView = readFileSync(
  new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url),
  'utf8',
)
const scriptStart = taskView.indexOf('>', taskView.indexOf('<script setup')) + 1
const scriptSource = taskView.slice(scriptStart, taskView.indexOf('</script>', scriptStart))
const sourceFile = ts.createSourceFile(
  'ContentTaskView.ts',
  scriptSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS,
)
const functionNames = [
  'cleanParameters',
  'normalizedResourceIds',
  'imageRevisionKnowledgeResourceIds',
  // Keeps the revised image at the source size; added after this harness was written.
  'imageRevisionSize',
  'generateImageRevision',
]
const declarations = functionNames.map((name) => {
  const declaration = sourceFile.statements.find(statement =>
    ts.isFunctionDeclaration(statement) && statement.name?.text === name,
  )
  assert.ok(declaration, `未找到真实函数 ${name}`)
  return declaration.getText(sourceFile)
})
const bindings = [
  'editingArtifact',
  'imageRevisionInstruction',
  'task',
  'busy',
  'activeRun',
  'ensureGenerationAvailable',
  'imageReferenceLimit',
  'flushArtifactSelection',
  'currentMode',
  'activeStageKey',
  'contentApi',
  'generationForm',
  'imageGenerationFormState',
  'taskKnowledgeResourceIds',
  'createIdempotencyKey',
  'waitForJob',
  'refreshWorkflowAndArtifacts',
  'artifacts',
  'imageRevisionNotice',
  'editDialogOpen',
  'ElMessage',
  'handleWorkflowMutationError',
  'refreshServiceState',
]
const { outputText, diagnostics } = ts.transpileModule(`
  export function createHarness(context: Record<string, unknown>) {
    const { ${bindings.join(', ')} } = context as any
    ${declarations.join('\n\n')}
    return { generateImageRevision }
  }
`, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  reportDiagnostics: true,
})
assert.deepEqual(
  (diagnostics ?? []).filter(item => item.category === ts.DiagnosticCategory.Error),
  [],
)
// eslint-disable-next-line no-unsanitized/method -- only local repository AST declarations and this test harness are imported.
const { createHarness } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`,
)

const modification = '背景换成夜间露台，产品自然一些，保持包装文字准确'
const legacyPrompt = [
  '以当前参考图片为基础生成新版本，保留未明确要求修改的主体、构图和品牌元素。',
  `本次必须执行的修改要求：${modification}`,
].join('\n')
const state = value => ({ value })

function setup(mode, stage = 'image') {
  const source = {
    id: 'original-image',
    kind: 'image',
    version: 1,
    content: {
      ratio: '16:9',
      size: '4K',
      asset_id: 'original-asset',
      knowledge_resource_ids: ['product-original'],
    },
  }
  const form = {
    count: 4,
    image_prompt: '春节团圆',
    image_scene: '生活方式',
    image_style: '写实摄影',
    image_ratio: '1:1',
    image_size: '2K',
    reference_artifact_ids: ['another-image'],
    reference_asset_ids: ['upload-1'],
    knowledge_resource_ids: ['current-product'],
  }
  const calls = []
  const warnings = []
  const errors = []
  const waits = []
  let servicesRefreshed = 0
  const context = {
    editingArtifact: state(source),
    imageRevisionInstruction: state(`  ${modification}\n`),
    task: state({ id: 'image-task', mode }),
    busy: state(''),
    activeRun: state(undefined),
    ensureGenerationAvailable: async () => true,
    imageReferenceLimit: state(9),
    flushArtifactSelection: async () => true,
    currentMode: state(mode),
    activeStageKey: state(stage),
    contentApi: {
      generateStage: async (...args) => {
        calls.push(args)
        return { id: `run-${calls.length}`, job_ids: ['job-1'], artifact_ids: ['new-image'] }
      },
    },
    generationForm: form,
    imageGenerationFormState,
    taskKnowledgeResourceIds: () => ['task-product'],
    createIdempotencyKey,
    waitForJob: async id => waits.push(id),
    refreshWorkflowAndArtifacts: async () => {},
    artifacts: state([source, { id: 'new-image', kind: 'image', version: 2 }]),
    imageRevisionNotice: state(''),
    editDialogOpen: state(true),
    ElMessage: { warning: message => warnings.push(message), success() {} },
    handleWorkflowMutationError: async error => errors.push(error),
    refreshServiceState: async () => {
      servicesRefreshed += 1
    },
  }
  return {
    context,
    calls,
    warnings,
    errors,
    waits,
    source,
    form,
    sourceBefore: structuredClone(source),
    formBefore: structuredClone(form),
    servicesRefreshed: () => servicesRefreshed,
    ...createHarness(context),
  }
}

const tests = []
const test = (name, callback) => tests.push({ name, callback })

for (const mode of ['full', 'image']) {
  test(`${mode} 图片再生成只发送原始修改要求，保留源图、引用、参数和独立新请求`, async () => {
    const harness = setup(mode)
    await harness.generateImageRevision()
    await harness.generateImageRevision()
    assert.equal(harness.calls.length, 2)
    for (const [taskId, stage, request] of harness.calls) {
      assert.equal(taskId, 'image-task')
      assert.equal(stage, 'image')
      assert.equal(request.count, 1)
      assert.equal(request.instruction, modification)
      assert.equal(request.source_artifact_id, 'original-image')
      assert.deepEqual(request.parameters, {
        prompt: modification,
        ratio: '16:9',
        size: '4K',
        reference_artifact_ids: ['original-image'],
        reference_asset_ids: [],
        knowledge_resource_ids: ['product-original'],
        form_state: imageGenerationFormState(harness.formBefore),
      })
      assert.match(request.idempotency_key, /^image-task:image:/u)
    }
    assert.notEqual(harness.calls[0][2].idempotency_key, harness.calls[1][2].idempotency_key)
    assert.deepEqual(harness.source, harness.sourceBefore)
    assert.deepEqual(harness.form, harness.formBefore)
    assert.deepEqual(harness.waits, ['job-1', 'job-1'])
    assert.equal(harness.context.busy.value, '')
    assert.equal(harness.servicesRefreshed(), 2)
  })
}

for (const [mode, stage] of [['video', 'image'], ['copy', 'image'], ['full', 'video_preview'], ['image', 'export']]) {
  test(`${mode}/${stage} 不采用本次图片修改策略，保留原有请求模板`, async () => {
    const harness = setup(mode, stage)
    await harness.generateImageRevision()
    assert.equal(harness.calls.length, 1)
    assert.equal(harness.calls[0][2].parameters.prompt, legacyPrompt)
    assert.equal(harness.calls[0][2].instruction, modification)
  })
}

test('源图没有产品引用时仍按既有规则采用当前表单产品引用', async () => {
  const harness = setup('full')
  harness.source.content.knowledge_resource_ids = []
  await harness.generateImageRevision()
  assert.deepEqual(harness.calls[0][2].parameters.knowledge_resource_ids, ['current-product'])
})

test('空修改要求和不支持参考图的限制不变', async () => {
  const empty = setup('image')
  empty.context.imageRevisionInstruction.value = '  '
  await empty.generateImageRevision()
  assert.equal(empty.calls.length, 0)
  assert.deepEqual(empty.warnings, ['请先填写图片修改要求'])
  const unsupported = setup('full')
  unsupported.context.imageReferenceLimit.value = 0
  await unsupported.generateImageRevision()
  assert.equal(unsupported.calls.length, 0)
  assert.deepEqual(unsupported.warnings, ['当前图片模型不支持参考图片，无法基于现有图片继续生成'])
})

test('生成失败不修改源图或表单，恢复按钮状态并沿用已有错误处理', async () => {
  const harness = setup('image')
  const failure = new Error('图片服务暂时不可用')
  harness.context.contentApi.generateStage = async () => {
    throw failure
  }
  await harness.generateImageRevision()
  assert.deepEqual(harness.source, harness.sourceBefore)
  assert.deepEqual(harness.form, harness.formBefore)
  assert.deepEqual(harness.errors, [failure])
  assert.equal(harness.context.busy.value, '')
  assert.equal(harness.context.editDialogOpen.value, true)
})

for (const { name, callback } of tests) {
  try {
    await callback()
    console.info(`PASS ${name}`)
  }
  catch (error) {
    console.error(`FAIL ${name}`)
    console.error(error)
    process.exitCode = 1
  }
}
if (!process.exitCode) {
  console.info(`Image revision scope behavior passed: ${tests.length} checks passed.`)
}
