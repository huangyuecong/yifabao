import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import vm from 'node:vm'
import axios, { AxiosHeaders } from 'axios'
import ts from 'typescript'
import { readonly, ref } from 'vue'

const root = resolve('src/auth/entrySession.ts')

function fixture(replies = [], dependencies = {}, session = new Map()) {
  const calls = []
  const navigation = []
  const removed = []
  const events = []
  const env = { VITE_CONTENT_AUTH_MODE: 'entry', VITE_AGENT_DEV_MODE: 'true', BASE_URL: '/content/', VITE_CONTENT_API_URL: '/content/api' }
  const window = {
    sessionStorage: {
      getItem: key => session.get(key) ?? null,
      setItem: (key, value) => session.set(key, value),
      removeItem: key => session.delete(key),
    },
    location: {
      origin: 'https://aihub.test',
      pathname: '/content/agent/content/library',
      search: '?page=2',
      hash: '#item',
      assign: value => navigation.push(['assign', value]),
      replace: value => navigation.push(['replace', value]),
    },
    dispatchEvent: event => events.push(event.type),
    addEventListener() {},
  }
  const cache = new Map()
  function load(path) {
    if (cache.has(path)) {
      return cache.get(path)
    }
    const source = readFileSync(path, 'utf8').replaceAll('import.meta.env', '__env')
    const code = ts.transpileModule(source, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
    }).outputText
    const exports = {}
    cache.set(path, exports)
    vm.runInNewContext(code, {
      exports,
      __env: env,
      window,
      URL,
      URLSearchParams,
      Headers,
      Event,
      AbortSignal,
      AbortController,
      setTimeout,
      clearTimeout,
      Blob,
      localStorage: { removeItem: key => removed.push(key), getItem() {
        throw new Error('SSO must not read stored tokens')
      } },
      fetch: async (path, options) => {
        calls.push({ path, options })
        const reply = replies.shift()
        assert.ok(reply, `Unexpected fetch: ${path}`)
        if (reply instanceof Error) {
          throw reply
        }
        return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), { status: reply.status ?? 200, headers: { 'Content-Type': 'application/json' } })
      },
      require(name) {
        if (Object.hasOwn(dependencies, name)) {
          return dependencies[name]
        }
        if (name === 'vue') {
          return { ref, readonly }
        }
        if (name === 'axios') {
          return { __esModule: true, default: axios, AxiosHeaders }
        }
        if (name === '@/auth/entrySession') {
          return load(root)
        }
        if (name === '@/auth/workspaceMode') {
          return load(resolve('src/auth/workspaceMode.ts'))
        }
        assert.ok(name.startsWith('.'), `Unexpected import: ${name}`)
        return load(resolve(dirname(path), `${name}.ts`))
      },
    })
    return exports
  }
  const api = load(root)
  return { ...api, calls, navigation, removed, events, env, load, session }
}
const user = { subject: 'user-a', display_name: '用户 A', roles: ['employee'], permissions: [] }
const verified = { ...user, roles: ['user'], department: '市场部', clearance: 'internal' }
const authenticated = () => ({ body: { authenticated: true, user } })

test('real SDK uses same-origin cookies, verifies backend identity, and deduplicates concurrent checks', async () => {
  const f = fixture([authenticated(), { body: verified }])
  assert.deepEqual(await Promise.all([f.ensureEntrySession('/content/agent/content/library'), f.ensureEntrySession('/content/')]), [true, true])
  assert.equal(f.calls.length, 2)
  assert.equal(f.calls[0].path, '/api/auth/me')
  assert.equal(f.calls[1].path, '/content/api/v1/identity')
  assert.equal(f.entryIdentity.value.subject, 'user-a')
  assert.deepEqual([...f.entryIdentity.value.roles], ['user'])
  for (const call of f.calls) {
    assert.equal(call.options.credentials, 'include')
    assert.equal(call.options.cache, 'no-store')
    assert.equal(call.options.headers, undefined)
  }
})

test('not logged in redirects once to Entry and preserves deep links', async () => {
  const f = fixture([{ status: 401, body: { authenticated: false } }])
  assert.equal(await f.ensureEntrySession('/content/agent/content/tasks/1?stage=copy#draft'), false)
  f.loginAtEntry('/content/')
  assert.equal(f.navigation.length, 1)
  const redirect = new URL(f.navigation[0][1], 'https://aihub.test')
  assert.equal(redirect.pathname, '/api/auth/login')
  assert.equal(redirect.searchParams.get('return_to'), '/content/agent/content/tasks/1?stage=copy#draft')
})

test('Entry outage and forbidden user produce clear errors without a login loop', async () => {
  for (const status of [403, 500, 503]) {
    const f = fixture([{ status, body: { detail: 'unavailable' } }])
    assert.equal(await f.ensureEntrySession('/content/'), false)
    assert.equal(f.navigation.length, 0)
    assert.match(f.entryAuthError.value, status === 403 ? /无权/ : /暂时不可用/)
  }
})

