<script setup lang="ts">
import { Document, Headset, Picture, VideoPlay } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { inspectLibraryFile } from './libraryUpload'

const props = withDefaults(defineProps<{
  file: File
  disabled?: boolean
  removeLabel?: string
}>(), { disabled: false, removeLabel: '移除文件' })
const emit = defineEmits<{ remove: [] }>()
const url = ref('')
const failed = ref(false)
const kind = computed(() => inspectLibraryFile(props.file, 'document').kind)
const extension = computed(() => props.file.name.split('.').at(-1)?.toUpperCase() || '文件')
const icon = computed(() => kind.value === 'audio' ? Headset : kind.value === 'video' ? VideoPlay : kind.value === 'image' ? Picture : Document)
const label = computed(() => kind.value === 'audio' ? '音频' : kind.value === 'video' ? '视频' : kind.value === 'image' ? '图片' : '文档')
const size = computed(() => props.file.size >= 1024 * 1024
  // eslint-disable-next-line financial/no-division -- Binary file size display, not monetary arithmetic.
  ? `${(props.file.size / (1024 * 1024)).toFixed(1)} MB`
  // eslint-disable-next-line financial/no-division -- Binary file size display, not monetary arithmetic.
  : `${Math.max(1, Math.ceil(props.file.size / 1024))} KB`)

function releaseUrl() {
  if (url.value) {
    URL.revokeObjectURL(url.value)
    url.value = ''
  }
}
watch(() => props.file, (file) => {
  releaseUrl()
  failed.value = false
  if (['image', 'video', 'audio'].includes(kind.value)) {
    url.value = URL.createObjectURL(file)
  }
}, { immediate: true })
onBeforeUnmount(releaseUrl)
</script>

<template>
  <div class="library-file-preview" :aria-label="`${label}文件：${file.name}`">
    <div class="file-preview-frame">
      <img
        v-if="kind === 'image' && !failed"
        :src="url"
        :alt="file.name"
        @error="failed = true"
      >
      <video
        v-else-if="kind === 'video' && !failed"
        :key="url"
        :src="url"
        controls
        playsinline
        preload="metadata"
        :aria-label="file.name"
        @error="failed = true"
      />
      <div v-else class="file-preview-placeholder">
        <ElIcon :size="40"><component :is="icon" /></ElIcon>
        <span>{{ label }}</span>
        <audio
          v-if="kind === 'audio' && !failed"
          :key="url"
          :src="url"
          controls
          preload="metadata"
          :aria-label="file.name"
          @error="failed = true"
        />
        <span v-if="failed" class="preview-hint">此格式暂不支持预览，仍可保存</span>
      </div>
    </div>
    <div class="file-preview-footer">
      <div class="file-preview-description">
        <span class="file-preview-name" :title="file.name">{{ file.name }}</span>
        <span class="file-preview-meta">{{ extension }} · {{ size }}</span>
      </div>
      <ElButton
        link
        type="danger"
        :disabled="disabled"
        @click="emit('remove')"
      >
        {{ removeLabel }}
      </ElButton>
    </div>
  </div>
</template>

<style scoped>
.library-file-preview {
  width: 320px;
  max-width: 100%;
  min-width: 0;
  overflow: hidden;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.file-preview-frame {
  height: 180px;
  overflow: hidden;
  background: var(--xi-semantic-color-background-page);
}

.file-preview-frame img,
.file-preview-frame video {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.file-preview-placeholder {
  display: flex;
  height: 100%;
  min-width: 0;
  padding: var(--xi-component-content-gap-sm);
  box-sizing: border-box;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--xi-component-content-gap-xs);
  color: var(--xi-semantic-color-text-brand);
}

.file-preview-placeholder audio {
  width: 100%;
  height: 40px;
  max-width: 100%;
}

.file-preview-footer {
  display: flex;
  align-items: center;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-sm);
}

.file-preview-description {
  display: grid;
  min-width: 0;
  flex: 1;
}

.file-preview-name {
  overflow: hidden;
  color: var(--xi-semantic-color-text-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-preview-meta,
.preview-hint {
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.file-preview-footer .el-button {
  flex-shrink: 0;
}
</style>
