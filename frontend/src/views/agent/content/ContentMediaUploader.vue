<script setup lang="ts">
import type { UploadFile } from 'element-plus'
import type { LibraryDataLevel, LibraryItem, LibraryResourceKind } from '@/api/agent/content'
import { computed, onBeforeUnmount, ref } from 'vue'
import { contentApi } from '@/api/agent/content'
import { friendlyContentError } from './contentWorkflow'

const props = withDefaults(defineProps<{
  defaultCategory?: string
  dataLevel?: LibraryDataLevel
}>(), {
  defaultCategory: '用户上传素材',
  dataLevel: 'internal',
})

const emit = defineEmits<{
  uploaded: [item: LibraryItem]
}>()

const selectedFile = ref<File>()
const name = ref('')
const category = ref(props.defaultCategory)
const uploading = ref(false)
const uploadCompleted = ref(false)
const uploadProgress = ref(0)
const errorMessage = ref('')
let progressTimer: ReturnType<typeof setInterval> | undefined

const resourceKind = computed<LibraryResourceKind | undefined>(() => inferResourceKind(selectedFile.value?.type || ''))
const canUpload = computed(() => Boolean(
  selectedFile.value
  && resourceKind.value
  && name.value.trim()
  && category.value.trim()
  && !uploading.value,
))

function inferResourceKind(mimeType: string): LibraryResourceKind | undefined {
  if (mimeType.startsWith('image/')) {
    return 'image'
  }
  if (mimeType.startsWith('video/')) {
    return 'video'
  }
  if (mimeType.startsWith('audio/')) {
    return 'audio'
  }
  return undefined
}

function resourceKindLabel(value?: LibraryResourceKind) {
  return {
    image: '图片',
    video: '视频',
    audio: '音频',
  }[value || ''] || '无法识别'
}

function fileSizeLabel(bytes: number) {
  return `${new Intl.NumberFormat('zh-CN').format(bytes)} 字节`
}

function handleFileChange(uploadFile: UploadFile) {
  const file = uploadFile.raw
  errorMessage.value = ''
  uploadCompleted.value = false
  uploadProgress.value = 0
  if (!file) {
    selectedFile.value = undefined
    return
  }
  if (!inferResourceKind(file.type)) {
    selectedFile.value = undefined
    errorMessage.value = '仅支持图片、视频或音频文件，请重新选择。'
    return
  }
  selectedFile.value = file
  name.value = file.name
}

function startProgress() {
  uploadProgress.value = 12
  progressTimer = setInterval(() => {
    uploadProgress.value = Math.min(88, uploadProgress.value + 4)
  }, 350)
}

function stopProgress() {
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = undefined
  }
}

async function upload() {
  const file = selectedFile.value
  const kind = resourceKind.value
  if (!file || !kind || !canUpload.value) {
    errorMessage.value = '请选择素材，并填写素材名称和分类。'
    return
  }
  uploading.value = true
  uploadCompleted.value = false
  errorMessage.value = ''
  startProgress()
  try {
    const item = await contentApi.uploadLibrary({
      resource_kind: kind,
      category: category.value.trim(),
      name: name.value.trim(),
      data_level: props.dataLevel,
      tags: ['用户上传'],
      file,
    })
    stopProgress()
    uploadProgress.value = 100
    uploadCompleted.value = true
    emit('uploaded', item)
  }
  catch (error) {
    stopProgress()
    uploadProgress.value = 0
    errorMessage.value = friendlyContentError(error, '素材上传失败，请检查文件后重试。')
  }
  finally {
    uploading.value = false
  }
}

function reset() {
  stopProgress()
  selectedFile.value = undefined
  name.value = ''
  category.value = props.defaultCategory
  uploadCompleted.value = false
  uploadProgress.value = 0
  errorMessage.value = ''
}

onBeforeUnmount(stopProgress)
</script>

<template>
  <section class="content-media-uploader" :aria-busy="uploading">
    <ElUpload
      drag
      action="#"
      accept="image/*,video/*,audio/*"
      :auto-upload="false"
      :show-file-list="false"
      :disabled="uploading"
      :on-change="handleFileChange"
    >
      <div class="upload-dropzone-copy">
        <strong>拖入或选择图片、视频、音频</strong>
        <span>文件将登记到内容智能体自己的资料与素材目录</span>
      </div>
    </ElUpload>

    <article v-if="selectedFile" class="selected-file">
      <div>
        <strong>{{ selectedFile.name }}</strong>
        <span>{{ resourceKindLabel(resourceKind) }} · {{ selectedFile.type || '类型未知' }} · {{ fileSizeLabel(selectedFile.size) }}</span>
      </div>
      <ElButton v-if="!uploading" link @click="reset">重新选择</ElButton>
    </article>

    <div class="upload-fields">
      <ElFormItem label="素材名称" required>
        <ElInput v-model="name" :disabled="uploading" placeholder="默认使用文件名，可修改" />
      </ElFormItem>
      <ElFormItem label="素材分类" required>
        <ElInput v-model="category" :disabled="uploading" placeholder="例如：用户上传素材" />
      </ElFormItem>
    </div>

    <div v-if="uploading || uploadCompleted" class="upload-progress">
      <div>
        <strong>{{ uploadCompleted ? '素材上传成功' : '正在上传并登记素材' }}</strong>
        <span>{{ uploadCompleted ? '已可在当前工作区或企业素材目录中选择使用。' : '请保持当前页面打开。' }}</span>
      </div>
      <ElProgress
        :percentage="uploadProgress"
        :status="uploadCompleted ? 'success' : undefined"
        :indeterminate="uploading"
        :duration="2"
      />
    </div>

    <ElAlert
      v-if="errorMessage"
      type="error"
      title="素材上传失败"
      :description="errorMessage"
      :closable="false"
      show-icon
    />

    <div class="upload-actions">
      <ElButton
        type="primary"
        :loading="uploading"
        :disabled="!canUpload"
        @click="upload"
      >
        上传并加入素材库
      </ElButton>
      <ElButton v-if="uploadCompleted" @click="reset">继续上传</ElButton>
    </div>
  </section>
</template>

<style scoped>
.content-media-uploader,
.upload-dropzone-copy,
.selected-file > div,
.upload-progress,
.upload-progress > div {
  display: grid;
}

.content-media-uploader {
  gap: var(--xi-component-content-gap-md);
  width: 100%;
}

.content-media-uploader :deep(.el-upload),
.content-media-uploader :deep(.el-upload-dragger) {
  width: 100%;
}

.content-media-uploader :deep(.el-upload-dragger) {
  padding: var(--xi-component-content-gap-lg);
  background: var(--xi-semantic-color-background-page);
  border-color: var(--xi-semantic-color-border-default);
  border-radius: var(--xi-component-content-radius-card);
}

.upload-dropzone-copy,
.selected-file > div,
.upload-progress > div {
  gap: var(--xi-component-content-gap-xxs);
}

.upload-dropzone-copy span,
.selected-file span,
.upload-progress span {
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.selected-file {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand-subtle);
  border-radius: var(--xi-component-content-radius-control);
}

.selected-file > div {
  min-width: 0;
}

.selected-file strong,
.selected-file span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.upload-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 var(--xi-component-content-gap-md);
}

.upload-fields :deep(.el-form-item) {
  margin-bottom: 0;
}

.upload-progress {
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
}

.upload-actions {
  display: flex;
  align-items: center;
  gap: var(--xi-component-content-gap-sm);
}
</style>
