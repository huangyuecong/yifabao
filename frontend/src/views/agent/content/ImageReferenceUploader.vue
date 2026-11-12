<script setup lang="ts">
import type { UploadFile, UploadFiles, UploadInstance, UploadRawFile } from 'element-plus'
import type { LibraryItem } from '@/api/agent/content'
import { computed, ref, watch } from 'vue'
import { contentApi } from '@/api/agent/content'
import ContentResourcePreview from './ContentResourcePreview.vue'
import { friendlyContentError } from './contentWorkflow'

const props = withDefaults(defineProps<{
  taskId: string
  selectedItems: LibraryItem[]
  remaining: number
  limit?: number
  disabled?: boolean
}>(), {
  limit: undefined,
  disabled: false,
})

const emit = defineEmits<{
  uploaded: [items: LibraryItem[]]
  remove: [id: string]
}>()

const uploadRef = ref<UploadInstance>()
const selectedFiles = ref<UploadFiles>([])
const pendingPreviewUrl = ref('')
const uploading = ref(false)
const completedCount = ref(0)
const errorMessage = ref('')
const maximumFileSize = 30 * 1024 * 1024
const supportedImageTypes = new Set(['image/jpeg', 'image/png', 'image/webp'])
const supportedImageExtensions = new Set(['jpg', 'jpeg', 'png', 'webp'])

const selectableLimit = computed(() => Math.max(1, Math.floor(props.remaining)))
const rawFiles = computed(() => selectedFiles.value
  .map(item => item.raw)
  .filter((file): file is UploadRawFile => Boolean(file)),
)
const fileValidationErrors = computed(() => rawFiles.value
  .map(file => ({ file, message: validateImageFile(file) }))
  .filter(item => item.message),
)
const canUpload = computed(() => Boolean(
  !props.disabled
  && !uploading.value
  && props.remaining > 0
  && rawFiles.value.length
  && rawFiles.value.length <= props.remaining
  && !fileValidationErrors.value.length,
))
const uploadPercentage = computed(() => {
  if (!rawFiles.value.length) {
    return 0
  }
  // eslint-disable-next-line financial/no-division -- progress is a UI percentage, not financial arithmetic.
  return Math.round((completedCount.value / rawFiles.value.length) * 100)
})

function validateImageFile(file: File) {
  const extension = file.name.split('.').pop()?.toLocaleLowerCase('en-US') || ''
  if (!supportedImageTypes.has(file.type.toLocaleLowerCase('en-US'))
    || !supportedImageExtensions.has(extension)) {
    return `“${file.name}”格式不支持，仅可上传 JPEG、PNG 或 WebP 图片。`
  }
  if (file.size > maximumFileSize) {
    return `“${file.name}”超过单张 30MB 上限，请压缩后重试。`
  }
  return ''
}

function syncFiles(_file: UploadFile, files: UploadFiles) {
  pendingPreviewUrl.value = ''
  selectedFiles.value = files
  completedCount.value = 0
  if (fileValidationErrors.value.length) {
    errorMessage.value = fileValidationErrors.value[0]?.message || '所选图片不符合上传要求。'
    return
  }
  if (rawFiles.value.length > props.remaining) {
    errorMessage.value = `当前最多还可上传 ${props.remaining} 张参考图片。`
    return
  }
  errorMessage.value = ''
}

function handleRemove(_file: UploadFile, files: UploadFiles) {
  syncFiles(_file, files)
}

function handleExceed() {
  errorMessage.value = props.remaining > 0
    ? `当前最多还可上传 ${props.remaining} 张参考图片，请先移除已有参考图。`
    : `参考图片已达到 ${props.limit ?? '当前模型'}上限，请先移除已有参考图。`
}

function previewPendingImage(target: EventTarget | null) {
  if (!(target instanceof HTMLImageElement)
    || !target.classList.contains('el-upload-list__item-thumbnail')) {
    return false
  }
  const file = selectedFiles.value.find(item => item.url && item.url === target.src)
  if (!file) {
    return false
  }
  // ElUpload owns this local object URL. Reuse it without creating or revoking it.
  pendingPreviewUrl.value = target.currentSrc || target.src
  return true
}

function handlePendingImageDoubleClick(event: MouseEvent) {
  if (previewPendingImage(event.target)) {
    event.stopPropagation()
  }
}

function handlePendingImageKeydown(event: KeyboardEvent) {
  if (!['Enter', ' '].includes(event.key)
    || !(event.target instanceof HTMLElement)
    || !event.target.matches('li.el-upload-list__item')) {
    return
  }
  const thumbnail = event.target.querySelector<HTMLImageElement>('img.el-upload-list__item-thumbnail')
  if (previewPendingImage(thumbnail)) {
    event.preventDefault()
    event.stopPropagation()
  }
}

watch(() => selectedFiles.value.map(file => [file.uid, file.url]), () => {
  pendingPreviewUrl.value = ''
})

