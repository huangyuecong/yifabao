<script setup lang="ts">
import type { TableInstance } from 'element-plus'
import type { ProductContent } from '@/api/agent/catalog'
import { Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { catalogApi, catalogMedia, recordStatusLabels } from '@/api/agent/catalog'
import { platformInitial, platformName, recordStatusClass, shortTime, yuan } from './catalogPresentation'
import { friendlyContentError } from './contentWorkflow'
import WbPagination from './WbPagination.vue'

defineOptions({ name: 'CatalogContentsView' })

const router = useRouter()
const table = ref<TableInstance>()
const contents = ref<ProductContent[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const query = ref('')
const loading = ref(false)
const selected = ref<ProductContent[]>([])
const viewing = ref<ProductContent | null>(null)
const viewerOpen = ref(false)
let searchTimer: ReturnType<typeof setTimeout> | undefined

const viewingImages = computed(() => viewing.value?.media.filter(media => media.kind === 'image')
  .map(media => catalogMedia.content(viewing.value!.id, media.id)) ?? [])
const viewingVideo = computed(() => viewing.value?.media.find(media => media.kind === 'video'))

async function load() {
  loading.value = true
  try {
    const result = await catalogApi.contents({ q: query.value.trim(), page: page.value, page_size: pageSize.value })
    contents.value = result.items
    total.value = result.total
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '读取失败'))
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

function imageCount(content: ProductContent) {
  return content.media.filter(media => media.kind === 'image').length
}

function open(content: ProductContent) {
  viewing.value = content
  viewerOpen.value = true
}

function publish(workflow: 'listing' | 'promotion', items = selected.value) {
  router.push({ path: `/agent/content/${workflow}`, query: { ids: items.map(item => item.id).join(',') } })
}

async function withdraw(content: ProductContent) {
  try {
    await ElMessageBox.confirm('撤回后这条内容不再出现在内容库，已发布的平台内容不受影响。', '撤回内容', {
      type: 'warning',
      confirmButtonText: '撤回',
      cancelButtonText: '取消',
    })
  }
  catch {
    return
  }
  try {
    await catalogApi.withdrawContent(content.id)
    viewerOpen.value = false
    table.value?.toggleRowSelection(content, false)
    await load()
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '撤回失败'))
  }
}

onMounted(load)
onActivated(load)
</script>

