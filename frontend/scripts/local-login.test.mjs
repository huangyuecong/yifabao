import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import vm from 'node:vm'
import { compileScript, parse } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

function loadRedirect() {
  const source = readFileSync(resolve('src/views/Login/loginRedirect.ts'), 'utf8')
  const exports = {}
  vm.runInNewContext(
    ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText,
    { exports, URL },
  )
  return exports.loginRedirectPath
}
const loginRedirectPath = loadRedirect()
const file = resolve('src/views/Login/Login.vue')
const { descriptor, errors } = parse(readFileSync(file, 'utf8'), { filename: file })
assert.deepEqual(errors, [])
const compiled = compileScript(descriptor, { id: 'local-login-test' }).content
const script = ts.transpileModule(compiled.replaceAll('import.meta.env', '__env'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText

function fixture({ captcha = false, tenant = false, entry = false, api = {}, redirect } = {}) {
  const calls = []
  const tokens = []
  const navigation = []
  const loginApi = {
    async getTenantIdByName(name) {
      calls.push(['tenant', name])
      return 42
    },
    async getPublicKey() {
      calls.push(['key'])
      return { algorithm: 'SM2', key: 'test-public-key' }
    },
    async login(data) {
      calls.push(['login', data])
      return { accessToken: 'test-access', refreshToken: 'test-refresh' }
    },
    async getAuthorizeUrl(callback) {
      calls.push(['authorize', callback])
      return 'https://login.example.test/authorize'
    },
    ...api,
  }
  const dependencies = {
    vue,
    'vue-router': {
      useRoute: () => ({ query: { redirect } }),
      useRouter: () => ({
        replace: async path => navigation.push(path),
        resolve: () => ({ href: '/content/login?type=30&agentid=test-agent' }),
      }),
    },
    '@/api/login': loginApi,
    '@/auth/entrySession': { isEntrySso: entry },
    '@/components/Verifition': { Verify: {} },
    '@/hooks/web/useCache': { deleteUserCache: () => calls.push(['clear-user-cache']) },
    '@/store/modules/app': { useAppStore: () => ({ getTitle: '测试工作台' }) },
    '@/store/modules/user': {
      useUserStore: () => ({ resetState: () => calls.push(['reset-user']) }),
    },
    '@/utils/auth': {
      setTenantId: id => calls.push(['set-tenant', id]),
      setToken: token => tokens.push(token),
    },
    '@/utils/PasswordEncryptionUtil': {
      isSupportedAlgorithm: value => ['SM2', 'SM4', 'NONE'].includes(value),
      encrypt: (password, algorithm, key) => {
        calls.push(['encrypt', algorithm, key])
        return `encrypted:${password}`
      },
    },
    '@/views/agent/ContentEntryAuth.vue': { default: {} },
    './loginRedirect': { loginRedirectPath },
  }
  const exports = {}
  vm.runInNewContext(script, {
    exports,
    __env: {
      VITE_APP_CAPTCHA_ENABLE: String(captcha),
      VITE_APP_TENANT_ENABLE: String(tenant),
      VITE_APP_DEFAULT_LOGIN_TENANT: '测试租户',
    },
    URL,
    window: {
      location: { origin: 'https://app.example.test', assign: url => navigation.push(url) },
    },
    require: name => {
      assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`)
      return dependencies[name]
    },
  })
  const state = exports.default.setup({}, { expose() {} })
  state.form.username = 'test-user'
  state.form.password = 'test-password'
  state.formRef.value = { validate: async () => true }
  state.verifyRef.value = { show: () => calls.push(['show-captcha']) }
  return { state, calls, tokens, navigation }
}

test('login redirects preserve local destinations and encoded query values', () => {
  assert.equal(
    loginRedirectPath('/agent/content/listing?next=a%2Fb#draft'),
    '/agent/content/listing?next=a%2Fb#draft',
  )
  assert.equal(loginRedirectPath('/sso?client_id=test'), '/sso?client_id=test')
})
test('login redirects reject external URLs, parser tricks and login loops', () => {
  for (const target of [
    undefined,
    ['//other.test'],
    'https://other.test',
    '//other.test',
    '/\\other.test',
    '/%2fother.test',
    '/%5Cother.test',
    '/\n/other.test',
    '/login?redirect=/login',
  ]) {
    assert.equal(loginRedirectPath(target), '/', String(target))
  }
})
test('password login obtains encryption settings and saves only the server session', async () => {
  const f = fixture({ redirect: '/agent/content/promotion' })
  await f.state.beginLogin()
  const data = f.calls.find(call => call[0] === 'login')[1]
  assert.equal(data.password, 'encrypted:test-password')
  assert.equal(data.passwordTransmissionMode, 'SM2')
  assert.equal(data.captchaVerification, '')
  assert.equal(f.tokens.length, 1)
  assert.deepEqual(f.navigation, ['/agent/content/promotion'])
  assert.equal(f.state.form.password, '')
  assert.ok(f.calls.some(call => call[0] === 'clear-user-cache'))
  assert.ok(f.calls.some(call => call[0] === 'reset-user'))
})
test('server rejection never creates a local session', async () => {
  const f = fixture({
    api: {
      async login() {
        throw new Error('服务拒绝登录')
      },
    },
  })
  await f.state.beginLogin()
  assert.equal(f.tokens.length, 0)
  assert.equal(f.navigation.length, 0)
  assert.equal(f.state.loading.value, false)
})
test('invalid encryption settings do not fall back to sending a password', async () => {
  const f = fixture({
    api: {
      async getPublicKey() {
        return { algorithm: 'UNKNOWN', key: '' }
      },
    },
  })
  await f.state.beginLogin()
  assert.equal(
    f.calls.some(call => call[0] === 'login'),
    false,
  )
  assert.equal(f.tokens.length, 0)
})
test('incomplete token response does not authenticate the user', async () => {
  const f = fixture({
    api: {
      async login() {
        return { accessToken: 'test-access' }
      },
    },
  })
  await f.state.beginLogin()
  assert.equal(f.tokens.length, 0)
  assert.equal(f.navigation.length, 0)
})
test('captcha-enabled login requires proof and prepares tenant before showing the challenge', async () => {
  const f = fixture({ captcha: true, tenant: true })
  await f.state.finishLogin()
  assert.equal(f.calls.length, 0)
  await f.state.beginLogin()
  assert.deepEqual(
    f.calls.map(call => call[0]),
    ['tenant', 'set-tenant', 'show-captcha'],
  )
  assert.equal(f.tokens.length, 0)
  await f.state.onVerified({ captchaVerification: 'test-captcha-proof' })
  assert.equal(
    f.calls.find(call => call[0] === 'login')[1].captchaVerification,
    'test-captcha-proof',
  )
  assert.equal(f.tokens.length, 1)
})
test('unknown tenant prevents login', async () => {
  const f = fixture({
    tenant: true,
    api: {
      async getTenantIdByName() {
        return null
      },
    },
  })
  await f.state.beginLogin()
  assert.equal(
    f.calls.some(call => call[0] === 'login'),
    false,
  )
  assert.equal(f.tokens.length, 0)
})
test('Entry SSO mode never invokes legacy password or enterprise login', async () => {
  const f = fixture({ entry: true })
  await f.state.beginLogin()
  await f.state.finishLogin('proof')
  await f.state.enterpriseLogin()
  assert.equal(f.calls.length, 0)
  assert.equal(f.tokens.length, 0)
  assert.equal(f.navigation.length, 0)
})
test('enterprise login follows the backend authorization URL and rejects script URLs', async () => {
  const f = fixture()
  await f.state.enterpriseLogin()
  assert.deepEqual(f.navigation, ['https://login.example.test/authorize'])
  assert.equal(
    f.calls.find(call => call[0] === 'authorize')[1],
    'https://app.example.test/content/login?type=30&agentid=test-agent',
  )
  const invalid = fixture({
    api: {
      async getAuthorizeUrl() {
        return 'javascript:alert(1)'
      },
    },
  })
  await invalid.state.enterpriseLogin()
  assert.equal(invalid.navigation.length, 0)
  assert.equal(invalid.tokens.length, 0)
})

function legacyGuardFixture({ userFailure = false, standalone = false } = {}) {
  let authenticated = false
  let removedTokens = 0
  let removedCache = 0
  const user = {
    getIsSetUser: false,
    async setUserInfoAction() {
      if (userFailure) throw new Error('Session rejected')
      user.getIsSetUser = true
    },
  }
  const dict = { getIsSetDict: true }
  const permission = {
    getRouters: [],
    getAddRouters: [{ path: '/agent/content/listing', name: 'Listing', component: {} }],
    async generateRoutes() {},
  }
  const router = createRouter({
    history: createMemoryHistory('/content/'),
    routes: [
      { path: '/', name: 'Home', component: {} },
      { path: '/login', name: 'Login', component: {} },
      { path: '/agent/content', name: 'ContentHome', component: {} },
      { path: '/agent/content/services', name: 'ContentServices', component: {} },
      { path: '/:pathMatch(.*)*', name: 'Fallback', component: {} },
    ],
  })
  const dependencies = {
    './router': { __esModule: true, default: router },
    '@/auth/entrySession': { isEntrySso: false, entryAuthError: vue.ref('') },
    '@/auth/workspaceMode': { isStandaloneMode: standalone },
    '@/config/axios/service': { isRelogin: {} },
    '@/hooks/web/useCache': { deleteUserCache: () => removedCache++ },
    '@/hooks/web/useNProgress': { useNProgress: () => ({ start() {}, done() {} }) },
    '@/hooks/web/usePageLoading': { usePageLoading: () => ({ loadStart() {}, loadDone() {} }) },
    '@/hooks/web/useTitle': { useTitle() {} },
    '@/store/modules/dict': { useDictStoreWithOut: () => dict },
    '@/store/modules/user': { useUserStoreWithOut: () => user },
    '@/store/modules/permission': { usePermissionStoreWithOut: () => permission },
    '@/utils/auth': {
      getAccessToken: () => (authenticated ? 'test-access' : undefined),
      removeToken: () => {
        authenticated = false
        removedTokens++
      },
    },
  }
  const code = ts.transpileModule(
    readFileSync(resolve('src/permission.ts'), 'utf8').replaceAll('import.meta.env', '__env'),
    {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    },
  ).outputText
  vm.runInNewContext(code, {
    exports: {},
    __env: { VITE_AGENT_DEV_MODE: 'false' },
    console: { info() {}, error() {} },
    require: name => {
      assert.ok(Object.hasOwn(dependencies, name), `Unexpected guard dependency: ${name}`)
      return dependencies[name]
    },
  })
  return {
    router,
    authenticate: () => {
      authenticated = true
    },
    clearedTokens: () => removedTokens,
    clearedCache: () => removedCache,
  }
}

test('actual legacy guard preserves deep links through login and dynamic route registration', async () => {
  const f = legacyGuardFixture()
  const destination =
    '/agent/content/listing?discount=50%25&nested=%2Fdocs%3Ffilter%3Da%26page%3D2&tag=one&tag=two#draft'
  await f.router.push(destination)
  assert.equal(f.router.currentRoute.value.path, '/login')
  assert.equal(f.router.currentRoute.value.query.redirect, destination)
  f.authenticate()
  await f.router.replace(loginRedirectPath(f.router.currentRoute.value.query.redirect))
  assert.equal(f.router.currentRoute.value.path, '/agent/content/listing')
  assert.equal(f.router.currentRoute.value.query.discount, '50%')
  assert.equal(f.router.currentRoute.value.name, 'Listing')
  assert.deepEqual(f.router.currentRoute.value.query.tag, ['one', 'two'])
  assert.equal(f.router.currentRoute.value.query.nested, '/docs?filter=a&page=2')
  assert.equal(f.router.currentRoute.value.hash, '#draft')
  assert.equal(f.clearedTokens(), 0)
  assert.equal(f.clearedCache(), 0)
})

test('actual legacy guard does not decode literal percent signs or remove a valid session', async () => {
  const f = legacyGuardFixture()
  const destination = '/agent/content/listing?discount=50%#item'
  await f.router.push(destination)
  f.authenticate()
  await f.router.replace(loginRedirectPath(f.router.currentRoute.value.query.redirect))
  assert.equal(f.router.currentRoute.value.path, '/agent/content/listing')
  assert.equal(f.router.currentRoute.value.query.discount, '50%')
  assert.equal(f.router.currentRoute.value.hash, '#item')
  assert.equal(f.router.currentRoute.value.name, 'Listing')
  assert.equal(f.clearedTokens(), 0)
})

test('actual legacy guard never restores a rejected raw login redirect', async () => {
  const f = legacyGuardFixture()
  await f.router.push({ path: '/login', query: { redirect: '//other.example.test' } })
  f.authenticate()
  await f.router.replace(loginRedirectPath(f.router.currentRoute.value.query.redirect))
  assert.equal(f.router.currentRoute.value.fullPath, '/')
  assert.equal(f.clearedTokens(), 0)
})

test('actual legacy guard keeps the complete return path when session validation fails', async () => {
  const f = legacyGuardFixture({ userFailure: true })
  f.authenticate()
  const destination = '/agent/content/listing?a=1&nested=%2Fx%3Fq%3D50%25#draft'
  await f.router.push(destination)
  assert.equal(f.router.currentRoute.value.path, '/login')
  assert.equal(f.router.currentRoute.value.query.redirect, destination)
  assert.equal(f.clearedTokens(), 1)
  assert.equal(f.clearedCache(), 1)
})

test('standalone workspace opens model settings without a token or enterprise user lookup', async () => {
  const f = legacyGuardFixture({ standalone: true, userFailure: true })
  await f.router.push('/agent/content/services?capability=text#models')
  assert.equal(f.router.currentRoute.value.name, 'ContentServices')
  assert.equal(f.router.currentRoute.value.query.capability, 'text')
  assert.equal(f.router.currentRoute.value.hash, '#models')
  assert.equal(f.clearedTokens(), 0)
  assert.equal(f.clearedCache(), 0)
})

test('standalone login and account bookmarks return directly to the workspace', async () => {
  const f = legacyGuardFixture({ standalone: true, userFailure: true })
  for (const path of ['/login?redirect=https%3A%2F%2Fother.test', '/entry-auth', '/user/profile', '/']) {
    await f.router.push(path)
    assert.equal(f.router.currentRoute.value.fullPath, '/agent/content')
  }
  assert.equal(f.clearedTokens(), 0)
})
