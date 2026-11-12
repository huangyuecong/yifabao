<script lang="ts" setup>
import type { RouteRecordRaw } from 'vue-router'
import { ElLoading } from 'element-plus'
import * as LoginApi from '@/api/login'
import { isEntrySso } from '@/auth/entrySession'
import { isStandaloneMode } from '@/auth/workspaceMode'
import routerSearch from '@/components/RouterSearch/index.vue'
import { CACHE_KEY, useCache } from '@/hooks/web/useCache'
import { useDesign } from '@/hooks/web/useDesign'
import { useAppStore } from '@/store/modules/app'
import { usePermissionStoreWithOut } from '@/store/modules/permission'
import { useUserStore } from '@/store/modules/user'
import * as authUtil from '@/utils/auth'
import { isDark } from '@/utils/is'
// import { integer } from 'vue-types'
import router from './router'
import { useDictStore } from './store/modules/dict'

defineOptions({ name: 'APP' })

const { dictMap } = useDictStore()

provide('dictMap', dictMap)

const { push } = useRouter()

const { getPrefixCls } = useDesign()
const prefixCls = getPrefixCls('app')
const appStore = useAppStore()
const currentSize = computed(() => appStore.getCurrentSize)
const greyMode = computed(() => appStore.getGreyMode)
const { wsCache } = useCache()
const isAgentDevMode = import.meta.env.VITE_AGENT_DEV_MODE === 'true'

// 根据浏览器当前主题设置系统主题色
const setDefaultTheme = () => {
  let isDarkTheme = wsCache.get(CACHE_KEY.IS_DARK)
  if (isDarkTheme === null) {
    isDarkTheme = isDark()
  }
  appStore.setIsDark(isDarkTheme)
}
setDefaultTheme()

const getTenantId = async () => {
  const res = await LoginApi.getTenantIdByName('雅酿') // 目前写死，之后可能根据情况写为动态的
  authUtil.setTenantId(res)
}

const loading = ref()
async function wxLogin() {
  await getTenantId()
  const separators = /[?&]/
  const urlParam = window.location.search.split(separators)
  let system
  let ssotoken
  let code
  let redirectUrl
  let applicationId
  let oaApplicationId
  let urlIndex // 重定向url所在的位置

  for (const [index, param] of urlParam.entries()) {
    if (param.startsWith('ssotoken')) {
      const temp = param.split('=')[1]
      ssotoken = temp.split('&')[0]
      break
    }
    if (param.startsWith('code')) {
      const temp = param.split('=')[1]
      code = temp.split('&')[0]
    }
    if (param.startsWith('oaApplicationId')) {
      oaApplicationId = param.split('=')[1]
    }
    if (param.startsWith('system')) {
      system = param.split('=')[1]
    }
    if (param.startsWith('agentid') || param.startsWith('applicationId')) {
      applicationId = param.split('=')[1]
    }

    if (param.startsWith('redirect')) {
      urlIndex = index
      redirectUrl = `${param.split('=')[1]}`
    }
  }

  const query = {}
  for (let i = urlIndex + 1; i < urlParam.length; i += 1) {
    const [name, value] = urlParam[i].split('=')
    query[name] = value
  }

  let res
  if (code && applicationId) {
    res = await LoginApi.wxlogin(code, applicationId)
  }
  if (ssotoken) {
    res = await LoginApi.oalogin(ssotoken, oaApplicationId)
  }
  if (!res) {
    return
  }

  loading.value = ElLoading.service({
    lock: true,
    text: '正在加载系统中...',
    background: 'rgba(0, 0, 0, 0.7)',
  })
  const existKey = authUtil.getAccessToken()
  //  OA或企微登录时每次都获取最新的菜单数据
  authUtil.setToken(res)
  const userStore = useUserStore()
  const permissionStore = usePermissionStoreWithOut()

  await userStore.setUserInfoAction(true, system)
  await permissionStore.generateRoutes()
  permissionStore.getAddRouters.forEach((route) => {
    console.log(route)
    if (!route.path.startsWith('/')) {
      route.path = `/${route.path}`
    }
    router.addRoute(route as unknown as RouteRecordRaw) // 动态添加可访问路由表
  })
  if (redirectUrl && redirectUrl !== '') {
    push({ path: redirectUrl, query })
  }
  else if (!existKey) {
    push({ path: '/', query })
  }

  loading.value.close()
}
// 本地智能体联调不请求公司的租户和单点登录接口，避免无令牌时触发全局 401 弹窗。
// 超级入口构建由路由守卫验证 Session；其他非开发构建保留原企微/OA 流程。
if (!isStandaloneMode && !isAgentDevMode && !isEntrySso) {
  wxLogin()
}
const route = useRoute()

// 将不显示头部和底部的页面的地址写死，之后应和后端协调，为菜单信息添加配置选项并执行对应操作
const pagesWithoutHeaderAndFooter = ['/msls/strain-info-h5', '/msls/strain-info-detail-h5']
const setHeaderAndFooter = (route) => {
  let flag = true
  pagesWithoutHeaderAndFooter.forEach((item) => {
    if (route.fullPath.startsWith(item)) {
      flag = false
    }
  })

  appStore.setShowHeader(flag)
  appStore.setFooter(flag)
}
setHeaderAndFooter(route)
watch(route, () => {
  setHeaderAndFooter(route)
})

const keepAlivePages = ['MobileIndex']
</script>

<template>
  <ConfigGlobal :size="currentSize">
    <RouterView v-slot="{ Component }">
      <keep-alive :include="keepAlivePages">
        <component :is="Component" :class="greyMode ? `${prefixCls}-grey-mode` : ''" />
      </keep-alive>
    </RouterView>
    <routerSearch v-if="!isStandaloneMode" />
  </ConfigGlobal>
</template>

<style lang="scss">
$prefix-cls: #{$namespace}-app;

.size {
  width: 100%;
  height: 100%;
}

html,
body {
  @extend .size;

  padding: 0 !important;
  margin: 0;
  overflow: hidden;
  #app {
    @extend .size;
  }
}

.#{$prefix-cls}-grey-mode {
  filter: grayscale(100%);
}
</style>
