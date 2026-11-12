import { useAppStore } from '@/store/modules/app'
import { useTagsViewStoreWithOut } from '@/store/modules/tagsView'
import { computed, ref, toRaw, unref } from '@/vue-export'
import { uniqBy } from 'lodash-es'
import { useRouter } from 'vue-router'

const tagsViewStore = useTagsViewStoreWithOut()
const appStore = useAppStore()
const tagsView = ref(appStore.getTagsView) // 是否显示标签页
const iframeSrcMap = ref<Map<string, string>>(new Map())

export function useFrameKeepAlive() {
  const router = useRouter()
  const { currentRoute } = router

  const getFramePages = computed(() => {
    const ret = getAllFramePages(toRaw(router.getRoutes()) as unknown as AppRouteRecordRaw[]) || []
    return ret
  })

  const getOpenTabList = computed((): string[] => {
    return tagsViewStore.getVisitedViews.reduce((prev: string[], next) => {
      if (next.meta && Reflect.has(next.meta, 'pathType')) {
        prev.push(next.name as string)
      }
      return prev
    }, [])
  })

  function getFrameSrcByName(name: string) {
    return unref(iframeSrcMap).get(name)
  }

  function setFrameSrcByName(name: string, src: string) {
    iframeSrcMap.value.set(name, src)
    return src
  }

  function getAllFramePages(routes: AppRouteRecordRaw[]): AppRouteRecordRaw[] {
    let res: AppRouteRecordRaw[] = []

    for (const route of routes) {
      const { meta: { frameSrc } = {}, children } = route

      if (frameSrc) {
        res.push(route)
      }
      if (children && children.length) {
        res.push(...getAllFramePages(children))
      }
    }
    res = uniqBy(res, 'name')

    return res
  }

  function showIframe(item: AppRouteRecordRaw) {
    return item.name === unref(currentRoute).name
  }

  function hasRenderFrame(name: string) {
    if (!unref(tagsView)) {
      return router.currentRoute.value.name === name
    }

    return unref(getOpenTabList).includes(name)
  }

  return { hasRenderFrame, getFrameSrcByName, setFrameSrcByName, getFramePages, showIframe, getAllFramePages }
}
