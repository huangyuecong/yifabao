<script setup lang="ts">
import type { LibraryItem } from '@/api/agent/content'
import { computed, ref } from 'vue'
import ContentLibraryMedia from './ContentLibraryMedia.vue'
import { isProductResource } from './contentResourceCatalog'
import {
  imageReferenceCount as countImageReferences,
  productVisualReferenceResources,
} from './imageReferenceLimits'
import ImageReferenceUploader from './ImageReferenceUploader.vue'

export interface ImageGenerationValue {
  image_prompt: string
  image_scene: string
  image_style: string
  image_ratio: string
  image_size: string
  reference_artifact_ids: string[]
  reference_asset_ids: string[]
  knowledge_resource_ids: string[]
}

interface SelectOption {
  label: string
  value: string
}

const props = defineProps<{
  taskId: string
  modelValue: ImageGenerationValue
  resources: LibraryItem[]
  ratioOptions: SelectOption[]
  sizeOptions: SelectOption[]
  allowCustomRatio: boolean
  allowCustomSize: boolean
  referenceLimit?: number
  referenceError?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: ImageGenerationValue]
  'resourcesUploaded': [items: LibraryItem[]]
}>()

const referenceLimitError = ref('')
const sceneOptions = ['产品静物展示', '品牌文化空间', '节日礼赠', '商务宴请', '生活方式', '自然风景']
const visualStyleOptions = ['写实摄影', '品牌宣传', '国风雅致', '简约高级', '故事感', '插画']

function pixelLabel(value: string) {
  if (/^[234]K$/i.test(value)) {
    return value.toUpperCase()
  }
  return `${value.replace(/[x×]/i, '×')} px`
}

const visibleSizeOptions = computed<SelectOption[]>(() => {
  const values = props.sizeOptions.map(option => option.value)
  const current = props.modelValue.image_size.trim()
  const options = current && !values.includes(current) ? [current, ...values] : values
  return options.map(value => ({ value, label: pixelLabel(value) }))
})

const referenceImageCount = computed(() => countImageReferences(props.resources, {
  knowledgeResourceIds: props.modelValue.knowledge_resource_ids,
  libraryAssetIds: props.modelValue.reference_asset_ids,
  artifactIds: props.modelValue.reference_artifact_ids,
}))
const selectedProductResources = computed(() => props.modelValue.knowledge_resource_ids
  .map(id => props.resources.find(item => item.id === id || item.resource_entry_id === id))
  .filter((item): item is LibraryItem => Boolean(item && isProductResource(item))),
)
const selectedProductVisualReferences = computed(() => productVisualReferenceResources(
  props.resources,
  props.modelValue.knowledge_resource_ids,
))
const referenceSelectionLimit = computed(() => {
  const value = Number(props.referenceLimit)
  return Number.isInteger(value) && value >= 0 ? value : undefined
})
const referenceRemainingCount = computed(() => Math.max(
  0,
  (referenceSelectionLimit.value ?? 0) - referenceImageCount.value,
))
const selectedReferenceResources = computed(() => props.modelValue.reference_asset_ids
  .map(id => props.resources.find(item => item.id === id))
  .filter((item): item is LibraryItem => Boolean(item)),
)
const referenceValidationMessage = computed(() => {
  if (props.referenceError) {
    return props.referenceError
  }
  if (referenceSelectionLimit.value == null) {
    return '当前图片模型未声明参考图片上限，参考图片选择已禁用。'
  }
  if (referenceImageCount.value > referenceSelectionLimit.value) {
    return `参考图片最多选择 ${referenceSelectionLimit.value} 张，请先移除多余图片。`
  }
  return referenceLimitError.value
})

function update<K extends keyof ImageGenerationValue>(key: K, value: ImageGenerationValue[K]) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}

function updatePixelSize(value: unknown) {
  update('image_size', String(value || '').trim().replace(/[X×]/g, 'x'))
}

function updateReferenceAssets(ids: string[]) {
  const nextCount = countImageReferences(props.resources, {
    knowledgeResourceIds: props.modelValue.knowledge_resource_ids,
    libraryAssetIds: ids,
    artifactIds: props.modelValue.reference_artifact_ids,
  })
  if (referenceSelectionLimit.value == null && nextCount > referenceImageCount.value) {
    referenceLimitError.value = '当前图片模型未声明参考图片上限，已阻止本次添加。'
    return
  }
  if (
    referenceSelectionLimit.value != null
    && nextCount > referenceSelectionLimit.value
    && nextCount >= referenceImageCount.value
  ) {
    referenceLimitError.value
      = `参考图片最多选择 ${referenceSelectionLimit.value} 张，请先移除已有图片再添加。`
    return
  }
  referenceLimitError.value = ''
  update('reference_asset_ids', [...new Set(ids)])
}

function handleLocalReferencesUploaded(items: LibraryItem[]) {
  emit('resourcesUploaded', items)
  updateReferenceAssets([
    ...props.modelValue.reference_asset_ids,
    ...items.map(item => item.id),
  ])
}

function removeLocalReference(id: string) {
  updateReferenceAssets(props.modelValue.reference_asset_ids.filter(itemId => itemId !== id))
}

function clearOptionalVisualSettings() {
  referenceLimitError.value = ''
  emit('update:modelValue', {
    ...props.modelValue,
    image_scene: '',
    image_style: '',
    image_ratio: '',
    image_size: '',
    reference_artifact_ids: [],
  })
}
</script>

