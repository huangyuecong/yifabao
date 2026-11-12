<script setup lang="ts">
import type {
  LibraryCategory,
  LibraryDataLevel,
  LibraryItem,
  LibraryItemDetail,
  LibraryResourceKind,
} from '@/api/agent/content'
import {
  Box,
  Collection,
  Document,
  Grid,
  Headset,
  List,
  MoreFilled,
  Picture,
  Search,
  VideoPlay,
} from '@element-plus/icons-vue'
import { computed, ref, watch } from 'vue'
import { contentApi } from '@/api/agent/content'
import ContentKnowledgeSync from './ContentKnowledgeSync.vue'
import ContentLibraryMedia from './ContentLibraryMedia.vue'
import {
  previewResource as resolvePreviewResource,
  relatedMedia as resolveRelatedMedia,
} from './contentResourceCatalog'
import { formatDate } from './contentWorkflow'
import WbPagination from './WbPagination.vue'

interface CatalogNode {
  id: string
  label: string
  count: number
  kind?: LibraryResourceKind
  facetType?: 'category' | 'series'
  facetValue?: string
  children?: CatalogNode[]
}

const props = defineProps<{
  resources: LibraryItem[]
  categories: LibraryCategory[]
  loading?: boolean
  canEdit?: (item: LibraryItem) => boolean
}>()

const emit = defineEmits<{
  create: []
  edit: [item: LibraryItem]
  delete: [item: LibraryItem]
}>()

const query = ref('')
const kindFilter = ref<LibraryResourceKind | ''>('')
const categoryFilter = ref('')
const dataLevelFilter = ref<LibraryDataLevel | ''>('')
const selectedTreeId = ref('all')
const viewMode = ref<'card' | 'list'>('card')
const currentPage = ref(1)
const pageSize = ref(24)
const detailOpen = ref(false)
const selectedResourceId = ref('')
const detailMediaId = ref('')
const detailResource = ref<LibraryItemDetail>()
const detailLoading = ref(false)
const detailError = ref('')

const resourceKindOptions: Array<{ value: LibraryResourceKind, label: string }> = [
  { value: 'product', label: '产品资料' },
  { value: 'document', label: '企业文档' },
  { value: 'spec', label: '内容规范' },
  { value: 'logo', label: '品牌标识' },
  { value: 'image', label: '图片素材' },
  { value: 'video', label: '视频素材' },
  { value: 'audio', label: '音频素材' },
]

const categoryLabels: Record<string, string> = {
  brand: '品牌资料',
  brand_image: '品牌图片',
  brand_logo: '品牌标识',
  culture: '企业文化',
  material: '宣传素材',
  enterprise: '企业资料',
  product: '产品资料',
  product_image: '产品图片',
  history: '发展历程',
  public_welfare: '公益与社会责任',
  news: '新闻与活动',
  story: '品牌故事',
  positioning: '品牌定位',
  marketing: '宣传资料',
  visual: '视觉素材',
  logo: '品牌标识',
  image: '图片素材',
  video: '视频素材',
  audio: '音频素材',
  spec: '内容规范',
}

const dataLevelLabels: Record<string, string> = {
  public: '公开',
  internal: '内部',
  restricted: '受限',
}

function metadataRecord(item: LibraryItem) {
  return item.metadata ?? {}
}

function metadataString(item: LibraryItem, key: string) {
  const value = metadataRecord(item)[key]
  return typeof value === 'string' ? value.trim() : ''
}

function metadataStrings(item: LibraryItem, key: string) {
  const value = metadataRecord(item)[key]
  return Array.isArray(value) ? value.filter(entry => typeof entry === 'string') as string[] : []
}

function serviceCategoryLabel(value: string) {
  const category = props.categories.find(item => item.name === value || item.id === value)
  const record = category as unknown as Record<string, unknown> | undefined
  const supplied = record?.display_name ?? record?.label ?? record?.title
  if (typeof supplied === 'string' && supplied.trim()) {
    return supplied.trim()
  }
  return category?.name !== category?.id && /[\u3400-\u9FFF]/u.test(category?.name ?? '')
    ? category?.name ?? ''
    : ''
}

function categoryLabel(value: string) {
  const normalized = value.trim()
  if (!normalized) {
    return '未分类'
  }
  const supplied = serviceCategoryLabel(normalized)
  if (supplied) {
    return supplied
  }
  if (categoryLabels[normalized]) {
    return categoryLabels[normalized]
  }
  return normalized
}

function resourceKindLabel(value: LibraryResourceKind) {
  return resourceKindOptions.find(item => item.value === value)?.label ?? '其他资源'
}

function dataLevelLabel(value: LibraryDataLevel) {
  return dataLevelLabels[value] ?? '等级待确认'
}

