<script setup lang="ts">
import type { LibraryItem, LibraryResourceKind } from '@/api/agent/content'
import { computed, ref, watch } from 'vue'
import {
  categoryLabel,
  findParentResource,
  resourceSearchText,
} from './contentResourceCatalog'
import ContentResourcePreview from './ContentResourcePreview.vue'

const props = withDefaults(defineProps<{
  modelValue: string[]
  resources: LibraryItem[]
  kinds: LibraryResourceKind[]
  title?: string
  description?: string
  buttonText?: string
  emptyText?: string
  multiple?: boolean
  preserveUnknown?: boolean
  disabled?: boolean
  maxSelections?: number
  maxSelectionMessage?: string
}>(), {
  title: '选择企业素材',
  description: '按分类浏览、搜索和预览企业资料库中的素材。',
  buttonText: '浏览素材库',
  emptyText: '当前分类暂无可用素材',
  multiple: true,
  preserveUnknown: true,
  disabled: false,
  maxSelections: undefined,
  maxSelectionMessage: '',
})

const emit = defineEmits<{
  'update:modelValue': [value: string[]]
}>()

const open = ref(false)
const search = ref('')
const category = ref('')
const page = ref(1)
const draftIds = ref<string[]>([])
const selectionError = ref('')
const pageSize = 20
const selectionLimit = computed(() => {
  const value = Number(props.maxSelections)
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : undefined
})

const catalogResources = computed(() => props.resources.filter(item =>
  props.kinds.includes(item.resource_kind)
  && Boolean(item.has_asset || item.download_url || item.storage_key),
))
const catalogIds = computed(() => new Set(catalogResources.value.map(item => item.id)))
const selectedResources = computed(() => props.modelValue
  .map(id => catalogResources.value.find(item => item.id === id))
  .filter((item): item is LibraryItem => Boolean(item)),
)
const categoryOptions = computed(() => [...new Map(catalogResources.value.map(item => [
  item.category,
  categoryLabel(item.category, item),
])).entries()].map(([value, label]) => ({ value, label })))
const filteredResources = computed(() => {
  const query = search.value.trim().toLocaleLowerCase('zh-CN')
  return catalogResources.value.filter((item) => {
    const matchesCategory = !category.value || item.category === category.value
    const matchesSearch = !query || resourceSearchText(item).includes(query)
    return matchesCategory && matchesSearch
  })
})
const pagedResources = computed(() => filteredResources.value.slice(
  (page.value - 1) * pageSize,
  page.value * pageSize,
))

function openPicker() {
  search.value = ''
  category.value = ''
  page.value = 1
  draftIds.value = props.modelValue.filter(id => catalogIds.value.has(id))
  selectionError.value
    = selectionLimit.value != null && draftIds.value.length > selectionLimit.value
      ? selectionLimitMessage()
      : ''
  open.value = true
}

function selectionLimitMessage() {
  return props.maxSelectionMessage
    || `最多选择 ${selectionLimit.value ?? 0} 项，请先移除已有选择再添加。`
}

function toggle(item: LibraryItem) {
  if (!props.multiple) {
    draftIds.value = [item.id]
    selectionError.value = ''
    return
  }
  if (draftIds.value.includes(item.id)) {
    draftIds.value = draftIds.value.filter(id => id !== item.id)
    selectionError.value = ''
    return
  }
  if (selectionLimit.value != null && draftIds.value.length >= selectionLimit.value) {
    selectionError.value = selectionLimitMessage()
    return
  }
  draftIds.value = [...draftIds.value, item.id]
  selectionError.value = ''
}

function confirm() {
  const preserved = props.preserveUnknown
    ? props.modelValue.filter(id => !catalogIds.value.has(id))
    : []
  const nextIds = [...preserved, ...draftIds.value]
  if (selectionLimit.value != null && nextIds.length > selectionLimit.value) {
    selectionError.value = selectionLimitMessage()
    return
  }
  emit('update:modelValue', nextIds)
  open.value = false
}

function remove(id: string) {
  selectionError.value = ''
  emit('update:modelValue', props.modelValue.filter(value => value !== id))
}

watch([search, category], () => {
  page.value = 1
})
</script>

