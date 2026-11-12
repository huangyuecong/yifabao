<script setup lang="ts">
import type { LibraryItem } from '@/api/agent/content'
import { Headset, Picture, VideoPlay } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { contentApi } from '@/api/agent/content'
import { resourceMediaKind } from './contentResourceCatalog'

const props = withDefaults(defineProps<{
  item?: LibraryItem
  alt?: string
  controls?: boolean
  compact?: boolean
}>(), {
  alt: '资料素材预览',
  controls: false,
  compact: false,
})

const mediaUrl = ref('')
const loading = ref(false)
const failed = ref(false)
const previewVisible = ref(false)
let objectUrl = ''
let loadVersion = 0

const mediaKind = computed(() => resourceMediaKind(props.item))
const canRender = computed(() => ['image', 'logo', 'video', 'audio'].includes(mediaKind.value ?? ''))

function releaseObjectUrl() {
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl)
    objectUrl = ''
  }
}

function safeRemoteUrl(value?: string | null) {
  if (!value) {
    return ''
  }
  const match = value.trim().match(/^https?:\/\/[^\s\u3400-\u9FFF]+/u)
  return match?.[0] ?? ''
}

function metadataRemoteUrl(item?: LibraryItem) {
  const value = item?.metadata?.original_url
  return typeof value === 'string' ? safeRemoteUrl(value) : ''
}

async function loadMedia() {
  const currentLoadVersion = ++loadVersion
  previewVisible.value = false
  releaseObjectUrl()
  mediaUrl.value = ''
  failed.value = false
  const item = props.item
  if (!item || !canRender.value) {
    return
  }

  const fallbackUrl = safeRemoteUrl(item.source_url) || metadataRemoteUrl(item)
  if (!item.storage_key && !item.has_asset && !item.download_url) {
    mediaUrl.value = fallbackUrl
    failed.value = !fallbackUrl
    return
  }

  loading.value = true
  try {
    const result = item.download_url
      ? await contentApi.downloadLibraryUrl(item.download_url)
      : await contentApi.downloadLibraryItem(item.id)
    const nextObjectUrl = URL.createObjectURL(result.blob)
    if (currentLoadVersion !== loadVersion) {
      URL.revokeObjectURL(nextObjectUrl)
      return
    }
    objectUrl = nextObjectUrl
    mediaUrl.value = nextObjectUrl
  }
  catch {
    if (currentLoadVersion !== loadVersion) {
      return
    }
    mediaUrl.value = fallbackUrl
    failed.value = !fallbackUrl
  }
  finally {
    if (currentLoadVersion === loadVersion) {
      loading.value = false
    }
  }
}

function markFailed() {
  failed.value = true
  previewVisible.value = false
}

function openImagePreview() {
  if (['image', 'logo'].includes(mediaKind.value ?? '') && mediaUrl.value && !loading.value && !failed.value) {
    previewVisible.value = true
  }
}

watch(() => props.item?.id, loadMedia, { immediate: true })
onBeforeUnmount(() => {
  loadVersion += 1
  releaseObjectUrl()
})
</script>

<template>
  <div
    class="library-media"
    :class="{ 'is-compact': compact, 'is-image': ['image', 'logo'].includes(mediaKind ?? '') }"
    :aria-busy="loading"
  >
    <ElSkeleton v-if="loading" animated class="media-skeleton">
      <template #template><ElSkeletonItem variant="image" class="media-skeleton-item" /></template>
    </ElSkeleton>
    <template v-else-if="mediaUrl && !failed">
      <img
        v-if="['image', 'logo'].includes(mediaKind ?? '')"
        :src="mediaUrl"
        :alt="alt"
        :aria-label="`${alt}，双击或按回车键查看大图`"
        title="双击查看大图，也可按回车或空格键打开"
        role="button"
        tabindex="0"
        loading="lazy"
        @dblclick.stop="openImagePreview"
        @keydown.enter.stop.prevent="openImagePreview"
        @keydown.space.stop.prevent="openImagePreview"
        @error="markFailed"
      >
      <video
        v-else-if="mediaKind === 'video'"
        :src="mediaUrl"
        :controls="controls"
        :muted="!controls"
        preload="metadata"
        @error="markFailed"
      />
      <audio
        v-else-if="mediaKind === 'audio'"
        :src="mediaUrl"
        :controls="controls"
        preload="metadata"
        @error="markFailed"
      />
    </template>
    <div v-else class="media-placeholder">
      <ElIcon>
        <Picture v-if="['image', 'logo'].includes(mediaKind ?? '')" />
        <VideoPlay v-else-if="mediaKind === 'video'" />
        <Headset v-else-if="mediaKind === 'audio'" />
        <Picture v-else />
      </ElIcon>
      <span>{{ canRender ? '暂无可预览文件' : '无媒体预览' }}</span>
    </div>
    <ElImageViewer
      v-if="previewVisible && mediaUrl"
      :url-list="[mediaUrl]"
      teleported
      @close="previewVisible = false"
    />
  </div>
</template>

<style scoped>
.library-media {
  display: grid;
  width: 100%;
  min-height: var(--xi-component-content-preview-height);
  overflow: hidden;
  place-items: center;
  background: var(--xi-semantic-color-background-page);
  border-radius: var(--xi-component-content-radius-control);
}

.library-media.is-compact {
  min-height: var(--xi-component-content-control-height-large);
}

.library-media.is-image {
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  min-width: 0;
}

.library-media img,
.library-media video {
  display: block;
  width: 100%;
  height: 100%;
  max-height: var(--xi-component-content-preview-height);
  object-fit: contain;
}

.library-media audio {
  width: calc(100% - var(--xi-component-content-gap-lg));
}

.library-media img {
  min-width: 0;
  min-height: 0;
  max-width: 100%;
  cursor: zoom-in;
}

.library-media img:focus-visible {
  outline: 2px solid var(--xi-semantic-color-text-brand);
  outline-offset: -2px;
}

.media-skeleton,
.media-skeleton-item {
  width: 100%;
  height: 100%;
  min-height: inherit;
}

.media-placeholder {
  display: grid;
  gap: var(--xi-component-content-gap-xs);
  justify-items: center;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.media-placeholder .el-icon {
  color: var(--xi-semantic-color-text-brand);
  font-size: var(--xi-component-content-icon-size);
}
</style>
