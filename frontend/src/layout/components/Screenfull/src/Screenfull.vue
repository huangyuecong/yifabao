<script lang="ts" setup>
import { Icon } from '@/components/Icon'
import { useDesign } from '@/hooks/web/useDesign'
import { propTypes } from '@/utils/propTypes'
import { defineOptions, defineProps } from '@/vue-export'
import { useFullscreen } from '@vueuse/core'

defineOptions({ name: 'ScreenFull' })

defineProps({
  color: propTypes.string.def(''),
})

const { getPrefixCls } = useDesign()

const prefixCls = getPrefixCls('screenfull')

const { toggle, isFullscreen } = useFullscreen()

const toggleFullscreen = () => {
  toggle()
}
</script>

<template>
  <div :class="prefixCls" @click="toggleFullscreen">
    <Icon
      :color="color"
      :icon="isFullscreen ? 'svg-icon:fullscreen-exit' : 'svg-icon:fullscreen'"
      :size="18"
    />
  </div>
</template>
