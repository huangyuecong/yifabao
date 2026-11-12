<script setup lang="ts">
import type { LibraryItem, LibraryResourceKind } from '@/api/agent/content'
import { ElMessage } from 'element-plus'
import { computed, nextTick, ref, watch } from 'vue'
import { contentApi } from '@/api/agent/content'
import ContentKnowledgeSelectionList from './ContentKnowledgeSelectionList.vue'
import {
  categoryLabel,
  productGroupLabel,
  relatedMedia,
  previewResource as resolvePreviewResource,
  resourceKindLabel,
  resourceSearchText,
  resourceSummary,
} from './contentResourceCatalog'
import ContentResourcePreview from './ContentResourcePreview.vue'
import { friendlyContentError } from './contentWorkflow'

const props = withDefaults(
  defineProps<{
    modelValue: string[]
    resources: LibraryItem[]
    title?: string
    description?: string
    buttonText?: string
    emptyText?: string
    preserveUnknown?: boolean
    maxSelection?: number
    showSelectionSummary?: boolean
    selectionValidator?: (ids: string[]) => string
  }>(),
  {
    title: '选择知识库资料与素材',
    description: '可多选产品资料、企业文化、品牌资料、Logo、图片和内容规范等已启用内容。',
    buttonText: '浏览知识库',
    emptyText: '知识库中暂无可选资料或素材',
    preserveUnknown: true,
    maxSelection: 20,
    showSelectionSummary: true,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string[]]
  'resourcesRefreshed': [value: LibraryItem[]]
}>()

const open = ref(false)
const refreshing = ref(false)
const refreshError = ref('')
const refreshedResources = ref<LibraryItem[]>()
let refreshSequence = 0
const search = ref('')
const catalogNode = ref<string>()
const page = ref(1)
const draftIds = ref<string[]>([])
const selectionError = ref('')
const pageSize = 16

type CatalogGroupKey = 'product' | 'document' | 'image' | 'video'

interface CatalogGroupDefinition {
  key: CatalogGroupKey
  label: string
  kinds: readonly LibraryResourceKind[]
}

const catalogGroups: readonly CatalogGroupDefinition[] = [
  { key: 'product', label: '产品资料', kinds: ['product'] },
  { key: 'document', label: '企业文档', kinds: ['document', 'spec'] },
  { key: 'image', label: '图片素材', kinds: ['image', 'logo'] },
  { key: 'video', label: '视频素材', kinds: ['video', 'audio'] },
]
const cascaderProps = {
  checkStrictly: true,
  emitPath: false,
  expandTrigger: 'hover' as const,
}

function resourceId(item: LibraryItem) {
  return item.resource_entry_id || item.id
}

const catalogResources = computed(() => {
  const unique = new Map<string, LibraryItem>()
  for (const item of refreshedResources.value ?? props.resources) {
    if (item.active === false) {
      continue
    }
    const id = resourceId(item)
    if (!unique.has(id)) {
      unique.set(id, item)
    }
  }
  return [...unique.values()]
})
const resourceById = computed(
  () => new Map(catalogResources.value.map(item => [resourceId(item), item])),
)
const catalogIds = computed(() => new Set(resourceById.value.keys()))
const unavailableSelectionCount = computed(
  () => props.modelValue.filter(id => !catalogIds.value.has(id)).length,
)
const draftSelectionCount = computed(
  () => draftIds.value.length + (props.preserveUnknown ? unavailableSelectionCount.value : 0),
)

function catalogGroupFor(item: LibraryItem) {
  return catalogGroups.find(group => group.kinds.includes(item.resource_kind))
}

function catalogFacetLabel(item: LibraryItem, group: CatalogGroupDefinition) {
  if (group.key === 'product') {
    const label = productGroupLabel(item, catalogResources.value)
    return label === '产品资料' ? '未标注系列' : label
  }
  return categoryLabel(item.category, item)
}

function groupFilterValue(group: CatalogGroupDefinition) {
  return `group:${group.key}`
}

function facetFilterValue(group: CatalogGroupDefinition, facet: string) {
  return `facet:${group.key}:${encodeURIComponent(facet)}`
}

const catalogOptions = computed(() =>
  catalogGroups.map((group) => {
    const groupResources = catalogResources.value.filter(item =>
      group.kinds.includes(item.resource_kind),
    )
    const facets = new Map<string, number>()
    for (const item of groupResources) {
      const label = catalogFacetLabel(item, group)
      facets.set(label, (facets.get(label) ?? 0) + 1)
    }
    return {
      value: groupFilterValue(group),
      label: `${group.label}（${groupResources.length}）`,
      disabled: groupResources.length === 0,
      children: [...facets.entries()]
        .sort((left, right) => left[0].localeCompare(right[0], 'zh-CN'))
        .map(([label, count]) => ({
          value: facetFilterValue(group, label),
          label: `${label}（${count}）`,
        })),
    }
  }),
)

