<script lang="ts" setup>
import { CACHE_KEY, useCache } from '@/hooks/web/useCache'
import FramePage from '@/layout/components/Frame/index.vue'
import { useUserStore } from '@/store/modules/user'
import { getAccessToken } from '@/utils/auth'
import * as authUtil from '@/utils/auth'
import { computed, defineOptions, ref, unref, watch } from '@/vue-export'
import { useRouter } from 'vue-router'
import { useFrameKeepAlive } from './useFrameKeepAlive'

defineOptions({ name: 'FrameLayout' })
const userStore = useUserStore()
const { wsCache } = useCache()
const router = useRouter()
const { currentRoute } = router
const { getFramePages, hasRenderFrame, showIframe, getFrameSrcByName, setFrameSrcByName }
  = useFrameKeepAlive()

const iframeRefList = ref<any>([])
const showFrame = computed(() => unref(getFramePages).length > 0)
const frameSrc = computed(() => {
  return ({ frameSrc: originFrameSrc }) => {
    originFrameSrc ||= ''

    originFrameSrc = originFrameSrc.trim()

    const cacheFrameSrc = getFrameSrcByName(originFrameSrc)
    if (cacheFrameSrc) {
      return cacheFrameSrc
    }

    let frameSrc = originFrameSrc

    const query = unref(currentRoute).query
    const length = Object.keys(query).length
    if (length > 0) {
      const entries = Object.entries(query)

      for (let i = 0; i < length; i++) {
        if (i === 0) {
          frameSrc += `?${entries[0][0]}=${entries[0][1]}`
        }
        else {
          frameSrc += `&${entries[i][0]}=${entries[i][1]}`
        }
      }
    }

    return setFrameSrcByName(originFrameSrc, frameSrc)
  }
})
function handleFrameMessage(message) {
  // 确保子系统已挂载好监听事件
  if (message === 'Ready') {
    connectSystem(unref(currentRoute))
  }
}
function connectSystem(route) {
  for (const iframe of unref(iframeRefList)) {
    if (!iframe) {
      continue
    }
    const iframeSrc = route.meta.frameSrc

    if (iframeSrc) {
      const root = `/${route.path.split('/')[1]}`

      const menus = wsCache.get(CACHE_KEY.ROLE_ROUTERS) as AppCustomRouteRecordRaw[]
      const childMenu = menus.filter((item) => {
        return item.path === root
      })

      if (wsCache.get(CACHE_KEY.USER)) {
        const userInfo = wsCache.get(CACHE_KEY.USER)
        userInfo.menus = childMenu

        const token = getAccessToken()

        let path = route.path
        const query = unref(currentRoute).query
        const length = Object.keys(query).length
        if (length > 0) {
          const entries = Object.entries(query)

          for (let i = 0; i < length; i++) {
            if (i === 0) {
              path += `?${entries[0][0]}=${entries[0][1]}`
            }
            else {
              path += `&${entries[i][0]}=${entries[i][1]}`
            }
          }
        }

        iframe.postMessage(
          {
            path,
            token,
            userInfo,
            refreshToken: authUtil.getRefreshToken(),
          },
          iframeSrc,
        )
      }
    }

    return
  }
}

watch(unref(iframeRefList), (refs) => {
  userStore.setIframeRefs(refs)
})
</script>

<template>
  <div v-if="showFrame">
    <template v-for="frame in getFramePages" :key="frame.path">
      <FramePage
        v-if="frame.meta.frameSrc && hasRenderFrame(frame.name)"
        v-show="showIframe(frame)"
        ref="iframeRefList"
        :frame-src="frameSrc(frame.meta)"
        @message="handleFrameMessage"
      />
    </template>
  </div>
</template>
