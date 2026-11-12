/**
 * 配置浏览器本地存储的方式，可直接存储对象数组。
 */

import WebStorageCache from 'web-storage-cache'

type CacheType = 'localStorage' | 'sessionStorage'

export const CACHE_KEY = {
  // 用户相关
  ROLE_ROUTERS: 'roleRouters',
  USER: 'user',
  // 系统设置
  IS_DARK: 'isDark',
  LANG: 'lang',
  THEME: 'theme',
  LAYOUT: 'layout',
  DICT_CACHE: 'dictCache',
  // 登录表单
  LoginForm: 'loginForm',
  TenantId: 'tenantId',
  FileConfigId: 'fileConfigId',
}



export const useCache = (type: CacheType = 'localStorage') => {

  const wsCache: WebStorageCache = new WebStorageCache({
    storage: type
  })
  function addPrefix(type) {
    const originMethod = wsCache[type]
    wsCache[type] = function (...arg) {
      arg[0] = import.meta.env.VITE_BASE_PATH.replace(/^\//, '') + '_' + arg[0]
      return originMethod.call(this, ...arg)
    }
  }

  addPrefix('set')
  addPrefix('get')
  addPrefix('delete')
  return {
    wsCache
  }
}

export const deleteUserCache = () => {
  const { wsCache } = useCache()
  wsCache.delete(CACHE_KEY.USER)
  wsCache.delete(CACHE_KEY.ROLE_ROUTERS)
  // 注意，不要清理 LoginForm 登录表单
}
