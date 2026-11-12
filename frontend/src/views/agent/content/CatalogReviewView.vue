<script setup lang="ts">
import type { GenerationBatch, GenerationItem, GenerationPart, ItemStatus } from '@/api/agent/catalog'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { catalogApi, catalogMedia, itemStatusLabels, partLabels } from '@/api/agent/catalog'
import { itemStatusClass, shortTime, yuan } from './catalogPresentation'
import { friendlyContentError } from './contentWorkflow'
import { pendingReviewCount } from './reviewBadge'
import WbPagination from './WbPagination.vue'

defineOptions({ name: 'CatalogReviewView' })

type Filter = 'review' | 'running' | 'failed' | 'rejected' | 'approved' | 'all'

const route = useRoute()
const router = useRouter()
const filters: Array<{ key: Filter, label: string, statuses: ItemStatus[] }> = [
  { key: 'review', label: '待审核', statuses: ['review'] },
  { key: 'running', label: '生成中', statuses: ['queued', 'running'] },
  { key: 'failed', label: '失败', statuses: ['failed'] },
  { key: 'rejected', label: '驳回', statuses: ['rejected'] },
  { key: 'approved', label: '已入库', statuses: ['approved'] },
  { key: 'all', label: '全部', statuses: [] },
]
const filter = ref<Filter>(filters.some(item => item.key === route.query.status) ? route.query.status as Filter : 'review')
const batchId = ref('')
const batches = ref<GenerationBatch[]>([])
const items = ref<GenerationItem[]>([])
const counts = ref<Partial<Record<ItemStatus, number>>>({})
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const activeId = ref('')
const checked = ref<string[]>([])
const busy = ref(false)
const editing = ref(false)
const draft = reactive({ title: '', body: '', listing_title: '', listing_description: '', category: '' })
const regenOpen = ref(false)
const regen = reactive({ parts: ['copy'] as GenerationPart[], feedback: '' })
const rejectOpen = ref(false)
const rejectNote = ref('')

const active = computed(() => items.value.find(item => item.id === activeId.value) ?? null)
const images = computed(() => active.value?.media.filter(media => media.kind === 'image') ?? [])
const video = computed(() => active.value?.media.find(media => media.kind === 'video') ?? null)
const imageUrls = computed(() => images.value.map(media => catalogMedia.item(active.value!.id, media.id)))
const reviewable = computed(() => !!active.value && ['review', 'rejected', 'failed'].includes(active.value.status))
const reviewIds = computed(() => items.value.filter(item => item.status === 'review').map(item => item.id))
const working = computed(() => (counts.value.queued ?? 0) + (counts.value.running ?? 0))
const filterCount = (key: Filter) => key === 'all'
  ? Object.values(counts.value).reduce((sum, value) => sum + (value ?? 0), 0)
  : filters.find(item => item.key === key)!.statuses.reduce((sum, status) => sum + (counts.value[status] ?? 0), 0)

async function load(options: { quiet?: boolean } = {}) {
  if (!options.quiet) {
    loading.value = true
  }
  try {
    const statuses = filters.find(item => item.key === filter.value)!.statuses
    const result = await catalogApi.items({
      status: statuses.join(',') || undefined,
      batch_id: batchId.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    items.value = result.items
    counts.value = result.counts
    pendingReviewCount.value = result.counts.review ?? 0
    total.value = result.total
    checked.value = checked.value.filter(id => reviewIds.value.includes(id))
    if (!items.value.some(item => item.id === activeId.value)) {
      activeId.value = items.value[0]?.id ?? ''
    }
  }
  catch (error) {
    if (!options.quiet) {
      ElMessage.error(friendlyContentError(error, '读取失败'))
    }
  }
  finally {
    loading.value = false
  }
}

async function loadBatches() {
  try {
    batches.value = (await catalogApi.batches()).items
  }
  catch {
    batches.value = []
  }
}

function setFilter(key: Filter) {
  filter.value = key
  page.value = 1
  router.replace({ query: { ...route.query, status: key } })
  void load()
}

function replaceItem(item: GenerationItem) {
  const index = items.value.findIndex(value => value.id === item.id)
  if (index >= 0) {
    items.value[index] = item
  }
}

function startEdit() {
  if (!active.value) {
    return
  }
  Object.assign(draft, {
    title: active.value.title,
    body: active.value.body,
    listing_title: active.value.listing_title,
    listing_description: active.value.listing_description,
    category: active.value.category,
  })
  editing.value = true
}

async function saveEdit() {
  if (!active.value) {
    return
  }
  busy.value = true
  try {
    replaceItem(await catalogApi.editItem(active.value.id, { ...draft }))
    editing.value = false
    ElMessage.success('已保存修改')
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '保存失败'))
  }
  finally {
    busy.value = false
  }
}

