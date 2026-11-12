import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import axios, { AxiosHeaders } from 'axios'
import ts from 'typescript'
import { agentRequestPath } from '../src/api/agent/apiUrl.ts'

function standaloneClient() {
  let loginEvents = 0
  const exports = {}
  const dependencies = {
    'axios': { __esModule: true, default: axios, AxiosHeaders },
    '@/auth/workspaceMode': { isStandaloneMode: true },
    '@/auth/entrySession': { isEntrySso: false, reportEntryUnauthorized() {
      loginEvents++
    } },
    './apiUrl': { agentRequestPath },
  }
  const source = readFileSync(new URL('../src/api/agent/http.ts', import.meta.url), 'utf8')
  vm.runInNewContext(ts.transpileModule(source.replaceAll('import.meta.env', '__env'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    exports,
    __env: { VITE_AGENT_DEV_MODE: 'false' },
    Blob,
    get localStorage() {
      throw new Error('Standalone must not read cached company tokens')
    },
    require: (name) => {
      assert.ok(Object.hasOwn(dependencies, name), `Unexpected import: ${name}`)
      return dependencies[name]
    },
  })
  return { ...exports.createAgentClient('/content/api'), loginEvents: () => loginEvents }
}

test('standalone config requests omit login headers and retain the write intent header', async () => {
  const f = standaloneClient()
  let captured
  f.client.defaults.adapter = async (config) => {
    captured = config
    return { config, status: 200, statusText: 'OK', headers: {}, data: { revision: 2 } }
  }
  const result = await f.request({
    method: 'PUT',
    url: '/api/v1/ai-models/text',
    data: { provider: 'openai' },
    headers: {
      'Authorization': 'Bearer obsolete-token',
      'X-Entry-Identity': 'old-identity',
      'X-User-Id': 'someone-else',
      'X-User-Name': 'someone-else',
      'X-Department': 'old-company',
      'X-Roles': 'admin',
      'X-Content-Config-Request': '1',
    },
  })
  assert.equal(result.revision, 2)
  assert.equal(captured.url, '/v1/ai-models/text')
  assert.equal(captured.withCredentials, false)
  for (const name of ['Authorization', 'X-Entry-Identity', 'X-User-Id', 'X-User-Name', 'X-Department', 'X-Roles']) {
    assert.equal(captured.headers.has(name), false)
  }
  assert.equal(captured.headers.get('X-Content-Config-Request'), '1')
})

test('standalone API failures report the error without starting a company login', async () => {
  const f = standaloneClient()
  f.client.defaults.adapter = async (config) => {
    throw new axios.AxiosError('Rejected', 'ERR_BAD_REQUEST', config, {}, {
      config,
      status: 401,
      statusText: 'Unauthorized',
      headers: {},
      data: { detail: '本机访问检查失败' },
    })
  }
  await assert.rejects(f.request({ url: '/api/v1/identity' }), /本机访问检查失败/u)
  assert.equal(f.loginEvents(), 0)
})

test('Android frame errors retain a machine-readable code for read-only recovery', async () => {
  const f = standaloneClient()
  f.client.defaults.adapter = async (config) => {
    throw new axios.AxiosError('Rejected', 'ERR_BAD_REQUEST', config, {}, {
      config,
      status: 409,
      statusText: 'Conflict',
      headers: {},
      data: { detail: { code: 'android_stale_frame', message: '屏幕已变化，请刷新画面后操作。' } },
    })
  }
  await assert.rejects(f.request({ url: '/api/v1/automation/android/devices/test/actions' }), (error) => {
    assert.equal(error.code, 'android_stale_frame')
    assert.equal(error.status, 409)
    assert.equal(error.message, '屏幕已变化，请刷新画面后操作。')
    return true
  })
})
