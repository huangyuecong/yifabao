import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import axios, { AxiosHeaders } from 'axios'
import ts from 'typescript'
import { createMemoryHistory, createRouter } from 'vue-router'
import { rewriteContentApiPath } from '../build/contentProxy.ts'
import { productionPublicAssets } from '../build/vite/publicAssets.ts'
import { agentRequestPath, contentMediaUrl } from '../src/api/agent/apiUrl.ts'

const publicBase = '/content/api'

// Execute the real modules, replacing only build-time env and unrelated UI imports.
function loadTypeScript(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
    .replaceAll('import.meta.env', '__env')
  const code = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      esModuleInterop: true,
    },
  }).outputText
  const exports = {}
  vm.runInNewContext(code, {
    exports,
    __env: { VITE_AGENT_DEV_MODE: 'true' },
    useI18n: () => ({ t: key => key }),
    Blob,
    URL,
    require(name) {
      assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
      return dependencies[name]
    },
  }, { filename: path })
  return exports
}

const { createAgentClient, downloadBlob } = loadTypeScript('../src/api/agent/http.ts', {
  'axios': { __esModule: true, default: axios, AxiosHeaders },
  './apiUrl': { agentRequestPath },
  '@/auth/entrySession': { isEntrySso: false, reportEntryUnauthorized() {} },
  '@/auth/workspaceMode': { isStandaloneMode: false },
})

function capturedClient(baseURL = publicBase) {
  const result = createAgentClient(baseURL)
  const calls = []
  result.client.defaults.adapter = async (config) => {
    calls.push({ uri: result.client.getUri(config), config })
    return {
      data: config.responseType === 'blob' ? new Blob(['media']) : { ok: true },
      status: 200,
      statusText: 'OK',
      headers: { 'content-disposition': 'attachment; filename="reference.png"' },
      config,
    }
  }
  return { ...result, calls }
}

test('API requests and returned media paths share one public prefix', async () => {
  const { request, client, calls } = capturedClient()
  const paths = [
    '/api/v1/library/items/sample/download',
    '/content/api/v1/library/items/sample/download',
  ]
  for (const path of paths) {
    await request({ method: 'GET', url: path })
    const result = await downloadBlob(client, path)
    assert.equal(await result.blob.text(), 'media')
    assert.equal(result.fileName, 'reference.png')
    assert.equal(contentMediaUrl(path, publicBase), '/content/api/v1/library/items/sample/download')
  }
  assert.equal(calls.length, 4)
  for (const call of calls) {
    assert.equal(call.uri, '/content/api/v1/library/items/sample/download')
  }
})

test('encoded media identifiers and query strings survive frontend and development proxy', async () => {
  const { client, calls } = capturedClient()
  const suffix = '/v1/video-projects/project%2Fpart/assets/ref%20image/stream?signature=a%2Bb%2F%3D&name=%E4%B9%A0%E9%85%92'
  await downloadBlob(client, `/api${suffix}`)
  assert.equal(calls[0].uri, `/content/api${suffix}`)
  assert.equal(contentMediaUrl(`/api${suffix}`, publicBase), `/content/api${suffix}`)
  assert.equal(rewriteContentApiPath(calls[0].uri), `/api${suffix}`)
})

test('multipart uploads retain their request body, method and query parameters', async () => {
  const { request, calls } = capturedClient()
  const data = new FormData()
  data.append('file', new Blob(['reference'], { type: 'image/png' }), 'reference.png')
  await request({ method: 'POST', url: '/api/v1/tasks/task/image-references', data, params: { mode: 'local' } })
  assert.equal(calls[0].uri, '/content/api/v1/tasks/task/image-references?mode=local')
  assert.equal(calls[0].config.method, 'post')
  assert.equal(calls[0].config.data, data)
})

test('health remains the backend health endpoint while business APIs keep /api', async () => {
  const { request, calls } = capturedClient()
  await request({ url: '/health' })
  assert.equal(calls[0].uri, '/content/api/health')
  assert.equal(rewriteContentApiPath(calls[0].uri), '/health')
  assert.equal(rewriteContentApiPath('/content/api/health?probe=web'), '/health?probe=web')
  assert.equal(rewriteContentApiPath('/content/api/health/?probe=web'), '/health/?probe=web')
  assert.equal(rewriteContentApiPath('/content/api/v1/tasks?limit=20'), '/api/v1/tasks?limit=20')
  assert.equal(rewriteContentApiPath('/content/api/healthcheck'), '/api/healthcheck')
})

test('absolute URLs, data and Blob URLs are preserved', async () => {
  const { request, calls } = capturedClient()
  const urls = [
    'https://media.example.test/file.png?signature=a%2Bb',
    '//media.example.test/file.png',
    'blob:https://aihub.example.test/preview-id',
    'data:image/png;base64,aGVsbG8=',
  ]
  for (const url of urls) {
    assert.equal(agentRequestPath(url, publicBase), url)
    assert.equal(contentMediaUrl(url, publicBase), url)
  }
  await request({ url: urls[0] })
  assert.equal(calls[0].uri, urls[0])
  assert.equal(contentMediaUrl(undefined, publicBase), '')
  assert.equal(contentMediaUrl('/content/assets/brand.png', publicBase), '/content/assets/brand.png')
})

