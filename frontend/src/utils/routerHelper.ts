import type { RouteLocationNormalized, Router, RouteRecordNormalized, RouteRecordRaw } from 'vue-router'
import { isUrl } from '@/utils/is'
import { cloneDeep, omit } from 'lodash-es'
import qs from 'qs'
import { createRouter, createWebHistory } from 'vue-router'

const modules = import.meta.glob('../views/**/*.{vue,tsx}')
/**
 * 注册一个异步组件
 * @param componentPath 例:/bpm/oa/leave/detail
 */
export const registerComponent = (componentPath: string) => {
  for (const item in modules) {
    if (item.includes(componentPath)) {
      // 使用异步组件的方式来动态加载组件
      // eslint-disable-next-line ts/ban-ts-comment
      // @ts-expect-error
      return defineAsyncComponent(modules[item])
    }
  }
}
/* Layout */
export const Layout = () => import('@/layout/Layout.vue')
// export const ModuleFederationComponent = () => import('@/layout/components/ModuleFederation/index.vue')
const MicroAppComp = () => import('@/layout/components/MicroApp/src/MicroAppComp.vue')
const IFRAME = () => import('@/layout/components/Frame/FrameBlank.vue')
export const getParentLayout = () => {
  return () =>
    new Promise((resolve) => {
      resolve({
        name: 'ParentLayout',
      })
    })
}

// 按照路由中meta下的rank等级升序来排序路由
export const ascending = (arr: any[]) => {
  arr.forEach((v) => {
    if (v?.meta?.rank === null) {
      v.meta.rank = undefined
    }
    if (v?.meta?.rank === 0) {
      if (v.name !== 'home' && v.path !== '/') {
        console.warn('rank only the home page can be 0')
      }
    }
  })
  return arr.sort((a: { meta: { rank: number } }, b: { meta: { rank: number } }) => {
    return a?.meta?.rank - b?.meta?.rank
  })
}

export const getRawRoute = (route: RouteLocationNormalized): RouteLocationNormalized => {
  if (!route) {
    return route
  }
  const { matched, ...opt } = route
  return {
    ...opt,
    matched: (matched
      ? matched.map(item => ({
          meta: item.meta,
          name: item.name,
          path: item.path,
        }))
      : undefined) as RouteRecordNormalized[],
  }
}
const toCamelCase = (str: string, upperCaseFirst: boolean) => {
  str = (str || '')
    .replace(/-(.)/g, (group1: string) => {
      return group1.toUpperCase()
    })
    .replaceAll('-', '')

  if (upperCaseFirst && str) {
    str = str.charAt(0).toUpperCase() + str.slice(1)
  }

  return str
}