function shortDate(value?: string) {
  if (!value) {
    return '日期待确认'
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '日期待确认' : date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}

function editResource(item: LibraryItem) {
  detailOpen.value = false
  emit('edit', item)
}

function resourceAction(command: string, item: LibraryItem) {
  if (command === 'edit') {
    editResource(item)
  }
  else if (command === 'delete') {
    emit('delete', item)
  }
}

function seriesName(item: LibraryItem) {
  const supplied = metadataString(item, 'series_name')
  if (supplied) {
    return supplied
  }
  const fact = (item.facts ?? []).find(entry => typeof entry === 'string' && entry.startsWith('产品系列：'))
  if (typeof fact === 'string') {
    return fact.replace(/^产品系列：/, '').trim()
  }
  return item.tags?.find(tag => tag.endsWith('系列')) ?? ''
}

function entryIdentity(item: LibraryItem) {
  return item.resource_entry_id || item.id
}

function uniqueResources(values: LibraryItem[]) {
  return [...new Map(values.map(item => [item.id, item])).values()]
}

function parentResource(item: LibraryItem) {
  if (!item.parent_id) {
    return undefined
  }
  return props.resources.find(candidate =>
    candidate.id === item.parent_id || entryIdentity(candidate) === item.parent_id,
  )
}

function childResources(item: LibraryItem) {
  const identity = entryIdentity(item)
  const itemIds = metadataStrings(item, 'media_library_item_ids')
  const entryIds = metadataStrings(item, 'media_entry_ids')
  return uniqueResources(props.resources.filter(candidate =>
    candidate.parent_id === identity
    || itemIds.includes(candidate.id)
    || (candidate.resource_entry_id ? entryIds.includes(candidate.resource_entry_id) : false),
  ))
}

function relatedResources(item: LibraryItem) {
  return uniqueResources([
    ...(parentResource(item) ? [parentResource(item)!] : []),
    ...childResources(item),
  ])
}

function relatedMedia(item: LibraryItem) {
  return resolveRelatedMedia(item, props.resources)
}

function previewResource(item: LibraryItem) {
  return resolvePreviewResource(item, props.resources)
}

function resourceSummary(item: LibraryItem) {
  const fact = item.facts?.find(entry => typeof entry === 'string')
  if (typeof fact === 'string' && fact.trim()) {
    return fact.trim()
  }
  const text = item.text_content
    .split(/\r?\n/u)
    .map(value => value.trim())
    .filter(value => value && value !== item.name)
    .join(' ')
  return text || item.notes || '暂无内容摘要'
}

function factText(value: Record<string, unknown> | string) {
  if (typeof value === 'string') {
    return value
  }
  return Object.entries(value)
    .map(([key, entry]) => `${key}：${typeof entry === 'object' ? JSON.stringify(entry) : String(entry)}`)
    .join('；')
}

function safeSourceUrl(item: LibraryItem) {
  const value = item.source_url || item.source_uri
  const match = value?.trim().match(/^https?:\/\/[^\s\u3400-\u9FFF]+/u)
  return match?.[0] ?? ''
}

const catalogNodes = computed<CatalogNode[]>(() => {
  const children = resourceKindOptions
    .map((kindOption) => {
      const kindResources = props.resources.filter(item => item.resource_kind === kindOption.value)
      const facets = new Map<string, { label: string, value: string, type: 'category' | 'series', count: number }>()
      kindResources.forEach((item) => {
        const productSeries = kindOption.value === 'product' ? seriesName(item) : ''
        const value = productSeries || item.category || 'uncategorized'
        const type = productSeries ? 'series' : 'category'
        const existing = facets.get(`${type}:${value}`)
        facets.set(`${type}:${value}`, {
          label: productSeries || categoryLabel(item.category),
          value,
          type,
          count: (existing?.count ?? 0) + 1,
        })
      })
      return {
        id: `kind:${kindOption.value}`,
        label: kindOption.label,
        count: kindResources.length,
        kind: kindOption.value,
        children: [...facets.values()]
          .sort((left, right) => left.label.localeCompare(right.label, 'zh-CN'))
          .map(facet => ({
            id: `kind:${kindOption.value}:${facet.type}:${facet.value}`,
            label: facet.label,
            count: facet.count,
            kind: kindOption.value,
            facetType: facet.type,
            facetValue: facet.value,
          })),
      } satisfies CatalogNode
    })
    .filter(node => node.count > 0)
  return [{ id: 'all', label: '全部资料与素材', count: props.resources.length, children }]
})

function flattenNodes(nodes: CatalogNode[]): CatalogNode[] {
  return nodes.flatMap(node => [node, ...flattenNodes(node.children ?? [])])
}

const selectedNode = computed(() =>
  flattenNodes(catalogNodes.value).find(node => node.id === selectedTreeId.value),
)

const categoryOptions = computed(() => {
  const counts = new Map<string, number>()
  props.resources.forEach(item => counts.set(item.category, (counts.get(item.category) ?? 0) + 1))
  return [...counts.entries()]
    .filter(([value]) => Boolean(value))
    .map(([value, count]) => ({ value, label: categoryLabel(value), count }))
    .sort((left, right) => left.label.localeCompare(right.label, 'zh-CN'))
})

function matchesSelectedNode(item: LibraryItem) {
  const node = selectedNode.value
  if (!node || node.id === 'all') {
    return true
  }
  if (node.kind && item.resource_kind !== node.kind) {
    return false
  }
  if (node.facetType === 'category') {
    return (item.category || 'uncategorized') === node.facetValue
  }
  if (node.facetType === 'series') {
    return seriesName(item) === node.facetValue
  }
  return true
}

const filteredResources = computed(() => {
  const normalizedQuery = query.value.trim().toLocaleLowerCase('zh-CN')
  return props.resources.filter((item) => {
    if (!matchesSelectedNode(item)) {
      return false
    }
    if (kindFilter.value && item.resource_kind !== kindFilter.value) {
      return false
    }
    if (categoryFilter.value && item.category !== categoryFilter.value) {
      return false
    }
    if (dataLevelFilter.value && item.data_level !== dataLevelFilter.value) {
      return false
    }
    if (!normalizedQuery) {
      return true
    }
    const searchText = [
      item.name,
      item.text_content,
      item.notes,
      item.category,
      categoryLabel(item.category),
      resourceKindLabel(item.resource_kind),
      seriesName(item),
      ...(item.tags ?? []),
      ...(item.facts ?? []).map(factText),
    ].join(' ').toLocaleLowerCase('zh-CN')
    return searchText.includes(normalizedQuery)
  })
})

const paginatedResources = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredResources.value.slice(start, start + pageSize.value)
})

