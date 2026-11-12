import { cloneDeep } from 'lodash-es'
import { defineStore } from 'pinia'
import { isEntrySso } from '@/auth/entrySession'
import { isStandaloneMode } from '@/auth/workspaceMode'
import { CACHE_KEY, useCache } from '@/hooks/web/useCache'
import remainingRouter from '@/router/modules/remaining'
import { store } from '@/store'
import { flatMultiLevelRoutes, generateRoute } from '@/utils/routerHelper'

const { wsCache } = useCache()

export interface PermissionState {
  routers: AppRouteRecordRaw[]
  addRouters: AppRouteRecordRaw[]
  menuTabRouters: AppRouteRecordRaw[]
}

export const usePermissionStore = defineStore('permission', {
  state: (): PermissionState => ({
    routers: [],
    addRouters: [],
    menuTabRouters: [],
  }),
  getters: {
    getRouters(): AppRouteRecordRaw[] {
      return this.routers
    },
    getAddRouters(): AppRouteRecordRaw[] {
      return flatMultiLevelRoutes(cloneDeep(this.addRouters))
    },
    getMenuTabRouters(): AppRouteRecordRaw[] {
      return this.menuTabRouters
    },
  },
  actions: {
    async generateRoutes(): Promise<unknown> {
      if (isStandaloneMode || isEntrySso) {
        this.addRouters = []
        this.routers = cloneDeep(remainingRouter.filter(route => route.name === 'AgentWorkbench'))
        return
      }
      return new Promise<void>((resolve) => {
        // 获得菜单列表，它在登录的时候，setUserInfoAction 方法中已经进行获取
        let res: AppCustomRouteRecordRaw[] = []
        if (wsCache.get(CACHE_KEY.ROLE_ROUTERS)) {
          res = wsCache.get(CACHE_KEY.ROLE_ROUTERS) as AppCustomRouteRecordRaw[]
        }
        // console.log(res)
        const routerMap: AppRouteRecordRaw[] = generateRoute(res)
        // 动态路由，404一定要放到最后面
        // preschooler：vue-router@4以后已支持静态404路由，此处可不再追加
        let filteredRouterMap: AppRouteRecordRaw[] = []
        // 过滤后、未平铺的路由，用于 addRouters（保留 Layout 包装）
        let addRouterMap: AppRouteRecordRaw[] = []
        if (import.meta.env.VITE_APP_NAME) {
          addRouterMap = routerMap.filter(item => item.path.startsWith(import.meta.env.VITE_APP_NAME))
          filteredRouterMap = cloneDeep(addRouterMap)
          // 如果只有一个路由，直接将其子路由平铺（仅用于菜单渲染）
          if (filteredRouterMap.length === 1 && !window.__MICRO_APP_ENVIRONMENT__) {
            filteredRouterMap = filteredRouterMap[0].children || []
            filteredRouterMap.forEach((route) => {
              route.path = `${import.meta.env.VITE_APP_NAME}/${route.path}`
            })
          }
        }
        else {
          filteredRouterMap = routerMap
          addRouterMap = routerMap
        }

        this.addRouters = addRouterMap.concat([
          {
            path: '/:path(.*)*',
            // redirect: '/404',
            component: () => import('@/views/Error/404.vue'),
            name: '404Page',
            meta: {
              hidden: true,
              breadcrumb: false,
            },
          },
        ])
        // 渲染菜单的所有路由
        this.routers = cloneDeep(remainingRouter).concat(filteredRouterMap)
        resolve()
      })
    },
    setMenuTabRouters(routers: AppRouteRecordRaw[]): void {
      this.menuTabRouters = routers
    },
  },
  persist: false,
})

export const usePermissionStoreWithOut = () => {
  return usePermissionStore(store)
}