async function approve(item = active.value) {
  if (!item) {
    return
  }
  busy.value = true
  try {
    await catalogApi.approve(item.id, item.revision)
    ElMessage.success('已通过，内容已入库')
    await load({ quiet: true })
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '审核失败'))
  }
  finally {
    busy.value = false
  }
}

async function approveChecked() {
  busy.value = true
  try {
    const result = await catalogApi.approveMany(checked.value)
    checked.value = []
    if (result.skipped.length) {
      ElMessage.warning(`通过 ${result.approved.length} 个，${result.skipped.length} 个未通过：${result.skipped[0].reason}`)
    }
    else {
      ElMessage.success(`已通过 ${result.approved.length} 个，内容已入库`)
    }
    await load({ quiet: true })
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '批量审核失败'))
  }
  finally {
    busy.value = false
  }
}

function openRegenerate(parts?: GenerationPart[]) {
  const options = active.value?.options ?? {}
  const unfinished = active.value?.status === 'failed' ? active.value.pending_parts : []
  regen.parts = parts ?? (unfinished.length ? [...unfinished] : (Object.keys(partLabels) as GenerationPart[]).filter(part => options[part]))
  regen.feedback = ''
  regenOpen.value = true
}

async function submitRegenerate(parts = regen.parts, feedback = regen.feedback) {
  if (!active.value || !parts.length) {
    return
  }
  busy.value = true
  try {
    replaceItem(await catalogApi.regenerate(active.value.id, parts, feedback.trim()))
    regenOpen.value = false
    ElMessage.success('已提交重新生成')
    void load({ quiet: true })
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '提交失败'))
  }
  finally {
    busy.value = false
  }
}

async function submitReject() {
  if (!active.value) {
    return
  }
  busy.value = true
  try {
    await catalogApi.reject(active.value.id, rejectNote.value.trim())
    rejectOpen.value = false
    rejectNote.value = ''
    await load({ quiet: true })
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '驳回失败'))
  }
  finally {
    busy.value = false
  }
}

watch(activeId, () => {
  editing.value = false
})