const selectedResource = computed(() => {
  if (detailResource.value?.id === selectedResourceId.value) {
    return detailResource.value
  }
  return props.resources.find(item => item.id === selectedResourceId.value)
})
const serviceDetailRelations = computed(() => {
  const detail = detailResource.value
  if (!detail || detail.id !== selectedResourceId.value) {
    return []
  }
  return [detail.parent, ...detail.children]
    .filter(Boolean)
    .map(relation => props.resources.find(item =>
      item.id === relation?.item_id || entryIdentity(item) === relation?.resource_entry_id,
    ))
    .filter(Boolean) as LibraryItem[]
})
const detailRelations = computed(() => {
  if (!selectedResource.value) {
    return []
  }
  return uniqueResources([
    ...serviceDetailRelations.value,
    ...relatedResources(selectedResource.value),
  ])
})
const detailAssetResources = computed<LibraryItem[]>(() => {
  const detail = detailResource.value
  if (!detail || detail.id !== selectedResourceId.value) {
    return []
  }
  return detail.assets
    .map((asset, index) => {
      const resourceKind: LibraryResourceKind | undefined = asset.media_type.startsWith('image/')
        ? 'image'
        : asset.media_type.startsWith('video/')
          ? 'video'
          : asset.media_type.startsWith('audio/')
            ? 'audio'
            : undefined
      if (!resourceKind) {
        return undefined
      }
      return {
        id: asset.id,
        resource_kind: resourceKind,
        category: detail.category,
        name: `${detail.name} · 素材 ${index + 1}`,
        text_content: '',
        data_level: detail.data_level,
        version: detail.version,
        active: true,
        has_asset: true,
        download_url: asset.download_url,
      } satisfies LibraryItem
    })
    .filter(Boolean) as LibraryItem[]
})
const detailGallery = computed(() => {
  if (!selectedResource.value) {
    return []
  }
  if (['image', 'logo', 'video', 'audio'].includes(selectedResource.value.resource_kind)) {
    return [selectedResource.value]
  }
  return uniqueResources([
    ...detailRelations.value.filter(item =>
      ['image', 'logo', 'video', 'audio'].includes(item.resource_kind),
    ),
    ...detailAssetResources.value,
  ])
})
const activeDetailMedia = computed(() =>
  detailGallery.value.find(item => item.id === detailMediaId.value) ?? detailGallery.value[0],
)
const externalReferences = computed(() =>
  detailResource.value?.id === selectedResourceId.value
    ? detailResource.value.external_references
    : [],
)

function selectTreeNode(node: CatalogNode) {
  selectedTreeId.value = node.id
}

async function openDetail(item: LibraryItem) {
  selectedResourceId.value = item.id
  detailOpen.value = true
  detailResource.value = undefined
  detailError.value = ''
  const gallery = relatedMedia(item)
  detailMediaId.value = gallery[0]?.id ?? ''
  detailLoading.value = true
  try {
    const detail = await contentApi.getLibraryItemDetail(item.id)
    if (selectedResourceId.value !== item.id) {
      return
    }
    detailResource.value = detail
    const media = ['image', 'logo', 'video', 'audio'].includes(detail.resource_kind)
      ? [detail]
      : detail.children
        .map(relation => props.resources.find(candidate =>
          candidate.id === relation.item_id
          || entryIdentity(candidate) === relation.resource_entry_id,
        ))
        .filter(candidate => candidate && ['image', 'logo', 'video', 'audio'].includes(candidate.resource_kind)) as LibraryItem[]
    detailMediaId.value = media[0]?.id ?? detailMediaId.value
  }
  catch (error) {
    if (selectedResourceId.value === item.id) {
      detailError.value = error instanceof Error ? error.message : '资料详情加载失败'
    }
  }
  finally {
    if (selectedResourceId.value === item.id) {
      detailLoading.value = false
    }
  }
}

watch(
  [query, kindFilter, categoryFilter, dataLevelFilter, selectedTreeId],
  () => {
    currentPage.value = 1
  },
)

watch(() => props.resources, () => {
  if (selectedResourceId.value && !selectedResource.value) {
    detailOpen.value = false
    selectedResourceId.value = ''
  }
})
</script>

