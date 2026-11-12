<script setup lang="ts">
import type { ProductContent, PromotionPlatform, PublishRecord, Workflow } from '@/api/agent/catalog'
import { Search, View } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onActivated, onDeactivated, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { automationApi } from '@/api/agent/automation'
import { catalogApi, catalogMedia, recordStatusLabels } from '@/api/agent/catalog'
import { platformInitial, platformName, recordStatusClass, shortTime, yuan } from './catalogPresentation'
import ContentBrowserPanel from './ContentBrowserPanel.vue'
import { friendlyContentError } from './contentWorkflow'
import { copyProblems, mergeLimits } from './promotionPreview'
import PromotionPreview from './PromotionPreview.vue'

defineOptions({ name: 'CatalogPublishView' })
const props = defineProps<{ workflow: Workflow }>()
const route = useRoute()
const router = useRouter()
const isListing = computed(() => props.workflow === 'listing')
const contents = ref<ProductContent[]>([])
const query = ref('')
const loading = ref(false)
const chosen = ref<string[]>([])
const submitting = ref(false)
const records = ref<PublishRecord[]>([])
const listing = reactive({ condition: '全新', shippingFee: 0, location: '', includeGenerated: false })
const prices = reactive<Record<string, number | undefined>>({})
const platforms = ref<PromotionPlatform[]>(['douyin', 'xiaohongshu', 'kuaishou'])
const conditions = ['全新', '几乎全新', '轻微使用痕迹', '明显使用痕迹']
const promotionPlatforms: PromotionPlatform[] = ['douyin', 'xiaohongshu', 'kuaishou']
const LOCATION_KEY = 'catalog.listing.location'
const limits = ref(mergeLimits(undefined))
// The content shown in the preview; falls back to the first chosen one.
const focusId = ref('')

const visible = computed(() => {
  const keyword = query.value.trim()
  return keyword ? contents.value.filter(item => item.product.name.includes(keyword) || item.title.includes(keyword)) : contents.value
})
const chosenContents = computed(() => contents.value.filter(item => chosen.value.includes(item.id)))
const usable = (content: ProductContent) => isListing.value || content.has_video
const missingPrice = computed(() => isListing.value
  ? chosenContents.value.filter(item => !item.product.price && !prices[item.id])
  : [])
// The server rejects the whole submission when any copy is too long; Kuaishou's shared
// title + body limit would only fail later at the publisher, so it is caught here too.
const copyIssues = computed(() => isListing.value
  ? []
  : chosenContents.value.flatMap(content => platforms.value.flatMap(platform =>
      copyProblems(platform, platformName(platform), content, limits.value)
        .map(problem => `${content.product.name}：${problem}`))))
const recordCount = computed(() => isListing.value ? chosen.value.length : chosen.value.length * platforms.value.length)
const canSubmit = computed(() => chosen.value.length > 0 && (isListing.value
  ? !!listing.location.trim() && !missingPrice.value.length
  : platforms.value.length > 0 && !copyIssues.value.length))
const working = computed(() => records.value.some(record => ['queued', 'running', 'submitting'].includes(record.status)))
const previewContent = computed(() => contents.value.find(item => item.id === focusId.value) ?? chosenContents.value[0] ?? null)
const previewList = computed(() => {
  const focused = contents.value.find(item => item.id === focusId.value)
  return focused && !chosen.value.includes(focused.id) ? [...chosenContents.value, focused] : chosenContents.value
})
const previewPosition = computed(() => previewContent.value
  ? previewList.value.findIndex(item => item.id === previewContent.value!.id) + 1
  : 0)

async function load() {
  loading.value = true
  try {
    const result = await catalogApi.contents({ page_size: 100 })
    contents.value = result.items
    const requested = String(route.query.ids || '').split(',').filter(Boolean)
    if (requested.length) {
      chosen.value = requested.filter(id => contents.value.some(item => item.id === id && usable(item)))
      router.replace({ query: {} })
    }
    chosen.value = chosen.value.filter(id => contents.value.some(item => item.id === id))
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '读取内容失败'))
  }
  finally {
    loading.value = false
  }
}

