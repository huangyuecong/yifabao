<script setup lang="ts">
import type { LibraryItem } from '@/api/agent/content'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { contentApi } from '@/api/agent/content'

const props = withDefaults(defineProps<{
  item: LibraryItem
  preview?: boolean
}>(), {
  preview: true,
})

const root = ref<HTMLElement>()
const objectUrl = ref('')
const loading = ref(false)
const failed = ref(false)
const imagePreviewOpen = ref(false)
let observer: IntersectionObserver | undefined
let disposed = false

const directUrl = computed(() => {
  const original = props.item.metadata?.original_url
  const value = typeof original === 'string' && original.trim()
    ? original.trim()
    : props.item.source_url?.trim() || ''
  return /^(?:https?:|data:|blob:)/iu.test(value) ? value : ''
})
const mediaUrl = computed(() => objectUrl.value || directUrl.value)
const mediaType = computed(() => {
  if (props.item.resource_kind === 'video') {
    return 'video'
  }
  if (props.item.resource_kind === 'audio') {
    return 'audio'
  }
  return 'image'
})
const placeholder = computed(() => ({
  image: props.item.resource_kind === 'logo' ? 'Logo' : '图片',
  video: '视频',
  audio: '音频',
}[mediaType.value]))

function openImagePreview() {
  if (mediaType.value === 'image' && mediaUrl.value) {
    imagePreviewOpen.value = true
  }
}

watch(mediaUrl, () => {
  imagePreviewOpen.value = false
})

async function loadMedia() {
  const hasDownloadableAsset = props.item.has_asset || props.item.download_url || props.item.storage_key
  if (disposed || loading.value || objectUrl.value || !hasDownloadableAsset) {
    return
  }
  loading.value = true
  try {
    const result = await contentApi.downloadLibraryItem(props.item.id)
    if (disposed) {
      return
    }
    objectUrl.value = URL.createObjectURL(result.blob)
  }
  catch {
    if (!disposed) {
      failed.value = !directUrl.value
    }
  }
  finally {
    if (!disposed) {
      loading.value = false
    }
  }
}

onMounted(() => {
  if (!root.value || typeof IntersectionObserver === 'undefined') {
    void loadMedia()
    return
  }
  observer = new IntersectionObserver((entries) => {
    if (entries.some(entry => entry.isIntersecting)) {
      void loadMedia()
      observer?.disconnect()
    }
  }, { rootMargin: '160px' })
  observer.observe(root.value)
})

onBeforeUnmount(() => {
  disposed = true
  observer?.disconnect()
  if (objectUrl.value) {
    URL.revokeObjectURL(objectUrl.value)
  }
})
</script>

<template>
  <div ref="root" class="resource-preview" :class="`is-${mediaType}`">
    <ElImage
      v-if="mediaType === 'image' && mediaUrl"
      :src="mediaUrl"
      :alt="item.name"
      fit="contain"
      loading="lazy"
      :preview-src-list="[]"
      role="button"
      tabindex="0"
      title="双击查看大图"
      :aria-label="`${item.name}，双击或按回车查看大图`"
      @click="preview && openImagePreview()"
      @dblclick.stop="openImagePreview"
      @keydown.enter.stop.prevent="openImagePreview"
      @keydown.space.stop.prevent="openImagePreview"
    />
    <video
      v-else-if="mediaType === 'video' && mediaUrl"
      :src="mediaUrl"
      controls
      preload="metadata"
    />
    <audio
      v-else-if="mediaType === 'audio' && mediaUrl"
      :src="mediaUrl"
      controls
      preload="none"
    />
    <div v-else class="resource-placeholder" :aria-label="failed ? `${item.name}预览不可用` : item.name">
      <span>{{ loading ? '加载中' : placeholder }}</span>
      <small v-if="failed">预览暂不可用</small>
    </div>
    <ElImageViewer
      v-if="imagePreviewOpen && mediaUrl"
      :url-list="[mediaUrl]"
      teleported
      @close="imagePreviewOpen = false"
    />
  </div>
</template>

<style scoped>
.resource-preview,
.resource-preview :deep(.el-image),
.resource-preview video {
  width: 100%;
  height: 100%;
}

.resource-preview :deep(.el-image) {
  cursor: zoom-in;
}

.resource-preview :deep(.el-image:focus-within) {
  outline: 2px solid var(--xi-semantic-color-border-brand-default);
  outline-offset: -2px;
}

.resource-preview {
  min-height: 72px;
  overflow: hidden;
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
}

.resource-preview video {
  display: block;
  object-fit: contain;
}

.resource-preview audio {
  width: calc(100% - (var(--xi-component-content-gap-sm) * 2));
  margin: var(--xi-component-content-gap-sm);
}

.resource-placeholder {
  display: grid;
  min-height: inherit;
  place-content: center;
  gap: var(--xi-component-content-gap-xxs);
  color: var(--xi-semantic-color-text-tertiary);
  text-align: center;
}

.resource-placeholder small {
  font-size: var(--xi-semantic-typography-caption-font-size);
}
</style>