<template>
  <div class="library-catalog" :aria-busy="loading">
    <div class="catalog-shell">
      <aside class="catalog-sidebar">
        <div class="sidebar-heading">
          <div>
            <h3>资料分类</h3>
            <p>{{ resources.length }} 项资料与素材</p>
          </div>
          <ElIcon><Collection /></ElIcon>
        </div>
        <ElTree
          :data="catalogNodes"
          node-key="id"
          :props="{ label: 'label', children: 'children' }"
          :current-node-key="selectedTreeId"

          highlight-current
          :default-expanded-keys="['all']"
          :expand-on-click-node="false"
          @node-click="selectTreeNode"
        >
          <template #default="{ data }">
            <span class="tree-node">
              <span>{{ data.label }}</span>
              <small>{{ data.count }}</small>
            </span>
          </template>
        </ElTree>
      </aside>

      <section class="catalog-main">
        <div class="catalog-heading">
          <div>
            <h3>{{ selectedNode?.label ?? '全部资料与素材' }}</h3>
            <p>集中管理创作需要的产品资料、图片与音视频。</p>
          </div>
          <ElButton type="primary" @click="emit('create')">新增资料或素材</ElButton>
        </div>

        <div class="catalog-toolbar">
          <ElInput
            v-model="query"
            :prefix-icon="Search"
            clearable
            placeholder="搜索名称、系列、标签或正文"
            aria-label="搜索资料与素材"
          />
          <ElSelect
            v-model="kindFilter"
            clearable
            placeholder="全部资源类型"
            aria-label="资源类型筛选"
          >
            <ElOption
              v-for="item in resourceKindOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </ElSelect>
          <ElSelect
            v-model="categoryFilter"
            clearable
            filterable
            placeholder="全部业务分类"
            aria-label="业务分类筛选"
          >
            <ElOption
              v-for="item in categoryOptions"
              :key="item.value"
              :label="`${item.label}（${item.count}）`"
              :value="item.value"
            />
          </ElSelect>
          <ElSelect
            v-model="dataLevelFilter"
            clearable
            placeholder="全部数据等级"
            aria-label="数据等级筛选"
          >
            <ElOption label="公开" value="public" />
            <ElOption label="内部" value="internal" />
            <ElOption label="受限" value="restricted" />
          </ElSelect>
          <ElButtonGroup aria-label="视图切换">
            <ElButton :type="viewMode === 'card' ? 'primary' : ''" aria-label="卡片视图" @click="viewMode = 'card'">
              <ElIcon><Grid /></ElIcon>
            </ElButton>
            <ElButton :type="viewMode === 'list' ? 'primary' : ''" aria-label="列表视图" @click="viewMode = 'list'">
              <ElIcon><List /></ElIcon>
            </ElButton>
          </ElButtonGroup>
        </div>

        <div class="result-heading">
          <span>找到 <strong>{{ filteredResources.length }}</strong> 项资料</span>
          <small>点击资料查看详情</small>
        </div>

        <ElSkeleton v-if="loading" :rows="8" animated />
        <ElEmpty v-else-if="!filteredResources.length" description="当前筛选条件下暂无资料" />

        <div v-else-if="viewMode === 'card'" class="resource-grid">
          <article
            v-for="item in paginatedResources"
            :key="item.id"
            class="resource-card"
            role="button"
            tabindex="0"
            :aria-label="`查看${item.name}`"
            @click="openDetail(item)"
            @keydown.enter.self="openDetail(item)"
            @keydown.space.self.prevent="openDetail(item)"
          >
            <div class="resource-preview">
              <ContentLibraryMedia
                v-if="previewResource(item)"
                :item="previewResource(item)"
                :alt="`${item.name}预览`"
              />
              <div v-else class="resource-type-preview" :data-kind="item.resource_kind">
                <ElIcon>
                  <Box v-if="item.resource_kind === 'product'" />
                  <Document v-else-if="['document', 'spec'].includes(item.resource_kind)" />
                  <VideoPlay v-else-if="item.resource_kind === 'video'" />
                  <Headset v-else-if="item.resource_kind === 'audio'" />
                  <Picture v-else />
                </ElIcon>
                <span>{{ resourceKindLabel(item.resource_kind) }}</span>
              </div>
              <span v-if="relatedMedia(item).length > 1" class="media-count">
                {{ relatedMedia(item).length }} 项关联素材
              </span>
              <ElDropdown
                v-if="canEdit?.(item)"
                class="resource-more"
                trigger="click"
                @command="resourceAction($event, item)"
                @click.stop
                @keydown.stop
              >
                <ElButton circle :aria-label="`${item.name}的更多操作`" @click.stop>
                  <ElIcon><MoreFilled /></ElIcon>
                </ElButton>
                <template #dropdown>
                  <ElDropdownMenu>
                    <ElDropdownItem command="edit">编辑并新建版本</ElDropdownItem>
                    <ElDropdownItem command="delete" divided class="resource-delete-action">删除资料</ElDropdownItem>
                  </ElDropdownMenu>
                </template>
              </ElDropdown>
            </div>
            <div class="resource-card-body">
              <h4 :title="item.name">{{ item.name }}</h4>
              <div class="resource-card-tags">
                <ElTag size="small" effect="light">{{ resourceKindLabel(item.resource_kind) }}</ElTag>
                <span class="resource-version">v{{ item.version }}</span>
              </div>
              <p :title="resourceSummary(item)">{{ resourceSummary(item) }}</p>
              <div class="resource-card-meta">
                <span :title="seriesName(item) || categoryLabel(item.category)">{{ seriesName(item) || categoryLabel(item.category) }}</span>
                <span>{{ shortDate(item.updated_at) }}</span>
              </div>
            </div>
          </article>
        </div>

        <ElTable
          v-else-if="!loading && filteredResources.length"
          :data="paginatedResources"
          empty-text="暂无资料"
          class="resource-table"
          @row-click="openDetail"
        >
          <ElTableColumn label="名称" min-width="220">
            <template #default="scope">
              <button type="button" class="resource-name-button" @click.stop="openDetail(scope.row)">
                <strong>{{ scope.row.name }}</strong>
                <small>{{ seriesName(scope.row) || categoryLabel(scope.row.category) }}</small>
              </button>
            </template>
          </ElTableColumn>
          <ElTableColumn label="类型" min-width="110">
            <template #default="scope">{{ resourceKindLabel(scope.row.resource_kind) }}</template>
          </ElTableColumn>
          <ElTableColumn label="分类/系列" min-width="150">
            <template #default="scope">{{ seriesName(scope.row) || categoryLabel(scope.row.category) }}</template>
          </ElTableColumn>
          <ElTableColumn label="关联素材" width="100">
            <template #default="scope">{{ relatedMedia(scope.row).length }}</template>
          </ElTableColumn>
          <ElTableColumn label="等级" width="90">
            <template #default="scope"><ElTag effect="plain">{{ dataLevelLabel(scope.row.data_level) }}</ElTag></template>
          </ElTableColumn>
          <ElTableColumn prop="version" label="版本" width="70" />
          <ElTableColumn label="更新时间" min-width="160">
            <template #default="scope">{{ formatDate(scope.row.updated_at) }}</template>
          </ElTableColumn>
          <ElTableColumn
            label="操作"
            width="240"
            fixed="right"
            class-name="resource-table-ops"
          >
            <template #default="scope">
              <div class="resource-table-actions">
                <ElButton link type="primary" @click.stop="openDetail(scope.row)">查看</ElButton>
                <ElButton v-if="canEdit?.(scope.row)" link @click.stop="emit('edit', scope.row)">新建版本</ElButton>
                <ElButton
                  v-if="canEdit?.(scope.row)"
                  link
                  type="danger"
                  @click.stop="emit('delete', scope.row)"
                >
                  删除
                </ElButton>
              </div>
            </template>
          </ElTableColumn>
        </ElTable>

        <WbPagination
          v-model:page="currentPage"
          v-model:page-size="pageSize"
          :total="filteredResources.length"
          :page-sizes="[12, 24, 48, 96]"
          class="catalog-pagination"
        />
      </section>
    </div>

    <ElDrawer
      v-model="detailOpen"
      :title="selectedResource?.name || '资料详情'"
      size="min(680px, 94vw)"
      destroy-on-close
      class="library-detail-drawer"
    >
      <template #header>
        <div v-if="selectedResource" class="detail-heading">
          <div>
            <span>{{ resourceKindLabel(selectedResource.resource_kind) }}</span>
            <h3>{{ selectedResource.name }}</h3>
          </div>
          <ElTag effect="plain">{{ dataLevelLabel(selectedResource.data_level) }}</ElTag>
        </div>
      </template>

      <ElSkeleton v-if="detailLoading" :rows="8" animated />
      <ElAlert
        v-else-if="detailError"
        type="warning"
        title="已显示目录中的基础信息"
        :description="detailError"
        :closable="false"
        show-icon
        class="detail-load-alert"
      />

      <div v-if="selectedResource && !detailLoading" class="detail-content">
        <section v-if="detailGallery.length" class="detail-gallery">
          <ContentLibraryMedia
            :item="activeDetailMedia"
            :alt="`${selectedResource.name}素材预览`"
            controls
          />
          <div v-if="detailGallery.length > 1" class="detail-thumbnails">
            <button
              v-for="media in detailGallery"
              :key="media.id"
              type="button"
              :class="{ 'is-active': activeDetailMedia?.id === media.id }"
              @click="detailMediaId = media.id"
            >
              <ContentLibraryMedia :item="media" :alt="media.name" compact />
              <span>{{ media.name }}</span>
            </button>
          </div>
        </section>

        <section class="detail-information">
          <dl class="detail-properties">
            <div><dt>资源类型</dt><dd>{{ resourceKindLabel(selectedResource.resource_kind) }}</dd></div>
            <div><dt>业务分类</dt><dd>{{ categoryLabel(selectedResource.category) }}</dd></div>
            <div v-if="seriesName(selectedResource)"><dt>产品系列</dt><dd>{{ seriesName(selectedResource) }}</dd></div>
            <div><dt>当前版本</dt><dd>v{{ selectedResource.version }}</dd></div>
            <div><dt>更新时间</dt><dd>{{ shortDate(selectedResource.updated_at) }}</dd></div>
            <div><dt>时效状态</dt><dd>{{ selectedResource.freshness || '未标记' }}</dd></div>
          </dl>

          <div v-if="selectedResource.tags?.length" class="detail-block">
            <h4>标签</h4>
            <div class="detail-tags">
              <ElTag v-for="tag in selectedResource.tags" :key="tag" effect="plain">{{ tag }}</ElTag>
            </div>
          </div>

          <div v-if="selectedResource.facts?.length" class="detail-block">
            <h4>{{ selectedResource.resource_kind === 'product' ? '产品参数' : '关键事实' }}</h4>
            <ul class="fact-list">
              <li v-for="(fact, index) in selectedResource.facts" :key="index">{{ factText(fact) }}</li>
            </ul>
          </div>

          <div class="detail-block">
            <h4>资料正文</h4>
            <div class="detail-copy">{{ selectedResource.text_content || '暂无正文内容' }}</div>
          </div>

          <div v-if="selectedResource.notes" class="detail-block detail-notes">
            <h4>使用说明</h4>
            <p>{{ selectedResource.notes }}</p>
          </div>

          <div v-if="detailRelations.length" class="detail-block">
            <h4>关联资料与素材</h4>
            <div class="relation-list">
              <button
                v-for="item in detailRelations"
                :key="item.id"
                type="button"
                @click="openDetail(item)"
              >
                <span>{{ item.name }}</span>
                <small>{{ resourceKindLabel(item.resource_kind) }}</small>
              </button>
            </div>
          </div>

          <div v-if="externalReferences.length" class="detail-block">
            <h4>外部系统引用</h4>
            <div class="relation-list">
              <a
                v-for="reference in externalReferences"
                :key="`${reference.system}:${reference.external_id}`"
                :href="reference.source_url || undefined"
                :target="reference.source_url ? '_blank' : undefined"
                :aria-disabled="!reference.source_url"
              >
                <span>{{ reference.system }}</span>
                <small>{{ reference.external_id }}</small>
              </a>
            </div>
          </div>
          <details v-if="selectedResource.knowledge_sync" class="detail-sync">
            <summary>知识索引与原件状态</summary>
            <ContentKnowledgeSync
              :key="selectedResource.id"
              :item-id="selectedResource.id"
              :initial="selectedResource.knowledge_sync"
              :can-edit="canEdit?.(selectedResource)"
            />
          </details>
        </section>
      </div>

      <template #footer>
        <div v-if="selectedResource" class="detail-actions">
          <ElLink
            v-if="safeSourceUrl(selectedResource)"
            :href="safeSourceUrl(selectedResource)"
            target="_blank"
            type="primary"
          >
            查看资料来源
          </ElLink>
          <span />
          <ElButton @click="detailOpen = false">关闭</ElButton>
          <ElButton v-if="canEdit?.(selectedResource)" type="primary" @click="editResource(selectedResource)">编辑并创建新版本</ElButton>
        </div>
      </template>
    </ElDrawer>
  </div>