async function loadRecords() {
  try {
    records.value = (await catalogApi.records({ workflow: props.workflow, page_size: 10 })).items
  }
  catch {
    records.value = []
  }
}

async function loadLimits() {
  try {
    limits.value = mergeLimits((await automationApi.platforms()).items)
  }
  catch {}
}

function toggle(content: ProductContent) {
  if (!usable(content)) {
    return
  }
  const selected = chosen.value.includes(content.id)
  chosen.value = selected ? chosen.value.filter(id => id !== content.id) : [...chosen.value, content.id]
  if (!selected) {
    focusId.value = content.id
  }
  else if (focusId.value === content.id) {
    focusId.value = ''
  }
}

function stepPreview(delta: number) {
  const list = previewList.value
  if (list.length > 1 && previewPosition.value) {
    focusId.value = list[(previewPosition.value - 1 + delta + list.length) % list.length].id
  }
}

async function submit() {
  if (!canSubmit.value) {
    return
  }
  submitting.value = true
  try {
    let result
    if (isListing.value) {
      try {
        localStorage.setItem(LOCATION_KEY, listing.location.trim())
      }
      catch {}
      const overrides: Record<string, number> = {}
      chosen.value.forEach((id) => {
        if (prices[id]) {
          overrides[id] = prices[id]!
        }
      })
      result = await catalogApi.publishListing({
        content_ids: chosen.value,
        condition: listing.condition,
        shipping_fee: listing.shippingFee,
        location: listing.location.trim(),
        prices: overrides,
        include_generated_images: listing.includeGenerated,
      })
    }
    else {
      result = await catalogApi.publishPromotion({ content_ids: chosen.value, platforms: platforms.value })
    }
    ElMessage.success(`已提交 ${result.total} 条，按顺序在专用浏览器中执行`)
    chosen.value = []
    focusId.value = ''
    await loadRecords()
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '提交失败'))
  }
  finally {
    submitting.value = false
  }
}

let timer: ReturnType<typeof setInterval> | undefined
function startPolling() {
  clearInterval(timer)
  timer = setInterval(() => {
    if (working.value) {
      void loadRecords()
    }
  }, 5000)
}
onMounted(() => {
  try {
    listing.location = localStorage.getItem(LOCATION_KEY) || ''
  }
  catch {}
})
onActivated(() => {
  void load()
  void loadRecords()
  if (!isListing.value) {
    void loadLimits()
  }
  startPolling()
})
onDeactivated(() => clearInterval(timer))
</script>