let timer: ReturnType<typeof setInterval> | undefined
function startPolling() {
  clearInterval(timer)
  timer = setInterval(() => {
    if (working.value && !editing.value && !busy.value) {
      void load({ quiet: true })
    }
  }, 4000)
}
onMounted(() => {
  void load()
  void loadBatches()
  startPolling()
})
onActivated(() => {
  void load({ quiet: true })
  void loadBatches()
  startPolling()
})
onDeactivated(() => clearInterval(timer))
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div class="wb-page review-page">
    <header class="wb-page-head">
      <div>
        <h1>生成与审核</h1>
        <p>AI 生成的内容在这里由管理员审核。通过后进入内容库，供上架和宣传使用。</p>
      </div>
      <div class="wb-page-actions">
        <span v-if="working" class="wb-pill is-primary">{{ working }} 个生成中</span>
        <ElSelect
          v-model="batchId"
          placeholder="全部批次"
          clearable
          class="batch-select"
          @change="() => { page = 1; load() }"
        >
          <ElOption
            v-for="batch in batches"
            :key="batch.id"
            :value="batch.id"
            :label="`${batch.name}（${shortTime(batch.created_at)}）`"
          />
        </ElSelect>
        <ElButton :icon="Refresh" @click="load()">刷新</ElButton>
      </div>
    </header>

    <section class="wb-panel review-layout">
      <div class="review-list">
        <div class="wb-toolbar">
          <div class="wb-tabs">
            <button
              v-for="item in filters"
              :key="item.key"
              type="button"
              :class="{ active: filter === item.key }"
              @click="setFilter(item.key)"
            >
              {{ item.label }}<small>{{ filterCount(item.key) }}</small>
            </button>
          </div>
        </div>
        <div v-if="reviewIds.length" class="list-bulk">
          <ElCheckbox
            :model-value="checked.length > 0 && checked.length === reviewIds.length"
            :indeterminate="checked.length > 0 && checked.length < reviewIds.length"
            @change="checked = $event ? [...reviewIds] : []"
          >
            全选待审核
          </ElCheckbox>
          <ElButton
            v-if="checked.length"
            size="small"
            type="primary"
            :loading="busy"
            @click="approveChecked"
          >
            通过选中的 {{ checked.length }} 个
          </ElButton>
        </div>
        <div v-loading="loading" class="list-scroll">
          <div v-if="!items.length && !loading" class="wb-empty">
            {{ filter === 'review' ? '没有待审核的内容。到商品库勾选商品，点「批量生成」。' : '没有记录' }}
          </div>
          <div
            v-for="item in items"
            :key="item.id"
            class="list-row"
            :class="{ active: item.id === activeId }"
            role="button"
            tabindex="0"
            @click="activeId = item.id"
            @keydown.enter="activeId = item.id"
          >
            <ElCheckbox
              v-if="item.status === 'review'"
              :model-value="checked.includes(item.id)"
              @click.stop
              @change="checked = $event ? [...checked, item.id] : checked.filter(id => id !== item.id)"
            />
            <span v-else class="check-placeholder" />
            <img
              v-if="item.product.image_id"
              class="wb-thumb"
              :src="catalogMedia.productImage(item.product.id, item.product.image_id, true)"
              alt=""
              loading="lazy"
            >
            <span v-else class="wb-thumb wb-thumb-empty">无图</span>
            <span class="row-main">
              <strong class="wb-ellipsis">{{ item.product.name }}</strong>
              <small class="wb-ellipsis wb-muted">{{ item.title || item.error || '等待生成' }}</small>
            </span>
            <span class="row-side">
              <span class="wb-pill" :class="itemStatusClass(item.status)">{{ itemStatusLabels[item.status] }}</span>
              <small class="wb-muted wb-num">{{ shortTime(item.updated_at).slice(5) }}</small>
            </span>
          </div>
        </div>
        <WbPagination
          v-model:page="page"
          v-model:page-size="pageSize"
          :total="total"
          compact
          @change="load()"
        />
      </div>

      <div class="review-detail">
        <div v-if="!active" class="wb-empty">选择左侧一条记录查看</div>
        <template v-else>
          <div class="detail-head">
            <div class="detail-title">
              <h2>{{ active.product.name }}</h2>
              <p class="wb-muted">
                {{ active.product.price ? yuan(active.product.price) : '未定价' }} · {{ active.batch_name }}
                <template v-if="active.revision"> · 第 {{ active.revision }} 版</template>
              </p>
            </div>
            <span class="wb-pill" :class="itemStatusClass(active.status)">{{ itemStatusLabels[active.status] }}</span>
          </div>

          <div class="detail-body">
            <ElAlert
              v-if="active.status === 'failed' && active.error"
              type="error"
              :closable="false"
              :title="active.error"
              show-icon
            />
            <ElAlert
              v-if="active.status === 'rejected' && active.review_note"
              type="info"
              :closable="false"
              :title="`驳回原因：${active.review_note}`"
            />
            <ElAlert
              v-if="['queued', 'running'].includes(active.status)"
              type="info"
              :closable="false"
              show-icon
              :title="`正在生成：${active.pending_parts.map(part => partLabels[part]).join('、') || '内容'}。完成后自动刷新。`"
            />
            <ElAlert
              v-if="active.video_stale"
              type="warning"
              :closable="false"
              show-icon
              title="文案已修改，视频里的字幕还是旧文案。"
            >
              <ElButton size="small" :loading="busy" @click="submitRegenerate(['video'], '')">重新合成视频</ElButton>
            </ElAlert>

            <template v-if="editing">
              <div class="wb-section">
                <h3>宣传文案</h3>
                <ElInput
                  v-model="draft.title"
                  maxlength="120"
                  show-word-limit
                  placeholder="宣传标题"
                />
                <ElInput
                  v-model="draft.body"
                  class="gap"
                  type="textarea"
                  :rows="6"
                  maxlength="4000"
                  show-word-limit
                  placeholder="宣传正文"
                />
              </div>
              <div class="wb-section">
                <h3>上架文案</h3>
                <ElInput
                  v-model="draft.listing_title"
                  maxlength="30"
                  show-word-limit
                  placeholder="商品标题（闲鱼最多 30 字）"
                />
                <ElInput
                  v-model="draft.listing_description"
                  class="gap"
                  type="textarea"
                  :rows="5"
                  maxlength="1000"
                  show-word-limit
                  placeholder="商品描述"
                />
                <ElInput
                  v-model="draft.category"
                  class="gap"
                  maxlength="64"
                  placeholder="闲鱼分类，例如：白酒"
                />
              </div>
            </template>
            <template v-else-if="active.title">
              <div class="wb-section">
                <h3>宣传文案</h3>
                <p class="copy-title">{{ active.title || '—' }}</p>
                <p class="wb-text-block">{{ active.body || '—' }}</p>
              </div>
              <div class="wb-section">
                <h3>上架文案 <small v-if="active.category" class="wb-muted">分类：{{ active.category }}</small></h3>
                <p class="copy-title">{{ active.listing_title || '—' }}</p>
                <p class="wb-text-block">{{ active.listing_description || '—' }}</p>
              </div>
            </template>

            <div v-if="images.length || video" class="wb-section media-section">
              <div v-if="video" class="media-video">
                <h3>宣传视频 <small v-if="active.video_model" class="wb-muted">{{ active.video_model }}</small></h3>
                <video
                  :key="video.id"
                  class="wb-video"
                  :src="catalogMedia.item(active.id, video.id)"
                  controls
                  preload="metadata"
                />
                <p v-if="active.video_note" class="video-note">{{ active.video_note }}</p>
              </div>
              <div v-if="images.length" class="media-images">
                <h3>宣传图片 <small v-if="active.image_model" class="wb-muted">{{ active.image_model }}</small></h3>
                <div class="wb-media-grid">
                  <ElImage
                    v-for="(url, index) in imageUrls"
                    :key="url"
                    :src="url"
                    :preview-src-list="imageUrls"
                    :initial-index="index"
                    fit="cover"
                    preview-teleported
                    class="media-image"
                  />
                </div>
              </div>
            </div>
            <p v-if="active.model" class="model-note wb-muted">文案模型：{{ active.model }}</p>
          </div>

          <div class="detail-actions">
            <template v-if="editing">
              <ElButton @click="editing = false">取消</ElButton>
              <ElButton type="primary" :loading="busy" @click="saveEdit">保存修改</ElButton>
            </template>
            <template v-else-if="active.status === 'approved'">
              <span class="wb-muted">已于 {{ shortTime(active.reviewed_at) }} 通过</span>
              <ElButton @click="router.push('/agent/content/contents')">去内容库</ElButton>
            </template>
            <template v-else-if="reviewable">
              <ElButton v-if="active.status !== 'rejected'" :disabled="busy" @click="rejectOpen = true">驳回</ElButton>
              <ElButton :disabled="busy" @click="openRegenerate()">重新生成</ElButton>
              <ElButton v-if="active.title" :disabled="busy" @click="startEdit">修改文案</ElButton>
              <span class="wb-spacer" />
              <ElButton
                v-if="active.status === 'review'"
                type="primary"
                :loading="busy"
                :disabled="active.video_stale"
                @click="approve()"
              >
                通过并入库
              </ElButton>
            </template>
          </div>
        </template>
      </div>
    </section>

    <ElDialog
      v-model="regenOpen"
      title="重新生成"
      width="480px"
      append-to-body
    >
      <div class="wb-field-label">重新生成哪些内容</div>
      <ElCheckboxGroup v-model="regen.parts" class="regen-parts">
        <ElCheckbox v-for="(label, part) in partLabels" :key="part" :value="part">{{ label }}</ElCheckbox>
      </ElCheckboxGroup>
      <p class="regen-hint wb-muted">只重新生成文案时，如已有视频会自动用新文案重新合成字幕。</p>
      <div class="wb-field-label">修改意见</div>
      <ElInput
        v-model="regen.feedback"
        type="textarea"
        :rows="4"
        maxlength="2000"
        placeholder="例如：标题更短一些，突出年份和口感，去掉“送礼”相关的说法"
      />
      <template #footer>
        <ElButton @click="regenOpen = false">取消</ElButton>
        <ElButton
          type="primary"
          :loading="busy"
          :disabled="!regen.parts.length"
          @click="submitRegenerate()"
        >
          提交
        </ElButton>
      </template>
    </ElDialog>

    <ElDialog
      v-model="rejectOpen"
      title="驳回"
      width="420px"
      append-to-body
    >
      <ElInput
        v-model="rejectNote"
        type="textarea"
        :rows="3"
        maxlength="1000"
        placeholder="驳回原因（选填）。驳回后仍可修改或重新生成。"
      />
      <template #footer>
        <ElButton @click="rejectOpen = false">取消</ElButton>
        <ElButton type="danger" :loading="busy" @click="submitReject">驳回</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.batch-select {
  width: 260px;
}
.review-layout {
  display: grid;
  grid-template-columns: 380px minmax(0, 1fr);
  min-height: calc(100dvh - 150px);
}
.review-list {
  display: flex;
  min-width: 0;
  flex-direction: column;
  border-right: 1px solid var(--xi-semantic-color-border-subtle);
}
.review-list .wb-tabs {
  flex-wrap: nowrap;
  overflow-x: auto;
  scrollbar-width: none;
}
.review-list .wb-tabs button {
  flex: none;
  padding: 0 8px;
}
.list-bulk {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 40px;
  padding: 0 16px;
  font-size: 13px;
  border-bottom: 1px solid var(--xi-semantic-color-border-subtle);
}
.list-scroll {
  flex: 1;
  min-height: 200px;
  max-height: calc(100dvh - 240px);
  overflow-y: auto;
}
.list-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--xi-semantic-color-divider-default);
  cursor: pointer;
}
.list-row:hover {
  background: var(--wb-hover);
}
.list-row.active {
  background: var(--wb-selected);
  box-shadow: inset 2px 0 0 var(--xi-semantic-color-action-primary-default);
}
.list-row:focus-visible {
  outline: 2px solid var(--xi-semantic-color-action-primary-default);
  outline-offset: -2px;
}
.check-placeholder {
  width: 14px;
  flex: none;
}
.row-main {
  display: grid;
  flex: 1;
  min-width: 0;
  gap: 2px;
}
.row-main strong {
  font-size: 13px;
  font-weight: 500;
}
.row-main small,
.row-side small {
  font-size: 12px;
}
.row-side {
  display: grid;
  justify-items: end;
  gap: 4px;
}
.review-detail {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--xi-semantic-color-border-subtle);
}
.detail-title h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.detail-title p {
  margin: 2px 0 0;
  font-size: 13px;
}
.detail-body {
  display: grid;
  flex: 1;
  align-content: start;
  gap: 4px;
  padding: 16px 20px;
  overflow-y: auto;
}
.detail-body > .el-alert {
  margin-bottom: 12px;
}
.detail-body :deep(.el-alert__description) {
  margin-top: 6px;
}
.wb-section h3 small {
  margin-left: 6px;
  font-weight: 400;
}
.copy-title {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 600;
}
.gap {
  margin-top: 8px;
}
/* Video and images sit side by side from the left, so a single image never leaves a gap
 * across the whole panel. */