</template>

<style scoped>
.library-catalog {
  display: grid;
  min-width: 0;
  container: library-catalog / inline-size;
  gap: var(--xi-component-content-gap-md);
}

.catalog-shell {
  display: grid;
  grid-template-columns: 224px minmax(0, 1fr);
  min-width: 0;
  align-items: stretch;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
  overflow: hidden;
}

.catalog-sidebar {
  min-width: 0;
  padding: 24px 12px;
  border-right: var(--xi-component-content-border);
}

.sidebar-heading,
.catalog-heading,
.result-heading,
.detail-heading,
.detail-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-md);
}

.sidebar-heading {
  padding: 0 8px;
  margin-bottom: var(--xi-component-content-gap-md);
}

.sidebar-heading h3,
.sidebar-heading p,
.catalog-heading h3,
.catalog-heading p,
.detail-heading h3,
.detail-block h4,
.detail-block p {
  margin: 0;
}

.sidebar-heading h3,
.catalog-heading h3,
.detail-heading h3 {
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading2-font-size);
}

.sidebar-heading p,
.catalog-heading p,
.catalog-path {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.sidebar-heading .el-icon {
  color: var(--xi-semantic-color-text-brand);
  font-size: var(--xi-component-content-icon-size);
}

.catalog-sidebar :deep(.el-tree-node__content) {
  min-height: 40px;
  height: auto;
  padding-top: 6px;
  padding-bottom: 6px;
  margin: 2px 0;
  border-radius: 6px;
}

.catalog-sidebar :deep(.el-tree-node.is-current > .el-tree-node__content) {
  color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-brand-subtle);
  box-shadow: inset 3px 0 0 var(--xi-semantic-color-text-brand);
  font-weight: 600;
}

