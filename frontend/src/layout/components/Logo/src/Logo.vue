<script lang="ts" setup>
import { useDesign } from '@/hooks/web/useDesign'
import { useAppStore } from '@/store/modules/app'
import { computed, defineOptions, onMounted, ref, unref, watch } from '@/vue-export'

defineOptions({ name: 'Logo' })

const { getPrefixCls } = useDesign()

const prefixCls = getPrefixCls('logo')

const appStore = useAppStore()

const show = ref(true)

// const title = computed(() => appStore.getTitle)

const layout = computed(() => appStore.getLayout)

const collapse = computed(() => appStore.getCollapse)

onMounted(() => {
  if (unref(collapse)) {
    show.value = false
  }
})

watch(
  () => collapse.value,
  (collapse: boolean) => {
    if (unref(layout) === 'topLeft' || unref(layout) === 'cutMenu') {
      show.value = true
      return
    }
    if (!collapse) {
      setTimeout(() => {
        show.value = !collapse
      }, 400)
    }
    else {
      show.value = !collapse
    }
  },
)

watch(
  () => layout.value,
  (layout) => {
    if (layout === 'top' || layout === 'cutMenu') {
      show.value = true
    }
    else {
      if (unref(collapse)) {
        show.value = false
      }
      else {
        show.value = true
      }
    }
  },
)
</script>

<template>
  <div class="logo relative z-10 !h-[64px]">
    <router-link
      class="cursor-pointer decoration-none"
      :class="[
        prefixCls,
        layout !== 'classic' ? `${prefixCls}__Top` : '',
      ]"
      to="/"
    >
      <img
        class="h-[64px] w-[200px]"
        src="@/assets/imgs/logo.png"
      >
    </router-link>
  </div>
</template>

<style lang="scss" scoped>

</style>
