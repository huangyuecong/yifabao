<script setup>
import { hasPermi } from '@/directives/permission/hasPermi'
import { CACHE_KEY, useCache } from '@/hooks/web/useCache'
import { useDictStore } from '@/store/modules/dict'
import { useUserStore } from '@/store/modules/user'
import { getAccessToken, getRefreshToken } from '@/utils/auth'
import { onMounted, ref } from '@/vue-export'

import microApp from '@micro-zoe/micro-app'
import { useRoute } from 'vue-router'

const { wsCache } = useCache()
const userStore = useUserStore()
const route = useRoute()
const appUrl = ref('')

const appName = 'my-app'
const container = ref(null)
const getFullPath = () => {
  let fullPath = route.meta.microPath.replace(/\s/g, '')
  const query = {}
  for (const key in route.query) {
    if (key !== appName) {
      query[key] = route.query[key]
    }
  }
  let index = 0

  for (const key in query) {
    if (index === 0) {
      fullPath += `?${key}=${query[key]}`
    }
    else {
      fullPath += `&${key}=${query[key]}`
    }
    index += 1
  }

  return fullPath
}
const renderApp = () => {
  if (route.meta.origin.startsWith('/')) {
    appUrl.value = `${location.origin}${route.meta.origin}`
  }
  else {
    appUrl.value = route.meta.origin
  }

  if (window.__MICRO_APP_LOCATION_DEBUG_HOST) {
    appUrl.value = window.__MICRO_APP_LOCATION_DEBUG_HOST
  }
  const tokenInfo = {
    accessToken: getAccessToken(),
    refreshToken: getRefreshToken(),
  }
  microApp.setData(appName, { tokenInfo, userInfo: wsCache.get(CACHE_KEY.USER), userStore, hasPermi, getToken: getAccessToken, useUserStore, useDictStore })

  microApp
    .renderApp({ container: container.value, name: appName, url: appUrl.value, iframe: true })
    .then(() => {
      // micro-app首次加载路由
      // eslint-disable-next-line no-console
      console.log('渲染micro-app路由加载: ', appUrl.value, getFullPath())
      microApp.router.push({ name: appName, path: getFullPath() })
    })
}
onMounted(() => {
  renderApp()
  watch(
    () => route.path,
    (_newVal) => {
      console.log('micro-app跳转新路由: ', getFullPath())
      microApp.router.push({ name: appName, path: getFullPath() })
    },
  )
  // microApp.router.afterEach((to, from, appName) => {
  //   console.log('全局后置守卫 afterEach: ', to, from, appName)
  //   microApp.router.attachToURL(appName)
  // })
})
</script>

<template>
  <div id="microAppContainer" ref="container" />
</template>