.catalog-sidebar :deep(.el-tree-node__expand-icon) {
  flex: none;
}

.tree-node {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-xs);
  padding-right: var(--xi-component-content-gap-xs);
}

.tree-node small {
  color: var(--xi-semantic-color-text-tertiary);
  flex: none;
  font-variant-numeric: tabular-nums;
}

.tree-node > span {
  white-space: normal;
  overflow-wrap: anywhere;
}

.catalog-main {
  min-width: 0;
  padding: var(--xi-component-content-gap-lg);
}

.catalog-heading h3 {
  margin-top: var(--xi-component-content-gap-xxs);
}

.catalog-heading p {
  margin-top: var(--xi-component-content-gap-xxs);
}

.catalog-toolbar {
  display: grid;
  grid-template-columns: minmax(180px, 1.6fr) repeat(3, minmax(110px, 1fr)) auto;
  gap: var(--xi-component-content-gap-xs);
  margin-top: var(--xi-component-content-gap-md);
}

.catalog-toolbar > * {
  min-width: 0;
}

.result-heading {
  margin: var(--xi-component-content-gap-md) 0;
  color: var(--xi-semantic-color-text-secondary);
}

.result-heading strong {
  color: var(--xi-semantic-color-text-brand);
}

.result-heading small {
  color: var(--xi-semantic-color-text-tertiary);
}

.resource-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 210px), 1fr));
  gap: var(--xi-component-content-gap-md);
}