<template>
  <div class="image-generation-controls">
    <ElFormItem label="图片主题 / 提示词（可选）" class="generation-instruction">
      <ElInput
        :model-value="modelValue.image_prompt"
        placeholder="不填写时自动使用任务主题、所选产品或任务名称"
        @update:model-value="update('image_prompt', String($event))"
      />
    </ElFormItem>

    <div class="generation-instruction common-visual-settings">
      <div class="setting-heading">
        <div>
          <strong>常用画面设置</strong>
        </div>
        <ElButton link @click="clearOptionalVisualSettings">恢复模型默认</ElButton>
      </div>
      <div
        v-if="selectedProductResources.length"
        class="product-visual-references"
        aria-label="产品资料关联的视觉参考"
      >
        <div class="product-reference-copy">
          <strong>产品资料关联的产品图会自动作为参考图</strong>
          <span v-if="selectedProductVisualReferences.length">
            已识别 {{ selectedProductVisualReferences.length }} 张，提交时由服务端按产品资料自动解析，无需重复上传。
          </span>
          <span v-else>
            提交时由服务端按产品资料自动解析；当前列表未返回可预览的关联图，无需重复上传。
          </span>
        </div>
        <div v-if="selectedProductVisualReferences.length" class="product-reference-list">
          <article
            v-for="item in selectedProductVisualReferences"
            :key="item.resource_entry_id || item.id"
            :title="item.name"
          >
            <ContentLibraryMedia :item="item" :alt="item.name" compact />
            <span>{{ item.name }}</span>
          </article>
        </div>
      </div>
      <div class="setting-grid">
        <ElFormItem label="场景（可选）">
          <ElSelect
            :model-value="modelValue.image_scene"
            clearable
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入场景；可留空"
            @update:model-value="update('image_scene', String($event || ''))"
          >
            <ElOption
              v-for="item in sceneOptions"
              :key="item"
              :label="item"
              :value="item"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="画面风格（可选）">
          <ElSelect
            :model-value="modelValue.image_style"
            clearable
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入风格；可留空"
            @update:model-value="update('image_style', String($event || ''))"
          >
            <ElOption
              v-for="item in visualStyleOptions"
              :key="item"
              :label="item"
              :value="item"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="画面比例（可选）">
          <ElSelect
            v-if="ratioOptions.length || allowCustomRatio"
            :model-value="modelValue.image_ratio"
            clearable
            filterable
            :allow-create="allowCustomRatio"
            :default-first-option="allowCustomRatio"
            placeholder="模型自动"
            @update:model-value="update('image_ratio', String($event || ''))"
          >
            <ElOption
              v-for="option in ratioOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </ElSelect>
          <ElInput v-else disabled placeholder="当前服务自动决定比例" />
        </ElFormItem>
        <ElFormItem label="输出分辨率（可选）">
          <ElSelect
            v-if="visibleSizeOptions.length || allowCustomSize"
            :model-value="modelValue.image_size"
            clearable
            filterable
            :allow-create="allowCustomSize"
            :default-first-option="allowCustomSize"
            placeholder="选择 2K / 3K / 4K，或输入宽x高"
            @update:model-value="updatePixelSize"
          >
            <ElOption
              v-for="option in visibleSizeOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </ElSelect>
          <ElInput v-else disabled placeholder="当前服务自动决定尺寸" />
        </ElFormItem>
        <ElFormItem label="本地参考图片（可选）" class="reference-setting">
          <div class="reference-count" aria-live="polite">
            参考图片 {{ referenceImageCount }}/{{ referenceSelectionLimit ?? '未声明' }}
          </div>
          <ImageReferenceUploader
            :task-id="taskId"
            :selected-items="selectedReferenceResources"
            :remaining="referenceRemainingCount"
            :limit="referenceSelectionLimit"
            :disabled="referenceSelectionLimit == null || referenceSelectionLimit === 0"
            @uploaded="handleLocalReferencesUploaded"
            @remove="removeLocalReference"
          />
          <p
            v-if="referenceValidationMessage"
            class="reference-limit-error"
            role="alert"
            aria-live="assertive"
          >
            {{ referenceValidationMessage }}
          </p>
        </ElFormItem>
      </div>
    </div>
  </div>
</template>

<style scoped>
.image-generation-controls {
  display: grid;
  grid-column: 1 / -1;
  width: 100%;
  min-width: 0;
}

.image-generation-controls > * {
  min-width: 0;
}

.image-generation-controls :deep(.el-form-item__content),
.image-generation-controls :deep(.el-input),
.image-generation-controls :deep(.el-select),
.image-generation-controls :deep(.resource-picker) {
  width: 100%;
  min-width: 0;
}

.reference-count {
  margin-bottom: var(--xi-component-content-gap-xs);
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.reference-limit-error {
  width: 100%;
  margin: var(--xi-component-content-gap-xs) 0 0;
  color: var(--el-color-danger);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.common-visual-settings {
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.product-visual-references {
  display: grid;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-sm);
  margin-bottom: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand-subtle);
  border-radius: var(--xi-component-content-radius-control);
}

.product-reference-copy {
  display: grid;
  gap: var(--xi-component-content-gap-xxs);
}

.product-reference-copy span,
.product-reference-list span {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.product-reference-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(112px, 1fr));
  gap: var(--xi-component-content-gap-xs);
}

.product-reference-list article {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-xxs);
}

.product-reference-list :deep(.library-media) {
  height: 72px;
}

.product-reference-list span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.setting-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-sm);
  margin-bottom: var(--xi-component-content-gap-sm);
}

.setting-heading > div {
  min-width: 0;
  display: grid;
  gap: var(--xi-component-content-gap-xxs);
}

.setting-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 var(--xi-component-content-gap-md);
}

.setting-grid :deep(.el-form-item) {
  margin-bottom: 0;
}

.reference-setting {
  grid-column: 1 / -1;
  margin-top: var(--xi-component-content-gap-sm);
}

@media (max-width: 900px) {
  .setting-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: var(--xi-component-content-gap-sm);
  }

  .setting-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
