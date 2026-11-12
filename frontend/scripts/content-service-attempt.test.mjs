import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import process from 'node:process'
import ts from 'typescript'
import { formatContentGenerationError } from '../src/api/agent/contentGenerationErrors.ts'

function scriptSetupSource(path) {
  const filename = new URL(path, import.meta.url)
  const source = readFileSync(filename, 'utf8')
  const opening = source.indexOf('<script setup')
  const contentStart = source.indexOf('>', opening) + 1
  const contentEnd = source.indexOf('</script>', contentStart)
  assert.ok(opening >= 0 && contentStart > opening && contentEnd > contentStart, `${path} 缺少 script setup`)
  return source.slice(contentStart, contentEnd)
}

function extractTopLevelFunctions(source, names, filename) {
  const sourceFile = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  )
  const requested = new Set(names)
  const declarations = new Map()
  for (const statement of sourceFile.statements) {
    if (
      ts.isFunctionDeclaration(statement)
      && statement.name
      && requested.has(statement.name.text)
    ) {
      declarations.set(statement.name.text, statement.getText(sourceFile))
    }
  }
  for (const name of names) {
    assert.ok(declarations.has(name), `${filename} 中未找到真实函数 ${name}`)
  }
  return names.map(name => declarations.get(name)).join('\n\n')
}

async function compileHarness({ source, functions, bindings, exposed, filename }) {
  const declarations = extractTopLevelFunctions(source, functions, filename)
  const harnessSource = `
    export function createHarness(context: Record<string, unknown>) {
      const { ${bindings.join(', ')} } = context as any
      ${declarations}
      return { ${exposed.join(', ')} }
    }
  `
  const transpiled = ts.transpileModule(harnessSource, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: filename,
    reportDiagnostics: true,
  })
  const errors = (transpiled.diagnostics ?? []).filter(
    diagnostic => diagnostic.category === ts.DiagnosticCategory.Error,
  )
  assert.deepEqual(errors, [], `${filename} 行为测试夹具转译失败`)
  // eslint-disable-next-line no-unsanitized/method -- the data URL contains only AST-extracted repository source plus this local test harness.
  const module = await import(
    `data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString('base64')}`,
  )
  return module.createHarness
}

function state(value) {
  return { value }
}

const taskViewSource = scriptSetupSource('../src/views/agent/content/ContentTaskView.vue')
const editorSource = scriptSetupSource(
  '../src/views/agent/content/video-editor/VideoEditorWorkspace.vue',
)

const createTaskServiceHarness = await compileHarness({
  source: taskViewSource,
  functions: [
    'capabilityForStage',
    'capabilityForOperation',
    'statusForCapability',
    'isGenerationUnavailable',
    'unavailableMessage',
    'ensureGenerationAvailable',
  ],
  bindings: [
    'activeStageKey',
    'capabilities',
    'serviceStatus',
    'busy',
    'contentApi',
    'ElMessage',
  ],
  exposed: ['isGenerationUnavailable', 'ensureGenerationAvailable'],
  filename: 'ContentTaskView.service-attempt.ts',
})

const createEditorServiceHarness = await compileHarness({
  source: editorSource,
  functions: ['ensureEditorAiService'],
  bindings: [
    'speechGenerationAvailable',
    'subtitleGenerationAvailable',
    'aiVideoCapabilityReady',
    'aiServiceAttemptChecking',
    'loadAiVideoCapabilities',
    'aiVideoCapabilityError',
    'ElMessage',
  ],
  exposed: ['ensureEditorAiService'],
  filename: 'VideoEditorWorkspace.service-attempt.ts',
})

const createMediaJobHarness = await compileHarness({
  source: editorSource,
  functions: ['acceptMediaJobUpdate'],
  bindings: [
    'exactPreviewJob',
    'exactPreviewBusy',
    'isTerminalJob',
    'prepareExactPreview',
    'exportJob',
    'exporting',
    'downloadMediaJob',
    'liveMessage',
    'ElMessage',
    'aiVideoJob',
    'videoGenerating',
    'notifiedAiVideoFailureId',
    'friendlyContentError',
    'refreshProjectAssets',
    'messageOf',
    'formatContentGenerationError',
  ],
  exposed: ['acceptMediaJobUpdate'],
  filename: 'VideoEditorWorkspace.media-job-update.ts',
})

const configuredImageCapability = {
  capability: 'image-generation',
  provider_id: 'image-provider',
  enabled: true,
  mode: 'real',
  parameters: {},
}

const tests = []

function test(name, callback) {
  tests.push({ name, callback })
}

