import * as FileApi from '@/api/infra/file'
import { getInfo, loginOut, quickBILoginout } from '@/api/login'
import { CACHE_KEY, deleteUserCache, useCache } from '@/hooks/web/useCache'
import { store } from '@/store'
import { getAccessToken, removeToken } from '@/utils/auth'
import { defineStore } from 'pinia'

const { wsCache } = useCache()

interface UserVO {
  id: number
  avatar: string
  nickname: string
  deptId: number
}

interface UserInfoVO {
  // USER 缓存
  permissions: string[]
  roles: string[]
  menus: string[]
  isSetUser: boolean
  user: UserVO
  iframeRefs: any[]
  fileConfigId: string
}

export const useUserStore = defineStore('admin-user', {
  state: (): UserInfoVO => ({
    permissions: [],
    roles: [],
    menus: [],
    isSetUser: false,
    user: {
      id: 0,
      avatar: '',
      nickname: '',
      deptId: 0,
    },
    iframeRefs: [],
    fileConfigId: '',
  }),
  getters: {
    getPermissions(): string[] {
      return this.permissions
    },
    getRoles(): string[] {
      return this.roles
    },
    getMenus(): string[] {
      return this.menus
    },
    getIsSetUser(): boolean {
      return this.isSetUser
    },
    getUser(): UserVO {
      return this.user
    },
    getIframeRefs() {
      return this.iframeRefs
    },
    getFileConfigId(): string {
      return this.fileConfigId || wsCache.get(CACHE_KEY.FileConfigId)
    },
  },
  actions: {
    setFileConfigId(id) {
      this.fileConfigId = id
      wsCache.set(CACHE_KEY.FileConfigId, id)
    },
    setIframeRefs(refs) {
      this.iframeRefs = refs
    },
    async setUserInfoAction(isWxOrOa = false, system = '') {
      if (!getAccessToken()) {
        this.resetState()
        return null
      }
      FileApi.getConfigID().then((id) => {
        this.setFileConfigId(id)
      })
      let userInfo = wsCache.get(CACHE_KEY.USER)

      if (!userInfo || isWxOrOa) {
        userInfo = await getInfo()
      }

      this.permissions = userInfo.permissions
      this.roles = userInfo.roles
      this.menus = userInfo.menus
      this.user = userInfo.user
      this.isSetUser = true
      // 过滤非子应用的路由
      if (isWxOrOa && system) {
        userInfo.menus = userInfo.menus.filter(menu => menu.path === `/${system}`)
      }
      wsCache.set(CACHE_KEY.USER, userInfo)
      wsCache.set(CACHE_KEY.ROLE_ROUTERS, userInfo.menus)
    },
    async setUserAvatarAction(avatar: string) {
      const userInfo = wsCache.get(CACHE_KEY.USER)
      // NOTE: 是否需要像`setUserInfoAction`一样判断`userInfo != null`
      this.user.avatar = avatar
      userInfo.user.avatar = avatar
      wsCache.set(CACHE_KEY.USER, userInfo)
    },
    async setUserNicknameAction(nickname: string) {
      const userInfo = wsCache.get(CACHE_KEY.USER)
      // NOTE: 是否需要像`setUserInfoAction`一样判断`userInfo != null`
      this.user.nickname = nickname
      userInfo.user.nickname = nickname
      wsCache.set(CACHE_KEY.USER, userInfo)
    },
    async loginOut() {
      quickBILoginout()

      for (const iframe of unref(this.iframeRefs)) {
        if (!iframe) {
          continue
        }
        iframe.postMessage('loginOut', '*')
      }
      await loginOut()

      removeToken()
      deleteUserCache() // 删除用户缓存

      this.resetState()
      this.iframeRefs = []
    },

    resetState() {
      this.permissions = []
      this.roles = []
      this.menus = []
      this.isSetUser = false
      this.user = {
        id: 0,
        avatar: '',
        nickname: '',
        deptId: 0,
      }
    },
  },
})

export const useUserStoreWithOut = () => {
  return useUserStore(store)
}
