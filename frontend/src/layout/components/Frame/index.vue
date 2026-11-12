<script setup>
import { propTypes } from '@/utils/propTypes'
import { computed, defineEmits, defineExpose, defineProps, onMounted, onUnmounted, ref, unref } from '@/vue-export'
import { useWindowSizeFn } from './useWindowSizeFn'

defineProps({
  frameSrc: propTypes.string.def(''),
})
const emit = defineEmits(['message'])
const loading = ref(true)
const topRef = ref(50)
const heightRef = ref(window.innerHeight)
const frameRef = ref(null)
useWindowSizeFn(calcHeight, { wait: 150, immediate: true })

const getWrapStyle = computed(() => {
  return {
    height: `${unref(heightRef)}px`,
  }
})

function calcHeight() {
  const iframe = unref(frameRef)
  if (!iframe) {
    return
  }
  // const top = headerHeightRef.value
  const top = 85

  topRef.value = top
  heightRef.value = window.innerHeight - top
  const clientHeight = document.documentElement.clientHeight - top
  iframe.style.height = `${clientHeight}px`
}

function hideLoading() {
  loading.value = false
  calcHeight()
}

const messageHandler = (e) => {
  emit('message', e.data)
}

const postMessage = (message, tragetOrigin, transfer) => {
  const iframe = document.querySelector('#iframe')
  if (!iframe) {
    return
  }

  iframe.contentWindow?.postMessage(message, tragetOrigin, transfer)
}

const reload = () => {
  loading.value = true
  const iframe = frameRef.value
  if (!iframe) {
    return
  }
  iframe.contentWindow?.location.reload()
  loading.value = false
}
const remove = () => {
  const iframe = document.querySelector('#iframe')
  if (!iframe) {
    return
  }

  iframe.remove()
}

onMounted(() => {
  window.addEventListener('message', messageHandler)
})

onUnmounted(() => {
  window.removeEventListener('message', messageHandler)
})

defineExpose({ postMessage, reload, remove })
</script>

<template>
  <div v-loading="loading" :style="getWrapStyle">
    <iframe
      id="iframe"
      ref="frameRef"
      frameborder="no"
      scrolling="auto"
      width="100%"
      height="100%"
      :src="frameSrc"

      box-border
      block
      h-full
      w-full
      overflow-hidden
      border-0
      @load="hideLoading"
    />
  </div>
</template>