test('backend forbidden or mismatched development identity blocks entry without login', async () => {
  for (const response of [{ status: 403, body: {} }, { body: { ...verified, subject: 'dev-user', roles: ['admin'] } }]) {
    const f = fixture([authenticated(), response])
    assert.equal(await f.ensureEntrySession('/content/'), false)
    assert.ok(f.entryAuthError.value)
    assert.equal(f.entryIdentity.value, null)
    assert.equal(f.navigation.length, 0)
  }
})

test('backend session expiry retries Entry once and blocks a persistent failure after returning', async () => {
  const session = new Map()
  const path = '/content/agent/content/tasks/1?stage=copy#draft'
  const first = fixture([authenticated(), { status: 401, body: {} }], {}, session)
  assert.equal(await first.ensureEntrySession(path), false)
  assert.equal(first.entryIdentity.value, null)
  assert.equal(first.entryAuthError.value, '')
  assert.equal(first.navigation.length, 1)
  const redirect = new URL(first.navigation[0][1], 'https://aihub.test')
  assert.equal(redirect.pathname, '/api/auth/login')
  assert.equal(redirect.searchParams.get('return_to'), path)
  assert.equal(redirect.searchParams.size, 1)
  assert.ok(!redirect.href.includes(user.subject))

  const returned = fixture([authenticated(), { status: 401, body: {} }], {}, session)
  assert.equal(await returned.ensureEntrySession(path), false)
  assert.equal(returned.navigation.length, 0)
  assert.match(returned.entryAuthError.value, /重新验证/)
  assert.equal(returned.entryIdentity.value, null)
})

test('a successful backend identity clears the retry guard for later session expiry', async () => {
  const session = new Map()
  const expired = fixture([authenticated(), { status: 401, body: {} }], {}, session)
  await expired.ensureEntrySession('/content/')
  assert.equal(session.size, 1)
  const renewed = fixture([authenticated(), { body: verified }], {}, session)
  assert.equal(await renewed.ensureEntrySession('/content/'), true)
  assert.equal(session.size, 0)
  const nextExpiry = fixture([authenticated(), { status: 401, body: {} }], {}, session)
  await nextExpiry.ensureEntrySession('/content/')
  assert.equal(nextExpiry.navigation.length, 1)
})

test('login retry expires and applies separately when the Entry account changes', async () => {
  const session = new Map()
  const first = fixture([authenticated(), { status: 401, body: {} }], {}, session)
  await first.ensureEntrySession('/content/')
  const [key, record] = [...session][0]
  session.set(key, JSON.stringify({ ...JSON.parse(record), started_at: Date.now() - 3 * 60 * 1000 }))
  const later = fixture([authenticated(), { status: 401, body: {} }], {}, session)
  await later.ensureEntrySession('/content/')
  assert.equal(later.navigation.length, 1)
  const switched = fixture([
    { body: { authenticated: true, user: { ...user, subject: 'user-b' } } },
    { status: 401, body: {} },
  ], {}, session)
  await switched.ensureEntrySession('/content/')
  assert.equal(switched.navigation.length, 1)
})

test('unavailable browser storage cannot turn a persistent backend 401 into a login loop', async () => {
  const session = {
    get() {
      throw new Error('storage disabled')
    },
    set() {
      throw new Error('storage disabled')
    },
    delete() {
      throw new Error('storage disabled')
    },
  }
  const blocked = fixture([authenticated(), { status: 401, body: {} }], {}, session)
  assert.equal(await blocked.ensureEntrySession('/content/'), false)
  assert.equal(blocked.navigation.length, 0)
  assert.match(blocked.entryAuthError.value, /重新验证/)
  const healthy = fixture([authenticated(), { body: verified }], {}, session)
  assert.equal(await healthy.ensureEntrySession('/content/'), true)
})

test('account switch reloads page to discard previous user data', async () => {
  const f = fixture([authenticated(), { body: verified }, { body: { authenticated: true, user: { ...user, subject: 'user-b' } } }, { body: { ...verified, subject: 'user-b' } }])
  assert.equal(await f.ensureEntrySession('/content/'), true)
  assert.equal(await f.ensureEntrySession('/content/agent/content/library'), false)
  assert.deepEqual(f.navigation, [['replace', '/content/agent/content/library']])
  assert.equal(f.entryIdentity.value, null)
})

test('logout uses Entry POST and leaves app only after success', async () => {
  const f = fixture([{ status: 204 }])
  await f.logoutFromEntry()
  assert.equal(f.calls[0].path, '/api/auth/logout')
  assert.equal(f.calls[0].options.method, 'POST')
  assert.deepEqual(f.navigation, [['assign', '/']])
  const failed = fixture([{ status: 500, body: {} }])
  await assert.rejects(failed.logoutFromEntry())
  assert.equal(failed.navigation.length, 0)
})