async function uploadSelectedImages() {
  const files = [...rawFiles.value]
  if (!canUpload.value) {
    errorMessage.value ||= props.remaining > 0
      ? '请先选择要作为参考的本地图片。'
      : `参考图片已达到 ${props.limit ?? '当前模型'}上限，请先移除已有参考图。`
    return
  }

  uploading.value = true
  completedCount.value = 0
  errorMessage.value = ''
  const uploaded: LibraryItem[] = []
  const failures: string[] = []
  for (const file of files) {
    try {
      uploaded.push(await contentApi.uploadImageReference(props.taskId, file))
    }
    catch (error) {
      failures.push(`${file.name}：${friendlyContentError(error, '上传失败')}`)
    }
    finally {
      completedCount.value += 1
    }
  }

  if (uploaded.length) {
    emit('uploaded', uploaded)
  }
  pendingPreviewUrl.value = ''
  uploadRef.value?.clearFiles()
  selectedFiles.value = []
  uploading.value = false
  if (failures.length) {
    errorMessage.value = `${failures.join('；')}。请重新选择失败的图片后重试。`
  }
}
</script>

<template>
  <section class="image-reference-uploader" :aria-busy="uploading">
    <div class="reference-upload-heading">
      <strong>上传本地图片</strong>
      <span>还可上传 {{ Math.max(remaining, 0) }} 张</span>
    </div>
    <div v-if="selectedItems.length" class="selected-reference-list">
      <article v-for="item in selectedItems" :key="item.id">
        <ContentResourcePreview class="selected-reference-preview" :item="item" :preview="false" />
        <div>
          <strong>{{ item.name }}</strong>
          <span>已加入本次图片生成</span>
        </div>
        <ElButton
          link
          :aria-label="`移除本地参考图 ${item.name}`"
          @click="emit('remove', item.id)"
        >
          移除
        </ElButton>
      </article>
    </div>
    <ElUpload
      ref="uploadRef"
      drag
      multiple
      action="#"
      accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"
      list-type="picture"
      :auto-upload="false"
      :disabled="disabled || uploading || remaining <= 0"
      :limit="selectableLimit"
      :on-change="syncFiles"
      :on-remove="handleRemove"
      :on-exceed="handleExceed"
      @dblclick="handlePendingImageDoubleClick"
      @keydown="handlePendingImageKeydown"
    >
      <div class="upload-dropzone-copy">
        <strong>
          {{ remaining > 0 ? '拖入或选择本地图片' : `参考图片已达到 ${limit ?? '当前模型'}上限` }}
        </strong>
        <span>支持一次多选；上传成功后会自动加入本次图片生成参考。</span>
      </div>
    </ElUpload>
    <small v-if="selectedFiles.length" class="pending-preview-hint">
      双击缩略图查看大图；键盘选中文件行后，也可按回车或空格查看。
    </small>
    <ElImageViewer
      v-if="pendingPreviewUrl"
      :url-list="[pendingPreviewUrl]"
      teleported
      @close="pendingPreviewUrl = ''"
    />

    <ElProgress
      v-if="uploading"
      :percentage="uploadPercentage"
      :indeterminate="completedCount === 0"
      :duration="2"
    />
    <p
      v-if="errorMessage"
      class="upload-error"
      role="alert"
      aria-live="assertive"
    >
      {{ errorMessage }}
    </p>
    <div class="upload-actions">
      <span
        v-if="rawFiles.length"
        aria-live="polite"
      >
        已选择 {{ rawFiles.length }} 张图片
      </span>
      <ElButton
        type="primary"
        plain
        :loading="uploading"
        :disabled="!canUpload"
        @click="uploadSelectedImages"
      >
        上传并作为参考图
      </ElButton>
    </div>
  </section>
</template>

<style scoped>
.image-reference-uploader,
.upload-dropzone-copy,
.selected-reference-list,
.selected-reference-list article > div {
  display: grid;
}

.image-reference-uploader {
  gap: var(--xi-component-content-gap-sm);
  width: 100%;
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
}

.reference-upload-heading,
.upload-actions,
.selected-reference-list article {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-sm);
}

.reference-upload-heading span,
.upload-actions span,
.upload-dropzone-copy span,
.pending-preview-hint,
.selected-reference-list span {
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.image-reference-uploader :deep(.el-upload-list__item-thumbnail) {
  object-fit: contain;
  cursor: zoom-in;
}

.image-reference-uploader :deep(.el-upload),
.image-reference-uploader :deep(.el-upload-dragger) {
  width: 100%;
}

.image-reference-uploader :deep(.el-upload-dragger) {
  padding: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-container);
  border-color: var(--xi-semantic-color-border-default);
}

.upload-dropzone-copy {
  gap: var(--xi-component-content-gap-xxs);
}

.selected-reference-list {
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--xi-component-content-gap-xs);
}

.selected-reference-list article {
  justify-content: initial;
  min-width: 0;
  padding: var(--xi-component-content-gap-xs);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand-subtle);
  border-radius: var(--xi-component-content-radius-control);
}

.selected-reference-preview {
  width: 52px;
  height: 52px;
  min-height: 52px;
  overflow: hidden;
  border-radius: var(--xi-component-content-radius-control);
}

.selected-reference-list article > div {
  min-width: 0;
  flex: 1;
  gap: var(--xi-component-content-gap-xxs);
}

.selected-reference-list strong,
.selected-reference-list span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.upload-error {
  margin: 0;
  color: var(--el-color-danger);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

@media (max-width: 720px) {
  .reference-upload-heading,
  .upload-actions {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