test('生成预检缺少配置时提示并复位 busy，修复配置后再次点击可以通过', async () => {
  const busy = state('')
  const capabilities = state({ capabilities: [] })
  const serviceStatus = state({ services: [] })
  const errors = []
  let capabilityCalls = 0
  let statusCalls = 0
  const capabilityResponses = [
    { capabilities: [] },
    { capabilities: [configuredImageCapability] },
  ]
  const statusResponse = {
    services: [{
      capability: 'image-generation',
      provider_id: 'image-provider',
      status: 'unavailable',
      consecutive_failures: 5,
    }],
  }
  const harness = createTaskServiceHarness({
    activeStageKey: state('image'),
    capabilities,
    serviceStatus,
    busy,
    contentApi: {
      capabilities: async () => capabilityResponses[capabilityCalls++],
      serviceStatus: async () => {
        statusCalls += 1
        return statusResponse
      },
    },
    ElMessage: { error: message => errors.push(message) },
  })

  assert.equal(await harness.ensureGenerationAvailable(), false)
  assert.equal(busy.value, '')
  assert.deepEqual(errors, ['暂时无法读取这项 AI 功能，请稍后再次点击生成。'])

  assert.equal(await harness.ensureGenerationAvailable(), true)
  assert.equal(busy.value, '')
  assert.equal(capabilityCalls, 2)
  assert.equal(statusCalls, 2)
  assert.equal(errors.length, 1)
})

test('已配置服务的 unavailable 或 degraded 健康状态不会阻止生成入口', () => {
  for (const status of ['unavailable', 'degraded']) {
    const harness = createTaskServiceHarness({
      activeStageKey: state('image'),
      capabilities: state({ capabilities: [configuredImageCapability] }),
      serviceStatus: state({
        services: [{
          capability: 'image-generation',
          provider_id: 'image-provider',
          status,
          last_success_at: null,
          consecutive_failures: 20,
        }],
      }),
      busy: state(''),
      contentApi: {},
      ElMessage: { error() {} },
    })

    assert.equal(harness.isGenerationUnavailable(), false, `${status} 不应成为历史健康门禁`)
  }
})

test('视频编辑器缺配置后复位 checking，下一次点击会重新检查并可通过', async () => {
  const aiVideoCapabilityReady = state(false)
  const aiServiceAttemptChecking = state(false)
  const warnings = []
  let attempts = 0
  const harness = createEditorServiceHarness({
    speechGenerationAvailable: state(false),
    subtitleGenerationAvailable: state(false),
    aiVideoCapabilityReady,
    aiServiceAttemptChecking,
    aiVideoCapabilityError: state('当前 AI 视频服务尚未配置'),
    loadAiVideoCapabilities: async () => {
      attempts += 1
      if (attempts === 2) {
        aiVideoCapabilityReady.value = true
      }
    },
    ElMessage: { warning: message => warnings.push(message) },
  })

  assert.equal(await harness.ensureEditorAiService('video'), false)
  assert.equal(aiServiceAttemptChecking.value, false)
  assert.deepEqual(warnings, ['当前 AI 视频服务尚未配置'])

  assert.equal(await harness.ensureEditorAiService('video'), true)
  assert.equal(aiServiceAttemptChecking.value, false)
  assert.equal(attempts, 2)
  assert.equal(warnings.length, 1)
})

test('AI 视频失败结束 loading 且同轮只提示一次，排队重试后失败会再次提示', async () => {
  const aiVideoJob = state({ id: 'video-job-1', status: 'queued', progress: 0 })
  const videoGenerating = state(true)
  const notifiedAiVideoFailureId = state('')
  const errors = []
  const harness = createMediaJobHarness({
    exactPreviewJob: state(undefined),
    exactPreviewBusy: state(false),
    isTerminalJob: job => ['succeeded', 'failed', 'cancelled'].includes(job.status),
    prepareExactPreview: async () => {},
    exportJob: state(undefined),
    exporting: state(false),
    downloadMediaJob: async () => false,
    liveMessage: state(''),
    ElMessage: {
      error: message => errors.push(message),
      success() {},
      warning() {},
    },
    aiVideoJob,
    videoGenerating,
    notifiedAiVideoFailureId,
    friendlyContentError: (message, fallback) => message || fallback,
    refreshProjectAssets: async () => {},
    messageOf: error => String(error),
    formatContentGenerationError,
  })
  const failed = {
    id: 'video-job-1',
    status: 'failed',
    progress: 35,
    error: '参考图片需要调整',
  }

  await harness.acceptMediaJobUpdate(failed)
  assert.equal(videoGenerating.value, false)
  assert.equal(aiVideoJob.value, failed)
  assert.deepEqual(errors, ['参考图片需要调整'])

  await harness.acceptMediaJobUpdate({ ...failed })
  assert.equal(errors.length, 1, '重复轮询同一次失败不能重复提示')

  videoGenerating.value = true
  await harness.acceptMediaJobUpdate({
    id: 'video-job-1',
    status: 'queued',
    progress: 0,
    attempt: 2,
  })
  assert.equal(notifiedAiVideoFailureId.value, '')

  await harness.acceptMediaJobUpdate({ ...failed, attempt: 2 })
  assert.equal(videoGenerating.value, false)
  assert.deepEqual(errors, ['参考图片需要调整', '参考图片需要调整'])
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
  console.info(`Content service attempt behavior passed: ${tests.length} checks passed.`)
}