<template>
  <div class="wb-page">
    <header class="wb-page-head">
      <div>
        <h1>{{ isListing ? '商品上架' : '内容宣传' }}</h1>
        <p v-if="isListing">从内容库选择已审核的内容，用商品原图和上架文案发布到闲鱼。每个商品生成一条发布记录。</p>
        <p v-else>从内容库选择带视频的已审核内容，发布到抖音、小红书、快手。每个平台生成一条发布记录。</p>
      </div>
      <div class="wb-page-actions">
        <ElButton v-if="isListing" link @click="router.push('/agent/content/listing/rules')">已有商品上下架</ElButton>
        <ElButton @click="router.push('/agent/content/records')">全部发布记录</ElButton>
      </div>
    </header>

    <div class="publish-layout" :class="{ 'with-preview': !isListing }">
      <section class="wb-panel pick-panel">
        <div class="wb-panel-head">
          <h2>1. 选择内容 <small class="wb-muted">已选 {{ chosen.length }}</small></h2>
          <ElInput
            v-model="query"
            :prefix-icon="Search"
            size="small"
            placeholder="筛选"
            clearable
            class="pick-search"
          />
        </div>
        <div v-loading="loading" class="pick-list">
          <div v-if="!contents.length && !loading" class="wb-empty">
            内容库还是空的。先在
            <ElButton link type="primary" @click="router.push('/agent/content/products')">商品库</ElButton>
            批量生成，审核通过后会出现在这里。
          </div>
          <div
            v-for="content in visible"
            :key="content.id"
            class="pick-row"
            :class="{ chosen: chosen.includes(content.id), disabled: !usable(content) }"
            role="checkbox"
            :aria-checked="chosen.includes(content.id)"
            :aria-disabled="!usable(content)"
            tabindex="0"
            @click="toggle(content)"
            @keydown.space.prevent="toggle(content)"
          >
            <ElCheckbox :model-value="chosen.includes(content.id)" :disabled="!usable(content)" @click.prevent />
            <img
              v-if="content.product.image_id"
              class="wb-thumb"
              :src="catalogMedia.productImage(content.product.id, content.product.image_id, true)"
              alt=""
              loading="lazy"
            >
            <span v-else class="wb-thumb wb-thumb-empty">无图</span>
            <span class="pick-main">
              <strong class="wb-ellipsis">{{ content.product.name }}</strong>
              <small class="wb-ellipsis wb-muted">{{ isListing ? content.listing_title || content.title : content.title }}</small>
            </span>
            <span class="pick-side">
              <ElButton
                v-if="!isListing && usable(content)"
                link
                :icon="View"
                :type="previewContent?.id === content.id ? 'primary' : undefined"
                title="预览发布效果"
                aria-label="预览发布效果"
                @click.stop="focusId = content.id"
                @keydown.space.stop
              />
              <template v-if="isListing">
                <span class="wb-num" :class="{ 'wb-muted': !content.product.price }">{{ content.product.price ? yuan(content.product.price) : '未定价' }}</span>
              </template>
              <span v-else-if="!content.has_video" class="wb-muted">无视频</span>
              <span
                v-for="item in content.publishes.filter(value => value.workflow === workflow)"
                :key="item.platform"
                class="wb-platform"
                :class="`p-${item.platform}`"
                :title="`${platformName(item.platform)}：${recordStatusLabels[item.status]}`"
              >
                <i>{{ platformInitial[item.platform] }}</i>
              </span>
            </span>
          </div>
        </div>
      </section>

      <div class="side-column">
        <section class="wb-panel settings-panel">
          <div class="wb-panel-head"><h2>2. {{ isListing ? '上架设置' : '发布平台' }}</h2></div>
          <div class="wb-panel-body">
            <template v-if="isListing">
              <ElForm label-position="top" class="listing-form" @submit.prevent>
                <div class="form-row">
                  <ElFormItem label="成色">
                    <ElSelect v-model="listing.condition">
                      <ElOption
                        v-for="value in conditions"
                        :key="value"
                        :value="value"
                        :label="value"
                      />
                    </ElSelect>
                  </ElFormItem>
                  <ElFormItem label="运费（0 为包邮）">
                    <ElInputNumber
                      v-model="listing.shippingFee"
                      :min="0"
                      :precision="2"
                      controls-position="right"
                    />
                  </ElFormItem>
                </div>
                <ElFormItem label="宝贝所在地" required>
                  <ElInput v-model="listing.location" maxlength="200" placeholder="与闲鱼地址选择中的名称完全一致" />
                </ElFormItem>
                <ElFormItem label="闲鱼分类">
                  <span class="wb-muted">固定为“其他闲置”</span>
                </ElFormItem>
                <ElCheckbox v-model="listing.includeGenerated">在商品原图后附加 AI 宣传图</ElCheckbox>
              </ElForm>
              <div v-if="chosenContents.length" class="price-table">
                <div class="wb-field-label">售价</div>
                <div v-for="content in chosenContents" :key="content.id" class="price-row">
                  <span class="wb-ellipsis">{{ content.product.name }}</span>
                  <ElInputNumber
                    v-model="prices[content.id]"
                    size="small"
                    :min="0.01"
                    :precision="2"
                    controls-position="right"
                    :placeholder="content.product.price ? `${Number(content.product.price).toFixed(2)}` : '必填'"
                    :class="{ 'is-missing': !content.product.price && !prices[content.id] }"
                  />
                </div>
                <p class="wb-muted price-hint">不填则使用商品库里的价格。</p>
              </div>
            </template>
            <template v-else>
              <ElCheckboxGroup v-model="platforms" class="platform-choices">
                <ElCheckbox
                  v-for="platform in promotionPlatforms"
                  :key="platform"
                  :value="platform"
                  border
                >
                  <span class="wb-platform" :class="`p-${platform}`"><i>{{ platformInitial[platform] }}</i>{{ platformName(platform) }}</span>
                </ElCheckbox>
              </ElCheckboxGroup>
              <p class="wb-muted platform-hint">发布视频、标题和正文。标题或正文超出平台字数限制时无法提交，预览里会标红超出的部分。</p>
            </template>

            <ElAlert
              v-if="missingPrice.length"
              type="warning"
              :closable="false"
              class="form-alert"
              :title="`${missingPrice.length} 个商品没有售价，请在上方填写`"
            />
            <ElAlert
              v-if="copyIssues.length"
              type="warning"
              :closable="false"
              class="form-alert"
              title="文案超出平台字数限制，暂时不能提交"
            >
              <p v-for="issue in copyIssues.slice(0, 4)" :key="issue" class="issue">{{ issue }}</p>
              <p v-if="copyIssues.length > 4" class="issue">另有 {{ copyIssues.length - 4 }} 处</p>
              <p class="issue">可以取消勾选对应平台，或在内容库撤回后重新生成。</p>
            </ElAlert>
            <ElButton
              type="primary"
              class="submit"
              :loading="submitting"
              :disabled="!canSubmit"
              @click="submit"
            >
              {{ chosen.length ? `提交 ${recordCount} 条${isListing ? '上架' : '发布'}` : '请先选择内容' }}
            </ElButton>
          </div>
        </section>

        <PromotionPreview
          v-if="!isListing"
          :content="previewContent"
          :position="previewPosition"
          :total="previewList.length"
          :chosen="!!previewContent && chosen.includes(previewContent.id)"
          :platforms="platforms"
          :limits="limits"
          @step="stepPreview"
        />

        <section class="wb-panel account-panel">
          <div class="wb-panel-head"><h2>平台账号</h2></div>
          <div class="wb-panel-body">
            <ContentBrowserPanel :enabled="true" :purpose="workflow" />
          </div>
        </section>
      </div>
    </div>

    <section class="wb-panel">
      <div class="wb-panel-head">
        <h2>最近记录</h2>
        <ElButton link type="primary" @click="router.push('/agent/content/records')">查看全部</ElButton>
      </div>
      <div v-if="!records.length" class="wb-empty">还没有{{ isListing ? '上架' : '发布' }}记录</div>
      <div v-for="record in records" :key="record.id" class="record-row">
        <span class="wb-platform" :class="`p-${record.platform}`"><i>{{ platformInitial[record.platform] }}</i>{{ platformName(record.platform) }}</span>
        <span class="record-main wb-ellipsis">{{ record.product_name }} · {{ record.title }}</span>
        <span class="wb-pill" :class="recordStatusClass(record.status)">{{ recordStatusLabels[record.status] }}</span>
        <span class="record-extra wb-ellipsis">
          <a
            v-if="record.external_url"
            :href="record.external_url"
            target="_blank"
            rel="noopener"
          >查看</a>
          <span v-else-if="record.error" class="wb-muted" :title="record.error">{{ record.error }}</span>
        </span>
        <span class="wb-muted wb-num">{{ shortTime(record.created_at) }}</span>
      </div>
    </section>
  </div>
