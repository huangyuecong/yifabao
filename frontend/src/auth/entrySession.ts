import type { AuthState } from '../../packages/platform-auth/src/models/session'
import { readonly, ref } from 'vue'
import { ApiRequestError, createPlatformClient } from '../../packages/platform-auth/src/index'

export const isEntrySso = import.meta.env.VITE_CONTENT_AUTH_MODE === 'entry'

export interface ContentIdentity {
  subject: string
  display_name: string
  department: string | null
  roles: string[]
  clearance: string
}

const identity = ref<ContentIdentity | null>(null)
export const entryIdentity = readonly(identity)
const error = ref('')
export const entryAuthError = readonly(error)
let client: ReturnType<typeof createPlatformClient> | undefined
let pending: Promise<boolean> | undefined
let redirecting = false
const loginRetryKey = 'content-entry-login-retry'
const loginRetryWindowMs = 2 * 60 * 1000

function allowSessionLoginRetry(subject: string) {
  try {
    const stored = window.sessionStorage.getItem(loginRetryKey)
    let previous: { subject?: string, started_at?: number } | undefined
    try {
      previous = stored ? JSON.parse(stored) : undefined
    }
    catch {}
    const now = Date.now()
    if (previous?.subject === subject && typeof previous.started_at === 'number'
      && Number.isFinite(previous.started_at) && now - previous.started_at < loginRetryWindowMs) {
      return false
    }
    // Survive the round trip through Entry without putting identity or tokens
    // into the return URL. A persistent failure must not cause a login loop.
    window.sessionStorage.setItem(loginRetryKey, JSON.stringify({ subject, started_at: now }))
    return true
  }
  catch {
    // If browser storage is unavailable, show the retry page instead of
    // redirecting with no way to remember the previous attempt.
    return false
  }
}

function clearSessionLoginRetry() {
  try {
    window.sessionStorage.removeItem(loginRetryKey)
  }
  catch {}
}

function entryClient() {
  client ??= createPlatformClient()
  return client
}

async function sessionRequest<T>(path: string) {
  // Keep the Chrome 89 build target compatible; AbortSignal.timeout is newer.
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 10000)
  try {
    return await entryClient().http.requestJson<T>(path, { signal: controller.signal, cache: 'no-store' })
  }
  finally {
    clearTimeout(timer)
  }
}

export function entryReturnTo(value = window.location.pathname + window.location.search + window.location.hash) {
  const base = import.meta.env.BASE_URL
  const fallback = base.endsWith('/') ? base : `${base}/`
  try {
    const target = new URL(value, window.location.origin)
    if (target.origin === window.location.origin && target.pathname.startsWith(fallback)
      && !target.pathname.startsWith(`${fallback}entry-auth`)) {
      return `${target.pathname}${target.search}${target.hash}`
    }
  }
  catch {}
  return fallback
}

export function loginAtEntry(returnTo?: string) {
  if (!redirecting) {
    redirecting = true
    entryClient().auth.login(entryReturnTo(returnTo))
  }
}

export async function logoutFromEntry() {
  await entryClient().auth.logout()
  identity.value = null
  clearSessionLoginRetry()
  window.location.assign('/')
}

async function checkSession(returnTo: string) {
  error.value = ''
  try {
    // auth.me() treats every HTTP failure as logged out. Use the SDK's strict
    // transport so an Entry outage does not create an endless SSO redirect.
    let state: AuthState
    try {
      state = await sessionRequest<AuthState>('/api/auth/me')
    }
    catch (reason) {
      if (reason instanceof ApiRequestError && reason.status === 401) {
        identity.value = null
        loginAtEntry(returnTo)
        return false
      }
      throw reason
    }
    if (!state.authenticated || !state.user?.subject) {
      identity.value = null
      loginAtEntry(returnTo)
      return false
    }
    let verified: ContentIdentity
    try {
      verified = await sessionRequest<ContentIdentity>(
        `${import.meta.env.VITE_CONTENT_API_URL || '/content/api'}/v1/identity`,
      )
    }
    catch (reason) {
      if (reason instanceof ApiRequestError && reason.status === 401
        && allowSessionLoginRetry(state.user.subject)) {
        identity.value = null
        loginAtEntry(returnTo)
        return false
      }
      throw reason
    }
    if (verified.subject !== state.user.subject || !Array.isArray(verified.roles)) {
      throw new Error('当前用户身份未正确同步，请联系管理员检查统一登录配置')
    }
    clearSessionLoginRetry()
    if (identity.value && identity.value.subject !== verified.subject) {
      identity.value = null
      window.location.replace(entryReturnTo(returnTo))
      return false
    }
    identity.value = verified
    return true
  }
  catch (reason) {
    if (reason instanceof ApiRequestError) {
      error.value = reason.status === 403
        ? '当前账号无权访问内容智能体，请联系管理员开通权限'
        : reason.status === 401
          ? '内容智能体登录验证未通过，请重新验证；若仍失败，请联系管理员检查会话配置'
          : '统一登录服务暂时不可用，请稍后重试'
    }
    else {
      error.value = reason instanceof Error ? reason.message : '无法验证当前登录状态，请重试'
    }
    identity.value = null
    return false
  }
}

export function ensureEntrySession(returnTo: string) {
  pending ??= checkSession(entryReturnTo(returnTo)).finally(() => {
    pending = undefined
  })
  return pending
}

export function reportEntryUnauthorized() {
  if (isEntrySso) {
    window.dispatchEvent(new Event('content-entry-unauthorized'))
  }
}
