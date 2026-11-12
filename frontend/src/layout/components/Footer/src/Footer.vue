<script lang="ts" setup>
import { useDesign } from '@/hooks/web/useDesign'
import { useAppStore } from '@/store/modules/app'

import { computed, defineOptions } from '@/vue-export'

// eslint-disable-next-line vue/no-reserved-component-names
defineOptions({ name: 'Footer' })

const { getPrefixCls } = useDesign()

const prefixCls = getPrefixCls('footer')

const appStore = useAppStore()

const title = computed(() => appStore.getTitle)

const currentYear = new Date().getFullYear()
</script>

<template>
  <div
    :class="prefixCls"
    class="h-[var(--app-footer-height)] overflow-hidden text-center text-[var(--el-text-color-placeholder)] leading-[var(--app-footer-height)] dark:bg-[var(--el-bg-color)]"
  >
    <span class="text-14px">Copyright ©2024-{{ currentYear }}
      {{ title }} </span>
  </div>
</template>