function matchesCatalogNode(item: LibraryItem) {
  const selected = catalogNode.value
  if (!selected) {
    return true
  }
  const group = catalogGroupFor(item)
  if (!group) {
    return false
  }
  if (selected === groupFilterValue(group)) {
    return true
  }
  const facetPrefix = `facet:${group.key}:`
  if (!selected.startsWith(facetPrefix)) {
    return false
  }
  return catalogFacetLabel(item, group) === decodeURIComponent(selected.slice(facetPrefix.length))
}

const filteredResources = computed(() => {
  const query = search.value.trim().toLocaleLowerCase('zh-CN')
  return catalogResources.value.filter((item) => {
    const matchesCategory = matchesCatalogNode(item)
    const matchesSearch = !query || resourceSearchText(item).includes(query)
    return matchesCategory && matchesSearch
  })
})
const pagedResources = computed(() =>
  filteredResources.value.slice((page.value - 1) * pageSize, page.value * pageSize),
)

const mediaInfoByResourceId = computed(() => new Map(catalogResources.value.map(item => [
  resourceId(item),
  {
    preview: resolvePreviewResource(item, catalogResources.value),
    count: relatedMedia(item, catalogResources.value).length,
  },
])))

function hasCardPreview(item: LibraryItem) {
  return Boolean(mediaInfoByResourceId.value.get(resourceId(item))?.preview)
}

function cardPreview(item: LibraryItem) {
  return mediaInfoByResourceId.value.get(resourceId(item))?.preview ?? item
}

function cardRelatedMediaCount(item: LibraryItem) {
  return mediaInfoByResourceId.value.get(resourceId(item))?.count ?? 0
}

function openPicker() {
  search.value = ''
  catalogNode.value = undefined
  page.value = 1
  draftIds.value = props.modelValue.filter(id => catalogIds.value.has(id))
  selectionError.value = ''
  open.value = true
  void refreshCatalog()
}

async function refreshCatalog() {
  const sequence = ++refreshSequence
  refreshing.value = true
  refreshError.value = ''
  try {
    const latest = await contentApi.listLibrary()
    if (sequence !== refreshSequence || !open.value) {
      return
    }
    refreshedResources.value = latest
    emit('resourcesRefreshed', latest)
    await nextTick()
    draftIds.value = props.modelValue.filter(id => catalogIds.value.has(id))
  }
  catch (error) {
    if (sequence === refreshSequence && open.value) {
      refreshError.value = friendlyContentError(error, '知识库刷新失败，请重试')
    }
  }
  finally {
    if (sequence === refreshSequence) {
      refreshing.value = false
    }
  }
}

function nextSelection(ids: string[]) {
  const preserved = props.preserveUnknown
    ? props.modelValue.filter(id => !catalogIds.value.has(id))
    : []
  return [...new Set([...preserved, ...ids])]
}

function selectionValidationMessage(ids: string[]) {
  return props.selectionValidator?.(nextSelection(ids)) || ''
}

function toggle(item: LibraryItem) {
  const id = resourceId(item)
  if (draftIds.value.includes(id)) {
    const nextIds = draftIds.value.filter(value => value !== id)
    const message = selectionValidationMessage(nextIds)
    if (message) {
      selectionError.value = message
      return
    }
    draftIds.value = nextIds
    selectionError.value = ''
    return
  }
  if (draftSelectionCount.value >= props.maxSelection) {
    ElMessage.warning(`最多选择 ${props.maxSelection} 项知识库资料与素材`)
    return
  }
  const nextIds = [...draftIds.value, id]
  const message = selectionValidationMessage(nextIds)
  if (message) {
    selectionError.value = message
    return
  }
  draftIds.value = nextIds
  selectionError.value = ''
}

function confirm() {
  if (refreshing.value || refreshError.value) {
    return
  }
  const nextIds = nextSelection(draftIds.value)
  const message = props.selectionValidator?.(nextIds) || ''
  if (message) {
    selectionError.value = message
    return
  }
  emit('update:modelValue', nextIds)
  open.value = false
}

function remove(id: string) {
  emit(
    'update:modelValue',
    props.modelValue.filter(value => value !== id),
  )
}

watch([search, catalogNode], () => {
  page.value = 1
})
</script>

