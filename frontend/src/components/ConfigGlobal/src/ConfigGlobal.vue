<script setup lang="ts">
import type { ComponentSize } from 'element-plus'
import { useDesign } from '@/hooks/web/useDesign'
import { useAppStore } from '@/store/modules/app'
import { useLocaleStore } from '@/store/modules/locale'
import { setCssVar } from '@/utils'
import { propTypes } from '@/utils/propTypes'
import { computed, onMounted, provide, watch } from '@/vue-export'
import { useWindowSize } from '@vueuse/core'
import { ElConfigProvider } from 'element-plus'

const props = defineProps({
  size: propTypes.oneOf<ComponentSize>(['default', 'small', 'large']).def('default'),
})

const { variables } = useDesign()

const appStore = useAppStore()

provide('configGlobal', props)

// 初始化所有主题色
onMounted(() => {
  appStore.setCssVarTheme()
})

const { width } = useWindowSize()

// 监听窗口变化
watch(
  () => width.value,
  (width: number) => {
    if (width < 768) {
      !appStore.getMobile ? appStore.setMobile(true) : undefined
      setCssVar('--left-menu-min-width', '0')
      appStore.setCollapse(true)
      appStore.getLayout !== 'classic' ? appStore.setLayout('classic') : undefined
    }
    else {
      appStore.getMobile ? appStore.setMobile(false) : undefined
      // 由于取消了菜单的图标，因此在collapse的时候需要将菜单的宽度设置为0，由于布局原因，设置为1px
      setCssVar('--left-menu-min-width', '1px')
    }
  },
  {
    immediate: true,
  },
)

// 多语言相关
const localeStore = useLocaleStore()

const currentLocale = computed(() => localeStore.currentLocale)
</script>

<template>
  <ElConfigProvider
    :namespace="variables.elNamespace"
    :locale="currentLocale.elLocale"
    :message="{ max: 5 }"
    :size="size"
  >
    <slot />
  </ElConfigProvider>
</template>
