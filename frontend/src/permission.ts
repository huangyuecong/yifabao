import type { RouteRecordRaw } from 'vue-router'
import { ensureEntrySession, entryAuthError, isEntrySso } from '@/auth/entrySession'
import { isStandaloneMode } from '@/auth/workspaceMode'
import { isRelogin } from '@/config/axios/service'
import { deleteUserCache } from '@/hooks/web/useCache'
import { useNProgress } from '@/hooks/web/useNProgress'
import { usePageLoading } from '@/hooks/web/usePageLoading'
import { useTitle } from '@/hooks/web/useTitle'
import { useDictStoreWithOut } from '@/store/modules/dict'
import { usePermissionStoreWithOut } from '@/store/modules/permission'
import { useUserStoreWithOut } from '@/store/modules/user'
import { getAccessToken, removeToken } from '@/utils/auth'
import router from './router'

const { start, done } = useNProgress()

const { loadStart, loadDone } = usePageLoading()

// 路由不重定向白名单
const whiteList = [
  '/login',
  '/social-login',
  '/auth-redirect',
  '/bind',
  '/register',
  '/oauthLogin/gitee',
]

const isAgentDevRoute = (path: string) =>
  import.meta.env.VITE_AGENT_DEV_MODE === 'true' && path.startsWith('/agent')

// 路由加载前
router.beforeEach(async (to, _from, next) => {
  start()
  loadStart()
  if (isStandaloneMode) {
    const permissionStore = usePermissionStoreWithOut()
    if (!permissionStore.getRouters.length) {
      await permissionStore.generateRoutes()
    }
    // Login/profile bookmarks return to the local workspace. No company login
    // or cached browser token participates in standalone navigation.
    const contentRoute = to.path === '/agent/content' || to.path.startsWith('/agent/content/')
    next(contentRoute ? undefined : { path: '/agent/content', replace: true })
    return
  }
  if (isEntrySso) {
    if (to.name === 'ContentEntryAuth') {
      next()
      return
    }
    const returnTo = new URL(router.resolve(to).href, window.location.origin).pathname + to.fullPath.slice(to.path.length)
    if (await ensureEntrySession(returnTo)) {
      const permissionStore = usePermissionStoreWithOut()
      if (!permissionStore.getRouters.length) {
        await permissionStore.generateRoutes()
      }
      if (!to.path.startsWith('/agent/content')) {
        next('/agent/content')
      }
      else {
        next()
      }
    }
    else if (entryAuthError.value) {
      next({ name: 'ContentEntryAuth', query: { return_to: returnTo } })
    }
    else {
      next(false)
    }
    done()
    loadDone()
    return
  }
  // 仅本地联调时允许内容智能体页面绕过公司统一登录。
  // 生产构建默认关闭，身份必须由超级入口或可信网关提供。
  if (isAgentDevRoute(to.path)) {
    const permissionStore = usePermissionStoreWithOut()
    if (!permissionStore.getRouters.length) {
      await permissionStore.generateRoutes()
    }
    next()
    return
  }
  if (getAccessToken()) {
    if (to.path === '/login') {
      next({ path: '/' })
    }
    else {
      // 获取所有字典
      const dictStore = useDictStoreWithOut()
      const userStore = useUserStoreWithOut()
      const permissionStore = usePermissionStoreWithOut()
      if (!dictStore.getIsSetDict) {
        await dictStore.setDictMap()
      }
      if (!userStore.getIsSetUser) {
        isRelogin.show = true
        try {
          await userStore.setUserInfoAction()
          // 后端过滤菜单
          await permissionStore.generateRoutes()
          console.info('route', permissionStore.getAddRouters)
          permissionStore.getAddRouters.forEach((route) => {
            if (!route.path.startsWith('/')) {
              route.path = `/${route.path}`
            }

            router.addRoute(route as unknown as RouteRecordRaw) // 动态添加可访问路由表
          })
          // Re-resolve the destination after registering routes. Login already
          // validated it; reading or decoding the previous query would replace it.
          next({ path: to.path, query: to.query, hash: to.hash, replace: true })
        }
        catch (e) {
          // 令牌已失效（此时 isRelogin.show 为 true，service 不会弹重新登录框，异常会直接抛到这里）。
          // 必须清掉本地令牌和用户缓存，否则每次刷新都会重复失败，页面停在白屏且无法自愈。
          console.error('[获取用户信息失败]', e)
          removeToken()
          deleteUserCache()
          next({ path: '/login', query: { redirect: to.fullPath } })
        }
        finally {
          isRelogin.show = false
        }
      }
      else {
        next()
      }
    }
  }
  else {
    if (whiteList.includes(to.path)) {
      next()
    }
    else {
      next({ path: '/login', query: { redirect: to.fullPath } }) // 否则全部重定向到登录页
    }
  }
})

router.afterEach((to) => {
  useTitle(to?.meta?.title as string)
  done() // 结束Progress
  loadDone()
})

// Downloads and background API calls share this flow; 403 remains a business
// permission error and never initiates another login.
if (isEntrySso) {
  window.addEventListener('content-entry-unauthorized', async () => {
    if (router.currentRoute.value.name === 'ContentEntryAuth') {
      return
    }
    const returnTo = window.location.pathname + window.location.search + window.location.hash
    if (!(await ensureEntrySession(returnTo)) && entryAuthError.value) {
      void router.push({ name: 'ContentEntryAuth', query: { return_to: returnTo } })
    }
  })
}
