<script setup lang="ts">
import type { TableInstance } from 'element-plus'
import type { Product } from '@/api/agent/catalog'
import { Plus, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { catalogApi, catalogMedia, itemStatusLabels } from '@/api/agent/catalog'
import CatalogBatchDialog from './CatalogBatchDialog.vue'
import { itemStatusClass, shortTime, yuan } from './catalogPresentation'
import CatalogProductEditor from './CatalogProductEditor.vue'
import { friendlyContentError } from './contentWorkflow'
import WbPagination from './WbPagination.vue'

defineOptions({ name: 'CatalogProductsView' })

const router = useRouter()
const table = ref<TableInstance>()
const products = ref<Product[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const query = ref('')
const loading = ref(false)
const selected = ref<Product[]>([])
const editorOpen = ref(false)
const editing = ref<Product | null>(null)
const batchOpen = ref(false)
let searchTimer: ReturnType<typeof setTimeout> | undefined

async function load() {
  loading.value = true
  try {
    const result = await catalogApi.products({ q: query.value.trim(), page: page.value, page_size: pageSize.value })
    products.value = result.items
    total.value = result.total
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '商品读取失败'))
  }
  finally {
    loading.value = false
  }
}

function search() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 1
    void load()
  }, 250)
}

function openEditor(product: Product | null) {
  editing.value = product
  editorOpen.value = true
}

function onSaved(product: Product) {
  const index = products.value.findIndex(item => item.id === product.id)
  if (index >= 0) {
    products.value[index] = { ...products.value[index], ...product }
  }
  else {
    void load()
  }
}

async function remove(product: Product) {
  try {
    await ElMessageBox.confirm(`删除「${product.name}」？已入库的内容和发布记录会保留。`, '删除商品', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  }
  catch {
    return
  }
  try {
    await catalogApi.deleteProduct(product.id)
    table.value?.toggleRowSelection(product, false)
    await load()
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '删除失败'))
  }
}

function clearSelection() {
  table.value?.clearSelection()
}

function onBatchCreated(batch: { total: number }) {
  ElMessage.success(`已提交 ${batch.total} 个商品，生成完成后会出现在审核列表`)
  clearSelection()
  router.push('/agent/content/review?status=running')
}

function specText(product: Product) {
  return product.specs.slice(0, 3).map(spec => `${spec.name} ${spec.value}`).join(' · ')
}

onMounted(load)
onActivated(load)
</script>

<template>
  <div class="wb-page">
    <header class="wb-page-head">
      <div>
        <h1>商品库</h1>
        <p>商品的名称、价格、图片和参数。勾选商品后可批量生成文案、图片和视频。</p>
      </div>
      <div class="wb-page-actions">
        <ElInput
          v-model="query"
          :prefix-icon="Search"
          placeholder="搜索名称、货号、分类"
          clearable
          class="search"
          @input="search"
          @clear="search"
        />
        <ElButton type="primary" :icon="Plus" @click="openEditor(null)">新增商品</ElButton>
      </div>
    </header>

    <section class="wb-panel">
      <ElTable
        ref="table"
        v-loading="loading"
        :data="products"
        row-key="id"
        class="product-table"
        empty-text="还没有商品"
        @selection-change="selected = $event"
      >
        <ElTableColumn type="selection" width="44" reserve-selection />
        <ElTableColumn label="商品" min-width="240">
          <template #default="{ row }">
            <div class="product-cell">
              <img
                v-if="row.images.length"
                class="wb-thumb"
                :src="catalogMedia.productImage(row.id, row.images[0].id, true)"
                alt=""
                loading="lazy"
              >
              <span v-else class="wb-thumb wb-thumb-empty">无图</span>
              <span class="product-name">
                <button
                  type="button"
                  class="link-like wb-ellipsis"
                  :title="row.name"
                  @click="openEditor(row)"
                >{{ row.name }}</button>
                <small class="wb-muted">{{ row.sku || '无货号' }} · {{ row.images.length }} 张图</small>
              </span>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="价格" width="110" align="right">
          <template #default="{ row }">
            <span class="wb-num" :class="{ 'wb-muted': !row.price }">{{ row.price ? yuan(row.price) : '未定价' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="分类" width="110">
          <template #default="{ row }">
            <span :class="{ 'wb-muted': !row.category }">{{ row.category || '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="参数" min-width="200">
          <template #default="{ row }">
            <span class="wb-muted spec-text" :title="specText(row)">{{ specText(row) || '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="内容" width="120">
          <template #default="{ row }">
            <span v-if="row.draft_status && row.draft_status !== 'approved'" class="wb-pill" :class="itemStatusClass(row.draft_status)">
              {{ itemStatusLabels[row.draft_status as keyof typeof itemStatusLabels] }}
            </span>
            <span v-else-if="row.content_count" class="wb-pill is-success">已入库 {{ row.content_count }}</span>
            <span v-else class="wb-muted">未生成</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="更新" width="110">
          <template #default="{ row }">
            <span class="wb-muted wb-num">{{ shortTime(row.updated_at).slice(5) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="" width="130" align="right">
          <template #default="{ row }">
            <ElButton link type="primary" @click="openEditor(row)">编辑</ElButton>
            <ElButton link @click="remove(row)">删除</ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
      <WbPagination
        v-model:page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        @change="load"
      />
    </section>

    <div v-if="selected.length" class="wb-selection-bar">
      <span>已选 {{ selected.length }} 个商品</span>
      <ElButton link @click="clearSelection">取消选择</ElButton>
      <span class="wb-spacer" />
      <ElButton type="primary" @click="batchOpen = true">批量生成</ElButton>
    </div>

    <CatalogProductEditor v-model="editorOpen" :product="editing" @saved="onSaved" />
    <CatalogBatchDialog v-model="batchOpen" :products="selected" @created="onBatchCreated" />
  </div>
</template>

<style scoped>
.search {
  width: 240px;
}
.product-cell {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.product-name {
  display: grid;
  min-width: 0;
  gap: 2px;
}
.product-name small {
  font-size: 12px;
}
.link-like {
  padding: 0;
  color: var(--xi-semantic-color-text-primary);
  font: inherit;
  font-weight: 500;
  text-align: left;
  background: none;
  border: 0;
  cursor: pointer;
}
.link-like:hover {
  color: var(--xi-semantic-color-action-primary-default);
}
.spec-text {
  display: -webkit-box;
  overflow: hidden;
  font-size: 13px;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.product-table :deep(th.el-table__cell) {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  font-weight: 500;
  background: #fafbfc;
}
@media (width < 700px) {
  .search {
    width: 100%;
  }
  .wb-page-actions {
    width: 100%;
  }
}
</style>