.resource-card {
  display: flex;
  min-width: 0;
  overflow: hidden;
  flex-direction: column;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
  cursor: pointer;
  transition:
    border-color 180ms ease,
    box-shadow 180ms ease;
}

.resource-card:hover,
.resource-card:focus-visible {
  border-color: var(--xi-semantic-color-border-brand-default);
  box-shadow: 0 4px 16px rgb(79 70 229 / 8%);
}

.resource-preview {
  position: relative;
  aspect-ratio: 16 / 10;
  min-height: 0;
  overflow: hidden;
  background: var(--xi-semantic-color-background-page);
}

.resource-preview :deep(.library-media) {
  height: 100%;
  min-height: 0;
  border-radius: 0;
}

.resource-preview :deep(.library-media img),
.resource-preview :deep(.library-media video) {
  max-height: 100%;
  padding: 10px;
  box-sizing: border-box;
}

.resource-type-preview {
  display: grid;
  height: 100%;
  gap: var(--xi-component-content-gap-xs);
  place-items: center;
  align-content: center;
  color: var(--xi-semantic-color-text-tertiary);
}

/* Same neutral tile for documents, video and audio; the label under it names the kind. */
.resource-type-preview .el-icon {
  color: var(--xi-semantic-color-text-secondary);
  width: 56px;
  height: 56px;
  font-size: 26px;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.resource-type-preview[data-kind='product'] .el-icon {
  color: #15803d;
  background: #dcfce7;
}

.resource-more {
  position: absolute;
  top: 8px;
  right: 8px;
}

.resource-more :deep(.el-button) {
  width: 36px;
  height: 36px;
  color: var(--xi-semantic-color-text-secondary);
  background: var(--xi-semantic-color-background-container);
  box-shadow: 0 2px 8px rgb(15 23 42 / 6%);
}

.resource-delete-action {
  color: var(--el-color-danger);
}

.media-count {
  position: absolute;
  right: var(--xi-component-content-gap-md);
  bottom: var(--xi-component-content-gap-md);
  padding: var(--xi-component-content-gap-xxs) var(--xi-component-content-gap-xs);
  color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand);
  border-radius: var(--xi-component-content-radius-control);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.resource-card-body {
  display: grid;
  min-width: 0;
  flex: 1;
  gap: var(--xi-component-content-gap-xs);
  padding: var(--xi-component-content-gap-md);
}

.resource-card-tags,
.detail-tags {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-xxs);
}