// 后端控制路由生成
export const generateRoute = (routes: AppCustomRouteRecordRaw[]): AppRouteRecordRaw[] => {
  const res: AppRouteRecordRaw[] = []
  const modulesRoutesKeys = Object.keys(modules)
  for (const route of routes) {
    // 修复菜单类型为目录时pathType异常值
    if (route.children && route.children.length > 0) {
      route.pathType = 'component'
    }
    // 1. 生成 meta 菜单元数据
    const meta = {
      title: route.name,
      icon: route.icon,
      hidden: !route.visible,
      noCache: !route.keepAlive,
      alwaysShow:
        route.children
        && route.children.length === 1
        && (route.alwaysShow !== undefined ? route.alwaysShow : true),
    } as any
    // 特殊逻辑：如果后端配置的 MenuDO.component 包含 ?，则表示需要传递参数
    // 此时，我们需要解析参数，并且将参数放到 meta.query 中
    // 这样，后续在 Vue 文件中，可以通过 const { currentRoute } = useRouter() 中，通过 meta.query 获取到参数
    if (route.component && route.component.includes('?')) {
      const query = route.component.split('?')[1]
      route.component = route.component.split('?')[0]
      meta.query = qs.parse(query)
    }

    // 2. 生成 data（AppRouteRecordRaw）
    // 路由地址转首字母大写驼峰，作为路由名称，适配keepAlive
    const data: AppRouteRecordRaw = {
      path: route.path.includes('?') ? route.path.split('?')[0] : route.path,
      name:
        route.componentName && route.componentName.length > 0
          ? route.componentName
          : toCamelCase(route.path, true),
      redirect: route.redirect,
      meta,

    }
    // 处理内嵌iframe
    if (route.pathType === 'iframe' && route.path) {
      /**
       * 需要判断特殊情况  比如vue的hash是带#的
       * 比如链接 aaa.com/#/bbb  path会转换为 aaa/com/#/bbb
       * 比如链接 aaa.com/?bbb=xxx
       * 需要去除#  否则无法被添加到路由
       */
      if (route.path.includes('#')) {
        route.path = route.path.replace('#', '')
      }
      if (route.path.includes('?') || route.path.includes('&')) {
        route.path = route.path.replace('?', '')
        route.path = route.path.replace('&', '')
      }
      // 处理内嵌iframe是顶层目录的情况
      if (route.path.startsWith('/') && route.parentId === 0) {
        // route.path = route.path.slice(1)
        // data.path = '/'
        // /** 提取http(s):// 后的内容，用于设置name和path。（http开头会直接跳转到新链接） */
        // const regex = /^https?:\/\/(.*)$/
        // const match = route.path.match(regex)
        // let routePuerPath = ''
        // if (match) {
        //   routePuerPath = match[1]
        // }
        const { path, origin } = route

        data.component = Layout
        // 这里如果添加alwaysShow属性会报错
        data.meta = { title: route.name, icon: route.icon, hidden: !route.visible, noCache: !route.keepAlive }
        data.name = `${toCamelCase(path, true)}Parent`
        data.redirect = ''
        meta.alwaysShow = true
        const childrenData: AppRouteRecordRaw = {
          path: path.slice(1), // 这里path不能以/开头，会被框架识别为根路由，无法通过ifaeme组件打开
          name: route.componentName && route.componentName.length > 0
            ? route.componentName
            : toCamelCase(path, true),
          redirect: route.redirect,
          meta: { ...meta, pathType: route.pathType, frameSrc: origin + path },
        }
        childrenData.component = IFRAME
        data.children = [childrenData]
      }
      // 处理内嵌iframe是菜单的情况
      else {
        data.meta = { ...meta, pathType: route.pathType, origin: route.origin, pathname: route.pathname, path: route.path }
      }
    }
    // micro
    /* else if (route.pathType === 'micro') {

    } */
    // 外链
    else if (route.pathType === 'external') {
      data.pathType = 'external'
      if (!isUrl(data.path)) {
        data.path = window.location.origin + data.path
      }
    }
    // 处理模块联邦
    // else if (route.pathType === 'module_federation') {
    //   data.meta.origin = route.origin
    //   data.pathType = route.pathType
    //   data.meta.pathname = route.pathname
    //   data.component = ModuleFederationComponent
    //   data.name = `${route.pathType}${(route.pathname ?? '').replaceAll('/', '')}`
    // }
    else if (route.pathType === 'micro-app') {
      data.meta.origin = route.origin
      data.meta.appType = 'micro-app'
      data.pathType = route.pathType
      data.meta.pathname = route.pathname
      data.meta.urlPrefix = route.origin
      data.component = MicroAppComp
      data.name = `${route.pathType}${(route.pathname ?? '').replaceAll('/', '')}`
    }
    else if (route.pathType === 'QuicBi') {
      data.pathType = route.pathType
      data.meta.origin = route.origin
      data.meta.appType = 'QuicBi'
      data.meta.pathname = route.pathname
      data.meta.urlPrefix = route.origin
      data.component = MicroAppComp
      data.name = `${route.pathType}${(route.pathname ?? '').replaceAll('/', '')}`
    }
    // 处理顶级非目录路由
    else if (!route.children && route.parentId === 0 && route.component) {
      data.component = Layout
      data.meta = {}
      data.name = `${toCamelCase(route.path, true)}Parent`
      data.redirect = ''
      meta.alwaysShow = true
      const childrenData: AppRouteRecordRaw = {
        pathType: route.pathType,
        path: '',
        name:
          route.componentName && route.componentName.length > 0
            ? route.componentName
            : toCamelCase(route.path, true),
        redirect: route.redirect,
        meta,
      }
      const index = route?.component
        ? modulesRoutesKeys.findIndex(ev => ev.includes(route.component))
        : modulesRoutesKeys.findIndex(ev => ev.includes(route.path))
      childrenData.component = modules[modulesRoutesKeys[index]]
      data.children = [childrenData]
    }
    else {
      // 次级目录
      if (route.children) {
        data.component = Layout
        // eslint-disable-next-line ts/no-use-before-define
        data.redirect = getRedirect(route.path, route.children)
      }
      // 次级菜单
      else {
        // 对后端传component组件路径和不传做兼容（如果后端传component组件路径，那么path可以随便写，如果不传，component组件路径会根path保持一致）
        const index = route?.component
          ? modulesRoutesKeys.findIndex(ev => ev.includes(route.component))
          : modulesRoutesKeys.findIndex(ev => ev.includes(route.path))
        data.component = modules[modulesRoutesKeys[index]]
      }
      if (route.children) {
        data.children = generateRoute(route.children)
      }
    }
    res.push(data as AppRouteRecordRaw)
  }

  return res
}