<template>
  <div class="resource-picker">
    <div class="resource-picker-action">
      <ElButton :disabled="disabled || !catalogResources.length" @click="openPicker">
        {{ buttonText }}
      </ElButton>
      <span v-if="selectedResources.length || selectionLimit != null">
        已选 {{ selectedResources.length }}<template v-if="selectionLimit != null"> / {{ selectionLimit }}</template> 项
      </span>
    </div>
    <div v-if="selectedResources.length" class="selected-resource-list">
      <article v-for="item in selectedResources.slice(0, 8)" :key="item.id">
        <ContentResourcePreview :item="item" :preview="false" />
        <div>
          <strong>{{ item.name }}</strong>
          <span>{{ categoryLabel(item.category, item) }}</span>
        </div>
        <ElButton link aria-label="移除素材" @click="remove(item.id)">移除</ElButton>
      </article>
      <span v-if="selectedResources.length > 8" class="more-selection">
        另有 {{ selectedResources.length - 8 }} 项已选素材
      </span>
    </div>

    <ElDialog
      v-model="open"
      :title="title"
      width="min(1120px, 94vw)"
      destroy-on-close
      append-to-body
    >
      <p class="dialog-description">{{ description }}</p>
      <p
        v-if="selectionError"
        class="resource-limit-error"
        role="alert"
        aria-live="assertive"
      >
        {{ selectionError }}
      </p>
      <div class="resource-toolbar">
        <ElInput v-model="search" clearable placeholder="搜索名称、标签或资料内容" />
        <ElSelect
          v-model="category"
          clearable
          filterable
          placeholder="全部分类"
        >
          <ElOption
            v-for="option in categoryOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </ElSelect>
      </div>
      <ElEmpty v-if="!filteredResources.length" :description="emptyText" />
      <div v-else class="resource-grid">
        <article
          v-for="item in pagedResources"
          :key="item.id"
          class="resource-card"
          :class="{ selected: draftIds.includes(item.id) }"
          @click="toggle(item)"
        >
          <ContentResourcePreview class="resource-card-preview" :item="item" />
          <div class="resource-card-heading">
            <ElCheckbox :model-value="draftIds.includes(item.id)" @click.stop @change="toggle(item)" />
            <strong>{{ item.name }}</strong>
          </div>
          <div class="resource-card-meta">
            <ElTag size="small" effect="plain">{{ categoryLabel(item.category, item) }}</ElTag>
            <span v-if="findParentResource(item, resources)">
              {{ findParentResource(item, resources)?.name }}
            </span>
          </div>
        </article>
      </div>
      <ElPagination
        v-if="filteredResources.length > pageSize"
        v-model:current-page="page"
        class="resource-pagination"
        background
        layout="prev, pager, next"
        :page-size="pageSize"
        :total="filteredResources.length"
      />
      <template #footer>
        <div class="dialog-footer">
          <span>本次已选 {{ draftIds.length }} 项</span>
          <div>
            <ElButton @click="open = false">取消</ElButton>
            <ElButton type="primary" @click="confirm">确认选择</ElButton>
          </div>
        </div>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.resource-picker,
.selected-resource-list {
  display: grid;
  width: 100%;
  gap: var(--xi-component-content-gap-sm);
}

.resource-picker-action,
.resource-toolbar,
.dialog-footer,
.resource-card-heading,
.resource-card-meta {
  display: flex;
  align-items: center;
  gap: var(--xi-component-content-gap-sm);
}

.resource-picker-action > span,
.dialog-description,
.resource-card-meta,
.more-selection,
.dialog-footer > span {
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.resource-limit-error {
  padding: var(--xi-component-content-gap-xs) var(--xi-component-content-gap-sm);
  margin: 0 0 var(--xi-component-content-gap-sm);
  color: var(--el-color-danger);
  font-size: var(--xi-semantic-typography-caption-font-size);
  background: var(--el-color-danger-light-9);
  border: 1px solid var(--el-color-danger-light-7);
  border-radius: var(--xi-component-content-radius-control);
}

.selected-resource-list {
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
}

.selected-resource-list article {
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--xi-component-content-gap-xs);
  padding: var(--xi-component-content-gap-xs);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand-subtle);
  border-radius: var(--xi-component-content-radius-control);
}

.selected-resource-list article > :first-child {
  height: 48px;
  min-height: 48px;
}

.selected-resource-list article > div:not(:first-child) {
  display: grid;
  min-width: 0;
}

.selected-resource-list strong,
.resource-card-heading strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.selected-resource-list span {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.dialog-description {
  margin: 0 0 var(--xi-component-content-gap-md);
}

.resource-toolbar {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(180px, 300px);
  margin-bottom: var(--xi-component-content-gap-md);
}

.resource-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--xi-component-content-gap-sm);
}

.resource-card {
  display: grid;
  gap: var(--xi-component-content-gap-xs);
  min-width: 0;
  padding: var(--xi-component-content-gap-sm);
  cursor: pointer;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.resource-card.selected {
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand);
}

.resource-card-preview {
  height: 132px;
}

.resource-card-heading {
  min-width: 0;
}

.resource-card-heading strong {
  flex: 1;
}

.resource-card-meta {
  justify-content: space-between;
  min-width: 0;
}

.resource-card-meta span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.resource-pagination {
  justify-content: center;
  margin-top: var(--xi-component-content-gap-md);
}

.dialog-footer {
  justify-content: space-between;
}
</style>