<template>
  <div class="knowledge-picker">
    <div class="knowledge-picker-action">
      <ElButton @click="openPicker">
        {{ buttonText }}
      </ElButton>
    </div>

    <ContentKnowledgeSelectionList
      v-if="showSelectionSummary"
      :model-value="modelValue"
      :resources="resources"
      :preserve-unknown="preserveUnknown"
      @remove="remove"
    />

    <ElDialog
      v-model="open"
      :title="title"
      class="content-knowledge-dialog"
      width="min(960px, calc(100vw - 32px))"
      top="32px"
      destroy-on-close
      append-to-body
    >
      <p class="dialog-description">{{ description }}</p>
      <ElAlert
        v-if="refreshError"
        :title="refreshError"
        type="error"
        :closable="false"
        show-icon
      >
        <ElButton link type="primary" @click="refreshCatalog">重新加载</ElButton>
      </ElAlert>
      <p
        v-if="selectionError"
        class="knowledge-selection-error"
        role="alert"
        aria-live="assertive"
      >
        {{ selectionError }}
      </p>
      <div class="knowledge-toolbar">
        <ElInput v-model="search" clearable placeholder="搜索名称、标签或资料内容" />
        <ElCascader
          v-model="catalogNode"
          class="knowledge-cascader"
          :options="catalogOptions"
          :props="cascaderProps"
          clearable
          filterable
          :show-all-levels="false"
          placeholder="全部资料与素材"
        />
      </div>

      <div class="catalog-result-heading">
        <span>知识库内容</span>
        <small>共 {{ filteredResources.length }} 项</small>
      </div>
      <ElSkeleton
        v-if="refreshing"
        :rows="5"
        animated
        aria-label="正在刷新知识库"
      />
      <ElEmpty v-else-if="refreshError" description="暂时无法获取最新知识库" />
      <ElEmpty v-else-if="!filteredResources.length" :description="emptyText" />
      <div v-else class="knowledge-grid">
        <article
          v-for="item in pagedResources"
          :key="resourceId(item)"
          class="knowledge-card"
          :class="{ selected: draftIds.includes(resourceId(item)) }"
          role="checkbox"
          :aria-checked="draftIds.includes(resourceId(item))"
          tabindex="0"
          @click="toggle(item)"
          @keydown.enter.prevent="toggle(item)"
          @keydown.space.prevent="toggle(item)"
        >
          <div v-if="hasCardPreview(item)" class="knowledge-card-preview">
            <ContentResourcePreview
              class="knowledge-card-preview-media"
              :item="cardPreview(item)"
              :preview="false"
            />
            <span v-if="cardRelatedMediaCount(item) > 1" class="knowledge-card-media-count">
              {{ cardRelatedMediaCount(item) }} 项关联素材
            </span>
          </div>
          <div v-else class="knowledge-text-preview">
            <span>{{ resourceKindLabel(item.resource_kind) }}</span>
            <p>{{ resourceSummary(item) }}</p>
          </div>
          <div class="knowledge-card-heading">
            <ElCheckbox
              :model-value="draftIds.includes(resourceId(item))"
              :aria-label="`选择${item.name}`"
              @click.stop
              @change="toggle(item)"
            />
            <strong>{{ item.name }}</strong>
          </div>
          <div class="knowledge-card-meta">
            <ElTag size="small" effect="plain">{{ categoryLabel(item.category, item) }}</ElTag>
          </div>
        </article>
      </div>
      <ElPagination
        v-if="!refreshing && !refreshError && filteredResources.length > pageSize"
        v-model:current-page="page"
        class="knowledge-pagination"
        background
        layout="prev, pager, next"
        :page-size="pageSize"
        :total="filteredResources.length"
      />

      <template #footer>
        <div class="dialog-footer">
          <span>本次已选 {{ draftSelectionCount }} / {{ props.maxSelection }} 项</span>
          <div>
            <ElButton @click="open = false">取消</ElButton>
            <ElButton type="primary" :disabled="refreshing || Boolean(refreshError)" @click="confirm">确认选择</ElButton>
          </div>
        </div>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.knowledge-picker {
  display: grid;
  width: 100%;
  gap: var(--xi-component-content-gap-sm);
}

.knowledge-picker-action,
.knowledge-card-heading,
.knowledge-card-meta,
.catalog-result-heading,
.dialog-footer {
  display: flex;
  align-items: center;
  gap: var(--xi-component-content-gap-sm);
}

