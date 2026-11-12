<script setup lang="ts">
import type { LibraryItem } from '@/api/agent/content'
import { computed, ref, watch } from 'vue'
import {
  isProductResource,
  previewResource,
  productGroupLabel,
  relatedMedia,
  resourceSummary,
} from './contentResourceCatalog'
import ContentResourcePreview from './ContentResourcePreview.vue'

const props = withDefaults(defineProps<{
  modelValue: string
  productResourceId?: string
  resources: LibraryItem[]
  showPreview?: boolean
}>(), { showPreview: true })

const emit = defineEmits<{
  change: [name: string, resourceEntryId: string]
}>()

const open = ref(false)
const activeDetail = ref<LibraryItem>()

const products = computed(() => props.resources.filter(isProductResource))
const selectedProduct = computed(() => {
  const resourceId = props.productResourceId?.trim()
  const selectedById = resourceId
    ? products.value.find(item => [item.resource_entry_id, item.id].includes(resourceId))
    : undefined
  return selectedById || products.value.find(item => item.name === props.modelValue)
})
const selectedCover = computed(() => selectedProduct.value
  ? previewResource(selectedProduct.value, props.resources)
  : undefined,
)
const detailMedia = computed(() => activeDetail.value
  ? relatedMedia(activeDetail.value, props.resources)
  : [],
)
const cascaderOptions = computed(() => {
  const groups = new Map<string, LibraryItem[]>()
  for (const item of products.value) {
    const label = productGroupLabel(item, props.resources) || '其他产品'
    groups.set(label, [...(groups.get(label) ?? []), item])
  }
  return [...groups.entries()]
    .sort(([left], [right]) => left.localeCompare(right, 'zh-CN'))
    .map(([label, items]) => ({
      value: label,
      label,
      children: items
        .sort((left, right) => left.name.localeCompare(right.name, 'zh-CN'))
        .map(item => ({
          value: item.resource_entry_id || item.id,
          label: item.name,
        })),
    }))
})
const selectedValue = computed(() => selectedProduct.value
  ? selectedProduct.value.resource_entry_id || selectedProduct.value.id
  : '',
)

function factText(value: Record<string, unknown> | string) {
  if (typeof value === 'string') {
    return value
  }
  return Object.entries(value).map(([key, item]) => `${key}：${String(item)}`).join('；')
}

function openDetails() {
  activeDetail.value = selectedProduct.value || products.value[0]
  open.value = true
}

function choose(item: LibraryItem) {
  emit('change', item.name, item.resource_entry_id || item.id)
}

function selectProduct(value: unknown) {
  if (!value) {
    emit('change', '', '')
    return
  }
  const selectedId = Array.isArray(value) ? value.at(-1) : value
  const item = products.value.find(product =>
    [product.resource_entry_id, product.id].includes(String(selectedId)),
  )
  if (item) {
    choose(item)
  }
}

watch(selectedProduct, (item) => {
  if (open.value) {
    activeDetail.value = item
  }
})
</script>

<template>
  <div class="product-picker">
    <ElCascader
      :disabled="!products.length"
      :model-value="selectedValue"
      :options="cascaderOptions"
      :props="{ emitPath: false }"
      clearable
      filterable
      placeholder="请选择产品分类 / 产品名称"
      @change="selectProduct"
    />
    <article v-if="selectedProduct && showPreview !== false" class="selected-product">
      <ContentResourcePreview
        v-if="selectedCover"
        :key="selectedCover.id"
        class="selected-product-cover"
        :item="selectedCover"
        :preview="false"
      />
      <div>
        <strong>{{ selectedProduct.name }}</strong>
        <span>{{ productGroupLabel(selectedProduct, resources) }}</span>
        <p>{{ resourceSummary(selectedProduct) }}</p>
      </div>
      <ElButton link type="primary" @click="openDetails">查看产品资料</ElButton>
    </article>

    <ElDialog
      v-model="open"
      title="产品资料"
      width="min(920px, 94vw)"
      destroy-on-close
      append-to-body
    >
      <div class="product-detail">
        <template v-if="activeDetail">
          <div class="detail-heading">
            <div>
              <span>{{ productGroupLabel(activeDetail, resources) }}</span>
              <h4>{{ activeDetail.name }}</h4>
            </div>
            <ElTag type="success" effect="plain">当前已选</ElTag>
          </div>
          <div v-if="detailMedia.length" class="detail-media-grid">
            <ContentResourcePreview
              v-for="item in detailMedia.slice(0, 6)"
              :key="item.id"
              :item="item"
            />
          </div>
          <p class="detail-summary">{{ activeDetail.text_content || activeDetail.notes || '暂无产品说明' }}</p>
          <dl v-if="activeDetail.facts?.length" class="fact-list">
            <template v-for="(fact, index) in activeDetail.facts" :key="index">
              <dt>资料要点 {{ index + 1 }}</dt>
              <dd>{{ factText(fact) }}</dd>
            </template>
          </dl>
          <div v-if="activeDetail.tags?.length" class="detail-tags">
            <ElTag
              v-for="tag in activeDetail.tags"
              :key="tag"
              size="small"
              effect="plain"
            >
              {{ tag }}
            </ElTag>
          </div>
        </template>
        <ElEmpty v-else description="请选择产品查看资料" />
      </div>
    </ElDialog>
  </div>
</template>

<style scoped>
.product-picker,
.selected-product,
.product-detail {
  display: grid;
}

.product-picker {
  width: 100%;
  gap: var(--xi-component-content-gap-xs);
}

.selected-product p,
.detail-summary,
.fact-list dd {
  margin: 0;
  color: var(--xi-semantic-color-text-secondary);
}

.selected-product {
  grid-template-columns: 72px minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand-subtle);
  border-radius: var(--xi-component-content-radius-control);
}

.selected-product-cover {
  height: 64px;
}

.selected-product > div:not(.selected-product-cover) {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-xxs);
}

.selected-product span {
  color: var(--xi-semantic-color-text-brand);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.selected-product p {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.product-detail {
  align-content: start;
  gap: var(--xi-component-content-gap-md);
}

.detail-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-sm);
}

.detail-heading span {
  color: var(--xi-semantic-color-text-tertiary);
}

.product-detail {
  padding: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.detail-heading h4 {
  margin: var(--xi-component-content-gap-xxs) 0 0;
  font-size: var(--xi-semantic-typography-heading2-font-size);
}

.detail-media-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--xi-component-content-gap-xs);
}

.detail-media-grid > * {
  height: 104px;
}

.detail-summary {
  white-space: pre-wrap;
}

.fact-list {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: var(--xi-component-content-gap-xs) var(--xi-component-content-gap-sm);
  margin: 0;
}

.fact-list dt {
  color: var(--xi-semantic-color-text-primary);
  font-weight: var(--xi-semantic-typography-heading3-weight-medium);
}

.detail-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-xs);
}
</style>