.resource-card h4 {
  overflow: hidden;
  margin: 0;
  color: var(--xi-semantic-color-text-primary);
  font-size: 15px;
  line-height: 24px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.resource-card p {
  overflow: hidden;
  margin: 0;
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-body-font-size);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.resource-card-meta {
  display: flex;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-xs);
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.resource-card-meta > span:first-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.resource-card-meta > span:last-child {
  flex: none;
}

.resource-version {
  padding: 0 6px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
}

.resource-name-button {
  display: grid;
  width: 100%;
  gap: var(--xi-component-content-gap-xxs);
  padding: 0;
  color: var(--xi-semantic-color-text-primary);
  text-align: left;
  background: transparent;
  border: 0;
  cursor: pointer;
}

.resource-name-button small {
  color: var(--xi-semantic-color-text-tertiary);
}

.resource-table :deep(.el-table__row) {
  cursor: pointer;
}

.resource-table :deep(.resource-table-ops .cell) {
  overflow: visible;
  padding-left: var(--xi-component-content-gap-xs);
  padding-right: var(--xi-component-content-gap-xs);
  white-space: nowrap;
  word-break: keep-all;
}

.resource-table-actions {
  display: inline-flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 0;
  white-space: nowrap;
  --el-button-hover-link-text-color: var(--xi-semantic-color-link-hover);
}

.resource-table-actions :deep(.el-button.is-link) {
  height: var(--xi-component-content-control-height);
  min-width: auto;
  margin-left: 0;
  padding: var(--xi-component-content-gap-xs) var(--xi-component-content-gap-xxs);
  flex-shrink: 0;
  background-color: transparent !important;
  background-image: none !important;
  border-color: transparent !important;
  box-shadow: none !important;
  text-shadow: none !important;
  filter: none;
}

.resource-table-actions :deep(.el-button--primary.is-link) {
  color: var(--xi-semantic-color-link-default) !important;
}

.resource-table-actions :deep(.el-button--primary.is-link:not(.is-disabled):hover),
.resource-table-actions :deep(.el-button--primary.is-link:not(.is-disabled):focus) {
  color: var(--xi-semantic-color-link-hover) !important;
}

.resource-table-actions :deep(.el-button--primary.is-link:not(.is-disabled):active) {
  color: var(--xi-semantic-color-link-active) !important;
}

.resource-table-actions :deep(.el-button--default.is-link:not(.el-button--primary):not(.el-button--danger)) {
  color: var(--xi-semantic-color-text-secondary) !important;
}

.resource-table-actions :deep(.el-button--default.is-link:not(.el-button--primary):not(.el-button--danger):hover),
.resource-table-actions :deep(.el-button--default.is-link:not(.el-button--primary):not(.el-button--danger):focus) {
  color: var(--xi-semantic-color-link-hover) !important;
}

.resource-table-actions :deep(.el-button--default.is-link:not(.el-button--primary):not(.el-button--danger):active) {
  color: var(--xi-semantic-color-link-active) !important;
}

.resource-table-actions :deep(.el-button--danger.is-link) {
  color: var(--xi-semantic-color-feedback-danger-default) !important;
}

.resource-table-actions :deep(.el-button--danger.is-link:not(.is-disabled):hover),
.resource-table-actions :deep(.el-button--danger.is-link:not(.is-disabled):focus) {
  color: var(--xi-semantic-color-feedback-danger-background-hover) !important;
}

.resource-table-actions :deep(.el-button--danger.is-link:not(.is-disabled):active) {
  color: var(--xi-semantic-color-feedback-danger-active) !important;
}

.catalog-pagination.wb-pagination {
  margin-top: var(--xi-component-content-gap-lg);
  padding-inline: 0;
}

.detail-heading {
  width: 100%;
  min-width: 0;
  margin-right: 16px;
}

.detail-heading > div {
  min-width: 0;
}

.detail-heading span {
  color: var(--xi-semantic-color-text-brand);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.detail-heading h3 {
  overflow: hidden;
  margin-top: var(--xi-component-content-gap-xxs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-content {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--xi-component-content-gap-lg);
  align-items: start;
  padding-bottom: 24px;
}

.detail-gallery,
.detail-information,
.detail-block {
  display: grid;
  gap: var(--xi-component-content-gap-md);
}

.detail-load-alert {
  margin-bottom: var(--xi-component-content-gap-md);
}

.detail-gallery {
  min-width: 0;
}

.detail-gallery > :deep(.library-media) {
  aspect-ratio: 16 / 10;
  min-height: 0;
}

.detail-gallery > :deep(.library-media img),
.detail-gallery > :deep(.library-media video) {
  max-height: 340px;
}

.detail-properties {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px 24px;
  padding: 20px;
  margin: 0;
  background: var(--xi-semantic-color-background-page);
  border-radius: 12px;
}

.detail-properties dt {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  margin-bottom: 4px;
}

.detail-properties dd {
  margin: 0;
  color: var(--xi-semantic-color-text-primary);
  overflow-wrap: anywhere;
}

.detail-sync summary {
  padding: 12px 0;
  color: var(--xi-semantic-color-text-secondary);
  cursor: pointer;
}

.detail-thumbnails {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(calc(var(--xi-component-content-control-height-large) * 2), 1fr));
  gap: var(--xi-component-content-gap-xs);
}

.detail-thumbnails button,
.relation-list button,
.relation-list a {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-xxs);
  padding: var(--xi-component-content-gap-xs);
  color: var(--xi-semantic-color-text-secondary);
  text-align: left;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
  cursor: pointer;
  text-decoration: none;
}

.detail-thumbnails button.is-active {
  border-color: var(--xi-semantic-color-border-brand-default);
}

.detail-thumbnails button span,
.relation-list button span,
.relation-list a span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-information:only-child {
  grid-column: 1 / -1;
}

.detail-block {
  padding-top: var(--xi-component-content-gap-md);
  border-top: var(--xi-component-content-border);
}

.detail-block h4 {
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading3-font-size);
}

.fact-list {
  display: grid;
  gap: var(--xi-component-content-gap-xs);
  margin: 0;
  padding-left: var(--xi-component-content-gap-lg);
  color: var(--xi-semantic-color-text-secondary);
}

.detail-copy {
  max-height: var(--xi-component-content-editor-min-height);
  overflow: auto;
  padding: var(--xi-component-content-gap-md);
  color: var(--xi-semantic-color-text-secondary);
  white-space: pre-wrap;
  background: var(--xi-semantic-color-background-page);
  border-radius: var(--xi-component-content-radius-control);
}

.detail-notes {
  padding: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-feedback-warning-background-subtle);
  border: var(--xi-component-content-border);
  border-color: var(--xi-semantic-color-feedback-warning-border);
  border-radius: var(--xi-component-content-radius-control);
}

.detail-notes p {
  color: var(--xi-semantic-color-feedback-warning-text);
}

.relation-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(var(--xi-component-content-entry-min-width), 1fr));
  gap: var(--xi-component-content-gap-xs);
}

.relation-list small {
  color: var(--xi-semantic-color-text-tertiary);
}

.detail-actions span {
  flex: 1;
}

.detail-actions {
  width: 100%;
  flex-wrap: wrap;
}

@container library-catalog (max-width: 1100px) {
  .catalog-toolbar {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .catalog-toolbar > :first-child {
    grid-column: span 2;
  }
}

@container library-catalog (max-width: 760px) {
  .catalog-shell {
    grid-template-columns: minmax(0, 1fr);
  }

  .catalog-sidebar {
    max-height: 230px;
    overflow-y: auto;
    border-right: 0;
    border-bottom: var(--xi-component-content-border);
    padding: 16px 12px;
  }

  .catalog-main {
    padding: 16px;
  }

  .catalog-heading {
    flex-wrap: wrap;
  }

  .catalog-toolbar {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .catalog-toolbar > :first-child {
    grid-column: 1 / -1;
  }

  .result-heading small {
    display: none;
  }

  .catalog-pagination :deep(.el-pagination) {
    justify-content: center;
  }
}

@media (max-width: 480px) {
  .detail-properties {
    gap: 16px;
    padding: 16px;
  }

  .detail-actions {
    gap: 8px;
  }

  .detail-actions > span {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .resource-card {
    transition: none;
  }
}
</style>