.media-section {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 24px;
}
.media-video {
  flex: none;
  width: 240px;
}
.media-images {
  flex: 1;
  min-width: 260px;
}
.media-images .wb-media-grid {
  grid-template-columns: repeat(auto-fill, 120px);
}
.media-image {
  width: 100%;
  aspect-ratio: 1;
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 4px;
}
.video-note {
  max-width: 240px;
  margin: 6px 0 0;
  color: var(--xi-semantic-color-feedback-warning-text);
  font-size: 12px;
  line-height: 1.5;
}
.model-note {
  margin: 16px 0 0;
  font-size: 12px;
}
.detail-actions {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  background: #fff;
  border-top: 1px solid var(--xi-semantic-color-border-subtle);
}
.detail-actions .wb-spacer {
  flex: 1;
}
.regen-parts {
  display: flex;
  gap: 4px 16px;
  flex-wrap: wrap;
}
.regen-hint {
  margin: 6px 0 16px;
  font-size: 12px;
}
@media (width < 1100px) {
  .review-layout {
    grid-template-columns: 1fr;
  }
  .review-list {
    border-right: 0;
    border-bottom: 1px solid var(--xi-semantic-color-border-subtle);
  }
  .list-scroll {
    max-height: 360px;
  }
  .media-video {
    width: min(240px, 100%);
  }
}
@media (width < 700px) {
  .batch-select {
    width: 100%;
  }
  .detail-actions {
    flex-wrap: wrap;
  }
}
</style>