export const getRedirect = (parentPath: string, children: AppCustomRouteRecordRaw[]) => {
  if (!children || children.length === 0) {
    return parentPath
  }
  // eslint-disable-next-line ts/no-use-before-define
  const path = generateRoutePath(parentPath, children[0].path)
  // 递归子节点
  if (children[0].children) {
    return getRedirect(path, children[0].children)
  }
}

const generateRoutePath = (parentPath: string, path: string) => {
  if (parentPath.endsWith('/')) {
    parentPath = parentPath.slice(0, -1) // 移除默认的 /
  }
  if (!path.startsWith('/')) {
    path = `/${path}`
  }
  return parentPath + path
}
export const pathResolve = (parentPath: string, path: string) => {
  if (isUrl(path)) {
    return path
  }

  const childPath = path.startsWith('/') || !path ? path : `/${path}`

  if (!isUrl(`${parentPath}${childPath}`)) {
    return `${parentPath}${childPath}`.replace(/\/\//g, '/')
  }
  return `${parentPath}${childPath}`
}

// 路由降级
const iframeSystems = ['/msls', '/ow', '/ow-ma'] // 为了提高性能,这里先指定低于2级路由的子系统路径
export const flatMultiLevelRoutes = (routes: AppRouteRecordRaw[]) => {
  const modules: AppRouteRecordRaw[] = cloneDeep(routes)
  for (let index = 0; index < modules.length; index++) {
    const route = modules[index]
    // eslint-disable-next-line ts/no-use-before-define
    if (!isMultipleRoute(route)) {
      const path = route.path
      if (iframeSystems.includes(path) && Array.isArray(route.children)) {
        for (const child of route.children) {
          if (child.meta?.appType === 'micro-app') {
            let { origin } = child.meta
            if (!origin) {
              origin = window.location.origin
            }
            child.meta.microPath = `${path}/${child.path}`
          }
          else {
            let { origin, path: childPath, pathname } = child.meta
            if (pathname) {
              pathname = pathname.replace(/\s+/g, '')
            }
            if (!origin) {
              origin = window.location.origin + pathname
            }
            child.meta.frameSrc = `${origin}/#${path}/${childPath}`
          }
        }
      }
    }
    else {
      if (route.path.startsWith('/')) {
        // eslint-disable-next-line ts/no-use-before-define
        promoteRouteLevel(route)
      }
      else {
        console.error('路径已非`/`开头', route)
      }
    }
  }
  return modules
}

// 层级是否大于2
const isMultipleRoute = (route: AppRouteRecordRaw) => {
  if (!route || !Reflect.has(route, 'children') || !route.children?.length) {
    return false
  }

  const children = route.children

  let flag = false
  for (let index = 0; index < children.length; index++) {
    const child = children[index]
    if (child.children?.length) {
      flag = true
      break
    }
  }
  return flag
}

// 生成二级路由
const promoteRouteLevel = (route: AppRouteRecordRaw) => {
  let router: Router | null = createRouter({
    routes: [route as RouteRecordRaw],
    history: createWebHistory(import.meta.env.VITE_BASE_PATH),
  })

  const routes = router.getRoutes()

  // eslint-disable-next-line ts/no-use-before-define
  addToChildren(routes, route.children || [], route)
  router = null

  route.children = route.children?.map(item => omit(item, 'children'))
}

// 添加所有子菜单
const addToChildren = (
  routes: RouteRecordNormalized[],
  children: AppRouteRecordRaw[],
  routeModule: AppRouteRecordRaw,
) => {
  for (let index = 0; index < children.length; index++) {
    const child = children[index]
    const route = routes.find(item => item.name === child.name)

    if (!route) {
      continue
    }
    if (route.meta.appType === 'micro-app') {
      const { path } = route
      route.meta.microPath = path
    }
    if (route.meta.pathType === 'iframe') {
      const { path } = route
      let { origin, pathname } = route.meta
      if (pathname) {
        pathname = pathname.replace(/\s+/g, '')
      }
      if (!origin) {
        origin = window.location.origin + pathname
      }
      route.meta.frameSrc = `${origin}/#${path}`
    }
    routeModule.children = routeModule.children || []
    if (!routeModule.children.find(item => item.name === route.name)) {
      routeModule.children?.push(route as unknown as AppRouteRecordRaw)
    }
    if (child.children?.length) {
      addToChildren(routes, child.children, routeModule)
    }
  }
}