.dialog-description,
.catalog-result-heading small,
.dialog-footer > span {
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.knowledge-card-meta span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dialog-description {
  margin: 0;
}

.dialog-description {
  margin-bottom: var(--xi-component-content-gap-md);
}

.knowledge-selection-error {
  padding: var(--xi-component-content-gap-xs) var(--xi-component-content-gap-sm);
  margin: calc(var(--xi-component-content-gap-sm) * -1) 0 var(--xi-component-content-gap-sm);
  color: var(--el-color-danger);
  font-size: var(--xi-semantic-typography-caption-font-size);
  background: var(--el-color-danger-light-9);
  border: 1px solid var(--el-color-danger-light-7);
  border-radius: var(--xi-component-content-radius-control);
}

.knowledge-toolbar {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(240px, 360px);
  gap: var(--xi-component-content-gap-sm);
  margin-bottom: var(--xi-component-content-gap-md);
}

.knowledge-cascader {
  width: 100%;
}

.catalog-result-heading,
.dialog-footer {
  justify-content: space-between;
}

.dialog-footer {
  width: 100%;
  flex-wrap: wrap;
}

.dialog-footer > div {
  display: flex;
  gap: var(--xi-component-content-gap-xs);
}

.catalog-result-heading {
  margin-bottom: var(--xi-component-content-gap-sm);
}

.knowledge-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--xi-component-content-gap-md);
}

.knowledge-card {
  display: grid;
  min-width: 0;
  min-height: 238px;
  align-content: start;
  grid-template-rows: 140px 32px auto;
  gap: var(--xi-component-content-gap-xs);
  padding: var(--xi-component-content-gap-sm);
  cursor: pointer;
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border-strong);
  border-radius: var(--xi-component-content-radius-card);
  outline: none;
  transition:
    border-color 0.2s ease,
    box-shadow 0.2s ease;
}

.knowledge-card:hover,
.knowledge-card:focus-visible {
  border: var(--xi-component-content-border-brand);
  box-shadow: var(--xi-component-content-focus-ring);
}

.knowledge-card:focus-visible {
  box-shadow: var(--xi-component-content-focus-ring);
}

.knowledge-card.selected {
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand);
}

.knowledge-card-preview,
.knowledge-text-preview {
  height: 140px;
}

.knowledge-card-preview {
  position: relative;
  overflow: hidden;
}

.knowledge-card-preview-media {
  height: 100%;
}

.knowledge-card-media-count {
  position: absolute;
  right: var(--xi-component-content-gap-xs);
  bottom: var(--xi-component-content-gap-xs);
  padding: var(--xi-component-content-gap-xxs) var(--xi-component-content-gap-xs);
  color: var(--xi-semantic-color-text-brand);
  font-size: var(--xi-semantic-typography-caption-font-size);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border-brand);
  border-radius: var(--xi-component-content-radius-control);
}

.knowledge-text-preview {
  display: grid;
  align-content: center;
  gap: var(--xi-component-content-gap-xs);
  padding: var(--xi-component-content-gap-sm);
  color: var(--xi-semantic-color-text-secondary);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
}

.knowledge-text-preview span {
  color: var(--xi-semantic-color-text-brand);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.knowledge-text-preview p {
  display: -webkit-box;
  overflow: hidden;
  margin: 0;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.knowledge-card-heading,
.knowledge-card-meta {
  min-width: 0;
}

.knowledge-card-heading {
  height: 32px;
  align-items: center;
  overflow: hidden;
  line-height: 32px;
}

.knowledge-card-heading strong {
  flex: 1;
  display: block;
  min-width: 0;
  overflow: hidden;
  line-height: 32px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.knowledge-card-heading :deep(.el-checkbox) {
  flex: 0 0 auto;
  height: 24px;
  margin: 0;
}

.knowledge-card-meta {
  align-self: end;
  justify-content: flex-start;
  padding-top: var(--xi-component-content-gap-xs);
  border-top: var(--xi-component-content-border);
}

.knowledge-pagination {
  justify-content: center;
  margin-top: var(--xi-component-content-gap-md);
}

@media (max-width: 900px) {
  .knowledge-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .knowledge-toolbar,
  .knowledge-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .knowledge-card {
    grid-template-columns: 96px minmax(0, 1fr);
    grid-template-rows: minmax(40px, auto) auto;
    min-height: auto;
  }

  .knowledge-card-preview,
  .knowledge-text-preview {
    height: 88px;
    grid-row: span 2;
  }

  .knowledge-text-preview {
    gap: var(--xi-component-content-gap-xxs);
    padding: var(--xi-component-content-gap-xs);
  }

  .knowledge-text-preview p {
    line-height: 20px;
    -webkit-line-clamp: 2;
  }
}
</style>