</template>

<style scoped>
.publish-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  gap: 16px;
  align-items: start;
}
/* Promotion: list, platforms and accounts stack on the left; the phone preview owns the
 * right column and stays in view while the left side scrolls. */
.with-preview {
  grid-template-columns: minmax(0, 1fr) 340px;
  grid-template-rows: auto auto 1fr;
}
.with-preview .side-column {
  display: contents;
}
.with-preview .pick-panel {
  grid-area: 1 / 1;
}
.with-preview .settings-panel {
  grid-area: 2 / 1;
}
.with-preview .account-panel {
  grid-area: 3 / 1;
}
.with-preview .preview-panel {
  position: sticky;
  top: 16px;
  grid-area: 1 / 2 / 4 / 3;
}
.with-preview .settings-panel .wb-panel-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  column-gap: 16px;
  align-items: center;
}
.with-preview .platform-choices {
  grid-column: 1 / -1;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.with-preview .form-alert {
  grid-column: 1 / -1;
}
.with-preview .submit {
  grid-column: 2;
  width: auto;
  min-width: 180px;
  margin-top: 12px;
}
.with-preview .platform-hint {
  margin-top: 12px;
}
.with-preview .account-panel :deep(.browser-connections) {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.wb-panel-head h2 small {
  margin-left: 6px;
  font-weight: 400;
}
.pick-search {
  width: 180px;
}
.pick-list {
  min-height: 240px;
  max-height: 620px;
  overflow-y: auto;
}
.pick-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--xi-semantic-color-divider-default);
  cursor: pointer;
}
.pick-row:hover {
  background: var(--wb-hover);
}
.pick-row.chosen {
  background: var(--wb-selected);
}
.pick-row.disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.pick-row:focus-visible {
  outline: 2px solid var(--xi-semantic-color-action-primary-default);
  outline-offset: -2px;
}
.pick-main {
  display: grid;
  flex: 1;
  min-width: 0;
  gap: 2px;
}
.pick-main strong {
  font-size: 13px;
  font-weight: 500;
}
.pick-main small {
  font-size: 12px;
}
.pick-side {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.pick-side :deep(.el-button.is-link) {
  height: auto;
  padding: 4px;
  margin: 0;
  font-size: 16px;
}
.side-column {
  display: grid;
  gap: 16px;
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.listing-form :deep(.el-select),
.listing-form :deep(.el-input-number) {
  width: 100%;
}
.price-table {
  margin-top: 16px;
}
.price-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 140px;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 13px;
}
.price-row .is-missing :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--xi-semantic-color-feedback-warning-default) inset;
}
.price-hint,
.platform-hint {
  margin: 6px 0 0;
  font-size: 12px;
}
.platform-choices {
  display: grid;
  gap: 8px;
}
.platform-choices :deep(.el-checkbox) {
  width: 100%;
  margin: 0;
}
.form-alert {
  margin-top: 12px;
}
.issue {
  margin: 2px 0 0;
  font-size: 12px;
}
.submit {
  width: 100%;
  margin-top: 16px;
}
.account-panel :deep(.browser-connections) {
  margin: 0;
}
.account-panel :deep(.browser-heading) {
  display: none;
}
.account-panel :deep(.browser-platform) {
  padding: 12px;
  border-radius: 6px;
}
.account-panel :deep(.browser-actions) {
  gap: 6px;
  margin-top: 8px;
}
.account-panel :deep(.browser-actions .el-button) {
  padding: 5px 8px;
  font-size: 12px;
}
.record-row {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr) 72px minmax(0, 220px) 120px;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  font-size: 13px;
  border-bottom: 1px solid var(--xi-semantic-color-divider-default);
}
.record-row:last-child {
  border-bottom: 0;
}
.record-extra a {
  color: var(--xi-semantic-color-link-default);
}
@media (width < 1400px) {
  .with-preview .account-panel :deep(.browser-connections) {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (width < 1100px) {
  .publish-layout {
    grid-template-columns: 1fr;
  }
  .with-preview {
    grid-template-rows: none;
  }
  .with-preview .pick-panel,
  .with-preview .settings-panel,
  .with-preview .preview-panel,
  .with-preview .account-panel {
    grid-area: auto;
  }
  .with-preview .preview-panel {
    position: static;
  }
}
@media (width < 700px) {
  .record-row {
    grid-template-columns: auto minmax(0, 1fr) auto;
  }
  .record-extra,
  .record-row > .wb-num {
    display: none;
  }
  .with-preview .settings-panel .wb-panel-body {
    display: block;
  }
  .with-preview .platform-choices {
    grid-template-columns: minmax(0, 1fr);
  }
  .with-preview .submit {
    width: 100%;
  }
}
</style>