<template>
  <div class="wb-page">
    <header class="wb-page-head">
      <div>
        <h1>内容库</h1>
        <p>审核通过的文案、图片和视频。勾选后可上架到闲鱼，或发布到抖音、小红书、快手。</p>
      </div>
      <div class="wb-page-actions">
        <ElInput
          v-model="query"
          :prefix-icon="Search"
          placeholder="搜索商品或标题"
          clearable
          class="search"
          @input="search"
          @clear="search"
        />
      </div>
    </header>

    <section class="wb-panel">
      <ElTable
        ref="table"
        v-loading="loading"
        :data="contents"
        row-key="id"
        empty-text="还没有通过审核的内容"
        class="content-table"
        @selection-change="selected = $event"
      >
        <ElTableColumn type="selection" width="44" reserve-selection />
        <ElTableColumn label="商品" min-width="200">
          <template #default="{ row }">
            <div class="product-cell">
              <img
                v-if="row.product.image_id"
                class="wb-thumb"
                :src="catalogMedia.productImage(row.product.id, row.product.image_id, true)"
                alt=""
                loading="lazy"
              >
              <span v-else class="wb-thumb wb-thumb-empty">无图</span>
              <span class="cell-text">
                <strong class="wb-ellipsis">{{ row.product.name }}</strong>
                <small class="wb-muted wb-num">{{ yuan(row.product.price) }}</small>
              </span>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="宣传标题" min-width="220">
          <template #default="{ row }">
            <button type="button" class="link-like wb-ellipsis" @click="open(row)">{{ row.title }}</button>
          </template>
        </ElTableColumn>
        <ElTableColumn label="素材" width="120">
          <template #default="{ row }">
            <span class="wb-muted">
              {{ imageCount(row) }} 图{{ row.has_video ? ' · 视频' : '' }}
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="发布情况" min-width="200">
          <template #default="{ row }">
            <div v-if="row.publishes.length" class="publish-list">
              <span
                v-for="item in row.publishes"
                :key="item.platform"
                class="wb-platform"
                :class="`p-${item.platform}`"
                :title="`${platformName(item.platform)}：${recordStatusLabels[item.status as keyof typeof recordStatusLabels]}`"
              >
                <i>{{ platformInitial[item.platform as keyof typeof platformInitial] }}</i>
                <span class="wb-pill is-plain" :class="recordStatusClass(item.status)">{{ recordStatusLabels[item.status as keyof typeof recordStatusLabels] }}</span>
              </span>
            </div>
            <span v-else class="wb-muted">未发布</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="入库时间" width="140">
          <template #default="{ row }">
            <span class="wb-muted wb-num">{{ shortTime(row.approved_at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn width="170" align="right" class-name="action-cell">
          <template #default="{ row }">
            <ElButton link type="primary" @click="publish('listing', [row])">上架</ElButton>
            <ElButton
              link
              type="primary"
              :disabled="!row.has_video"
              @click="publish('promotion', [row])"
            >
              宣传
            </ElButton>
            <ElButton link type="primary" @click="open(row)">查看</ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
      <WbPagination
        v-model:page="page"
        v-model:page-size="pageSize"
        :total="total"
        @change="load"
      />
    </section>

    <div v-if="selected.length" class="wb-selection-bar">
      <span>已选 {{ selected.length }} 条内容</span>
      <ElButton link @click="table?.clearSelection()">取消选择</ElButton>
      <span class="wb-spacer" />
      <ElButton @click="publish('listing')">上架到闲鱼</ElButton>
      <ElButton type="primary" @click="publish('promotion')">发布宣传</ElButton>
    </div>

    <ElDrawer v-model="viewerOpen" size="600px" :title="viewing?.product.name">
      <template v-if="viewing">
        <div class="wb-section">
          <h3>宣传文案</h3>
          <p class="copy-title">{{ viewing.title }}</p>
          <p class="wb-text-block">{{ viewing.body }}</p>
        </div>
        <div class="wb-section">
          <h3>上架文案 <small v-if="viewing.category" class="wb-muted">分类：{{ viewing.category }}</small></h3>
          <p class="copy-title">{{ viewing.listing_title }}</p>
          <p class="wb-text-block">{{ viewing.listing_description }}</p>
        </div>
        <div v-if="viewingImages.length" class="wb-section">
          <h3>宣传图片</h3>
          <div class="wb-media-grid">
            <ElImage
              v-for="(url, index) in viewingImages"
              :key="url"
              :src="url"
              :preview-src-list="viewingImages"
              :initial-index="index"
              fit="cover"
              preview-teleported
              class="media-image"
            />
          </div>
        </div>
        <div v-if="viewingVideo" class="wb-section">
          <h3>宣传视频</h3>
          <video
            class="wb-video"
            :src="catalogMedia.content(viewing.id, viewingVideo.id)"
            controls
            preload="metadata"
          />
        </div>
        <p class="wb-muted approved-note">{{ shortTime(viewing.approved_at) }} 审核通过</p>
      </template>
      <template #footer>
        <ElButton v-if="viewing" @click="withdraw(viewing)">撤回</ElButton>
        <ElButton v-if="viewing" @click="publish('listing', [viewing])">上架到闲鱼</ElButton>
        <ElButton
          v-if="viewing"
          type="primary"
          :disabled="!viewing.has_video"
          @click="publish('promotion', [viewing])"
        >
          发布宣传
        </ElButton>
      </template>
    </ElDrawer>
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
.cell-text {
  display: grid;
  min-width: 0;
  gap: 2px;
}
.cell-text strong {
  font-weight: 500;
}
.cell-text small {
  font-size: 12px;
}
.link-like {
  max-width: 100%;
  padding: 0;
  color: var(--xi-semantic-color-text-primary);
  font: inherit;
  text-align: left;
  background: none;
  border: 0;
  cursor: pointer;
}
.link-like:hover {
  color: var(--xi-semantic-color-action-primary-default);
}
.publish-list {
  display: flex;
  gap: 4px 14px;
  flex-wrap: wrap;
}
/* Status as colored text beside the platform mark: one block of color per platform, not two. */
.publish-list .wb-pill {
  height: auto;
  padding: 0;
  background: none;
}
.content-table :deep(.action-cell .cell) {
  white-space: nowrap;
}
.content-table :deep(.action-cell .el-button + .el-button) {
  margin-left: 10px;
}
.content-table :deep(th.el-table__cell) {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  font-weight: 500;
  background: #fafbfc;
}
.copy-title {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 600;
}
.wb-section h3 small {
  margin-left: 6px;
  font-weight: 400;
}
.media-image {
  width: 100%;
  aspect-ratio: 1;
  border-radius: 4px;
}
.approved-note {
  margin-top: 20px;
  font-size: 12px;
}
@media (width < 700px) {
  .search {
    width: 100%;
  }
  .wb-page-actions {
    width: 100%;
  }
  .wb-selection-bar {
    flex-wrap: wrap;
  }
}
</style>
