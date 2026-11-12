<script lang="ts" setup>
import { Footer } from '@/layout/components/Footer'
import FrameLayout from '@/layout/iframe/index.vue'
import { contentRoutePageKey } from '@/router/videoEditorRouteSession'
import { useAppStore } from '@/store/modules/app'
import { useTagsViewStore } from '@/store/modules/tagsView'
import { computed, defineOptions, nextTick, provide, ref } from '@/vue-export'

defineOptions({ name: 'AppView' })

const appStore = useAppStore()

const layout = computed(() => appStore.getLayout)

const fixedHeader = computed(() => appStore.getFixedHeader)

const footer = computed(() => appStore.getFooter)

const tagsViewStore = useTagsViewStore()
const mobile = computed(() => appStore.getMobile)
// 组件库的几个大表格页，keep-alive 恢复时要一次性把上千个节点重新插回文档，
// 造成的重排比重新挂载还慢，因此不缓存
const noCacheViews = ['SystemDict', 'XuiMenu', 'XuiRole', 'SystemGroup']
const contentWorkbenchName = 'ContentWorkbench'
const getCaches = computed((): string[] => {
  const names = tagsViewStore.getCachedViews.filter(name => !noCacheViews.includes(name))
  // Content routes share this component name; tagsView caches route names and would never hit it.
  return names.includes(contentWorkbenchName) ? names : [...names, contentWorkbenchName]
})

const tagsView = computed(() => appStore.getTagsView)

// region 无感刷新
const routerAlive = ref(true)
// 无感刷新，防止出现页面闪烁白屏
const reload = () => {
  routerAlive.value = false
  nextTick(() => (routerAlive.value = true))
}
// 为组件后代提供刷新方法
provide('reload', reload)
// endregion
</script>

<template>
  <!-- 原来宽度 w-[calc(100%-var(--app-content-padding)-var(--app-content-padding))] -->
  <section
    class="app-view dark:bg-[var(--el-bg-color)]"
    :class="[
      {
        '!min-h-[calc(100%-var(--app-content-padding)-var(--app-content-padding)-var(--app-footer-height))]':
          (fixedHeader
            && (layout === 'classic' || layout === 'topLeft' || layout === 'top')
            && footer)
          || (!tagsView && layout === 'top' && footer),
        '!min-h-[calc(100%-var(--app-content-padding)-var(--app-content-padding)-var(--app-footer-height)-var(--tags-view-height))]':
          tagsView && layout === 'top' && footer,

        '!min-h-[calc(100%-var(--tags-view-height)-var(--app-content-padding)-var(--app-content-padding)-var(--top-tool-height)-var(--app-footer-height))]':
          !fixedHeader && layout === 'classic' && footer,

        '!min-h-[calc(100%-var(--tags-view-height)-var(--app-content-padding)-var(--app-content-padding)-var(--app-footer-height))]':
          !fixedHeader && layout === 'topLeft' && footer,

        '!min-h-[calc(100%-var(--top-tool-height)-var(--app-content-padding)-var(--app-content-padding))]':
          fixedHeader && layout === 'cutMenu' && footer,

        '!min-h-[calc(100%-var(--top-tool-height)-var(--app-content-padding)-var(--app-content-padding)-var(--tags-view-height))]':
          !fixedHeader && layout === 'cutMenu' && footer,
        '!w-full p-0': mobile,
      },
    ]"
  >
    <router-view v-if="routerAlive">
      <template #default="{ Component, route }">
        <keep-alive :include="getCaches">
          <component :is="Component" :key="contentRoutePageKey(route)" />
        </keep-alive>
      </template>
    </router-view>
    <FrameLayout />
  </section>
  <Footer v-if="footer" />
</template>

<style lang="scss" scoped>
.app-view {
  padding: 0 var(--app-content-padding);
  width: 100%;
  padding-left: 24px;
}
</style>