test('login return path cannot point to another origin or auth retry page', () => {
  const f = fixture()
  for (const value of ['https://evil.test/content/', '//evil.test/', '/other', '/content/entry-auth', '/content/../outside']) {
    assert.equal(f.entryReturnTo(value), '/content/')
  }
})

test('business requests omit browser identity and tokens, but keep cookies and media range', async () => {
  const f = fixture()
  const { createAgentClient } = f.load(resolve('src/api/agent/http.ts'))
  const { client } = createAgentClient('/content/api')
  let request
  client.defaults.adapter = async (config) => {
    request = config
    return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
  }
  await client.get('/api/v1/assets/example', { headers: { 'Authorization': 'Bearer old-token', 'X-User-Id': 'dev-user', 'X-Roles': 'admin', 'X-Entry-Identity': 'fake', 'Range': 'bytes=0-100' } })
  assert.equal(request.withCredentials, true)
  for (const name of ['Authorization', 'X-User-Id', 'X-Roles', 'X-Entry-Identity']) {
    assert.equal(request.headers.has(name), false)
  }
  assert.equal(request.headers.get('Range'), 'bytes=0-100')
  client.defaults.adapter = async (config) => {
    throw new axios.AxiosError('unauthorized', 'ERR_BAD_REQUEST', config, null, { status: 401 })
  }
  await assert.rejects(client.get('/api/v1/identity'))
  assert.deepEqual(f.events, ['content-entry-unauthorized'])
  client.defaults.adapter = async (config) => {
    throw new axios.AxiosError('forbidden', 'ERR_BAD_REQUEST', config, null, { status: 403 })
  }
  await assert.rejects(client.get('/api/v1/identity'))
  assert.equal(f.events.length, 1)
})

function guardFixture(replies) {
  let guard
  let menuBuilds = 0
  const obsoleteLogin = () => {
    throw new Error('Entry mode must not use the legacy login or cached role menus')
  }
  const permission = { getRouters: [], async generateRoutes() {
    menuBuilds++
  } }
  const router = {
    beforeEach(callback) {
      guard = callback
    },
    afterEach() {},
    resolve: to => ({ href: `/content${to.fullPath}` }),
  }
  const f = fixture(replies, {
    './router': { __esModule: true, default: router },
    '@/config/axios/service': { isRelogin: {} },
    '@/hooks/web/useCache': { deleteUserCache: obsoleteLogin },
    '@/hooks/web/useNProgress': { useNProgress: () => ({ start() {}, done() {} }) },
    '@/hooks/web/usePageLoading': { usePageLoading: () => ({ loadStart() {}, loadDone() {} }) },
    '@/hooks/web/useTitle': { useTitle() {} },
    '@/store/modules/dict': { useDictStoreWithOut: obsoleteLogin },
    '@/store/modules/user': { useUserStoreWithOut: obsoleteLogin },
    '@/store/modules/permission': { usePermissionStoreWithOut: () => permission },
    '@/utils/auth': { getAccessToken: obsoleteLogin, removeToken: obsoleteLogin },
  })
  f.load(resolve('src/permission.ts'))
  return {
    ...f,
    async navigate(path, name = 'ContentLibrary') {
      const url = new URL(path, 'https://aihub.test')
      const next = []
      await guard({ path: url.pathname, fullPath: path, name }, {}, value => next.push(value))
      return next
    },
    menuBuilds: () => menuBuilds,
  }
}

test('SSO route guard verifies deep links before showing content, bypassing legacy login', async () => {
  const f = guardFixture([authenticated(), { body: verified }])
  assert.deepEqual(await f.navigate('/agent/content/library?page=2#item'), [undefined])
  assert.equal(f.menuBuilds(), 1)
  const unknown = guardFixture([authenticated(), { body: verified }])
  assert.deepEqual(await unknown.navigate('/user/profile'), ['/agent/content'])
})

test('SSO route guard preserves return path for login and retry, without exposing content', async () => {
  const path = '/agent/content/library?page=2#item'
  const f = guardFixture([{ status: 401, body: {} }])
  assert.deepEqual(await f.navigate(path), [false])
  const login = new URL(f.navigation[0][1], 'https://aihub.test')
  assert.equal(login.searchParams.get('return_to'), `/content${path}`)
  assert.equal(f.menuBuilds(), 0)
  const failed = guardFixture([authenticated(), { status: 403, body: {} }])
  const [next] = await failed.navigate(path)
  assert.equal(next.name, 'ContentEntryAuth')
  assert.equal(next.query.return_to, `/content${path}`)
  assert.equal(failed.navigation.length, 0)
  assert.deepEqual(await failed.navigate('/entry-auth', 'ContentEntryAuth'), [undefined])
})