test('explicit legacy API bases remain usable without duplicate prefixes', async () => {
  const legacyBase = '/content-agent-api/'
  const { request, calls } = capturedClient(legacyBase)
  for (const path of ['/api/v1/tasks', '/content-agent-api/api/v1/tasks']) {
    await request({ url: path })
    assert.equal(contentMediaUrl(path, legacyBase), '/content-agent-api/api/v1/tasks')
  }
  assert.deepEqual(calls.map(call => call.uri), ['/content-agent-api/api/v1/tasks', '/content-agent-api/api/v1/tasks'])
})

test('trailing base slashes do not add duplicate separators', async () => {
  const { request, calls } = capturedClient('/content/api/')
  await request({ url: '/api/v1/tasks' })
  assert.equal(calls[0].uri, '/content/api/v1/tasks')
  assert.equal(contentMediaUrl('/api/v1/tasks', '/content/api/'), '/content/api/v1/tasks')
})

function deploymentRouter() {
  const contentRoutes = loadTypeScript('../src/router/modules/contentRoutes.ts')
  const { default: routes } = loadTypeScript('../src/router/modules/remaining.ts', {
    '@/utils/routerHelper': { Layout: {} },
    '@/auth/workspaceMode': { isStandaloneMode: false },
    './contentRoutes': contentRoutes,
  })
  const stubComponents = route => ({
    ...route,
    ...(route.component ? { component: {} } : {}),
    ...(route.children ? { children: route.children.map(stubComponents) } : {}),
  })
  return createRouter({
    history: createMemoryHistory('/content/'),
    strict: true,
    routes: routes.map(stubComponents),
  })
}

test('business and administrator entries resolve to the content workbench under the slug', async () => {
  const router = deploymentRouter()
  for (const entry of ['/', '/admin']) {
    assert.equal(router.resolve(entry).href, entry === '/' ? '/content/' : '/content/admin')
    await router.push(entry)
    assert.equal(router.currentRoute.value.name, 'ContentAgentWorkbench')
    assert.equal(router.currentRoute.value.meta.contentView, 'home')
    assert.equal(router.resolve(router.currentRoute.value.fullPath).href, '/content/agent/content')
  }
})

test('old administrator bookmarks preserve task location, query and hash', async () => {
  const router = deploymentRouter()
  await router.push('/admin/agent/content/tasks/task-123/stages/video_preview?mode=edit#timeline')
  assert.equal(router.currentRoute.value.name, 'ContentAgentTaskStage')
  assert.equal(router.currentRoute.value.params.taskId, 'task-123')
  assert.equal(router.currentRoute.value.params.stageKey, 'video_preview')
  assert.equal(router.currentRoute.value.query.mode, 'edit')
  assert.equal(router.currentRoute.value.hash, '#timeline')
  assert.equal(
    router.resolve(router.currentRoute.value.fullPath).href,
    '/content/agent/content/tasks/task-123/stages/video_preview?mode=edit#timeline',
  )
})

test('direct content-page reloads resolve their existing route metadata', () => {
  const router = deploymentRouter()
  for (const view of ['library', 'versions', 'services']) {
    const resolved = router.resolve(`/agent/content/${view}`)
    assert.equal(resolved.meta.contentView, view)
    assert.equal(resolved.href, `/content/agent/content/${view}`)
  }
  for (const mode of ['full', 'copy', 'image', 'video']) {
    const resolved = router.resolve(`/agent/content/create/${mode}`)
    assert.equal(resolved.meta.contentView, 'task')
    assert.equal(resolved.meta.contentMode, mode)
    assert.equal(resolved.href, `/content/agent/content/create/${mode}`)
  }
})

test('production public assets exclude test fixtures and preserve their local originals', () => {
  const root = mkdtempSync(join(tmpdir(), 'content-public-assets-'))
  const publicDir = join(root, 'public')
  const outDir = join(root, 'dist')
  const fixtures = ['_test_web_20260922/reference.png', 'nested/TEST_WEB_oversize.png', 'nested/_test_hidden/file.mp4']
  const assets = ['favicon.ico', 'nested/brand.png']
  try {
    for (const name of [...fixtures, ...assets]) {
      const destination = join(publicDir, name)
      mkdirSync(join(destination, '..'), { recursive: true })
      writeFileSync(destination, name)
    }
    const plugin = productionPublicAssets()
    assert.equal(plugin.apply, 'build')
    assert.equal(plugin.config().build.copyPublicDir, false)
    plugin.configResolved({ root, publicDir, build: { outDir } })
    plugin.writeBundle()
    for (const name of assets) {
      assert.equal(readFileSync(join(outDir, name), 'utf8'), name)
    }
    for (const name of fixtures) {
      assert.equal(existsSync(join(outDir, name)), false)
      assert.equal(readFileSync(join(publicDir, name), 'utf8'), name)
    }
  }
  finally {
    assert.ok(!relative(tmpdir(), root).startsWith('..'))
    rmSync(root, { recursive: true, force: true })
  }
})
