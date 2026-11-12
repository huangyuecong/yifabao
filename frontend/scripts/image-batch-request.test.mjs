import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { ContentGenerationError, requestContentGeneration } from '../src/api/agent/contentGenerationErrors.ts'
import { createImageBatchRequests, imageBatchTimeout } from '../src/api/agent/imageBatchRequest.ts'

function apiFixture(send) {
  const code = readFileSync(new URL('../src/api/agent/content.ts', import.meta.url), 'utf8')
    .replaceAll('import.meta.env.VITE_CONTENT_API_URL', '"/content/api"')
  const client = { request: send }
  const modules = {
    './contentGenerationErrors': { requestContentGeneration },
    './http': { createAgentClient: () => ({ client, request: send }), downloadBlob() {} },
    './imageBatchRequest': { createImageBatchRequests, imageBatchTimeout },
  }
  const scope = { exports: {}, require: name => modules[name] }
  vm.runInNewContext(ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, scope)
  return scope.exports.contentApi
}

test('both image entry points keep a sequential eight-image response alive past provider timeouts', async () => {
  const calls = []
  const api = apiFixture(async (config) => {
    calls.push(config)
    return { data: [] }
  })
  for (const count of [1, 2, 8]) {
    await api.generateVideoProjectImages('project-1', { count, prompt: '图片', expected_revision: 1 })
    await api.generateStage('task-1', 'image', { count, parameters: {}, idempotency_key: `batch-${count}` })
    const [editor, task] = calls.slice(-2)
    assert.ok(editor.timeout >= count * 300_000)
    assert.equal(editor.timeout, task.timeout)
    assert.equal(task.headers['Idempotency-Key'], `batch-${count}`)
  }
  assert.equal(imageBatchTimeout(100), imageBatchTimeout(8))
  assert.equal(imageBatchTimeout(Number.NaN), imageBatchTimeout(1))
})

test('identical inflight project batches share one request and conflicting parameters cannot start another', async () => {
  let complete
  let requests = 0
  const api = apiFixture(async () => {
    requests += 1
    return new Promise((resolve) => {
      complete = resolve
    })
  })
  const payload = { expected_revision: 3, count: 2, prompt: '产品海报' }
  const first = api.generateVideoProjectImages('project-1', payload)
  const second = api.generateVideoProjectImages('project-1', { ...payload })
  assert.equal(first, second)
  await Promise.resolve()
  assert.equal(requests, 1)
  await assert.rejects(api.generateVideoProjectImages('project-1', { ...payload, count: 3 }), /已有图片正在生成/u)
  complete({ data: [{ id: 'image-1' }, { id: 'image-2' }] })
  assert.deepEqual(await first, await second)
  assert.equal(requests, 1)
})

test('network failures are not replayed; a provider rejection stays distinct from an uncertain result', async () => {
  let requests = 0
  const api = apiFixture(async () => {
    requests += 1
    throw Object.assign(new Error('timeout of 2460000ms exceeded'), { code: 'ECONNABORTED' })
  })
  await assert.rejects(api.generateVideoProjectImages('project-1', { count: 8, prompt: '图片' }), error => error.outcomeUncertain === true)
  await Promise.resolve()
  assert.equal(requests, 1)
  const rejected = new ContentGenerationError({ response: { status: 502, data: { detail: { provider_code: 'InvalidParameter', provider_status_code: 400, message: '参考素材格式不支持' } } } })
  assert.equal(rejected.outcomeUncertain, false)
  assert.equal(new ContentGenerationError({ response: { status: 504, data: '<html>gateway timeout</html>' } }).outcomeUncertain, true)
})
