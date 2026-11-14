<script setup lang="ts">
import type { ProductContent, PublishRecord } from '@/api/agent/catalog'
import { Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onActivated, onDeactivated, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { catalogApi, catalogMedia, recordStatusLabels } from '@/api/agent/catalog'
import { platformInitial, platformName, recordStatusClass, shortTime, yuan } from './catalogPresentation'
import ContentBrowserPanel from './ContentBrowserPanel.vue'
import { friendlyContentError } from './contentWorkflow'
import ModuleTabs from './ModuleTabs.vue'
import WbPagination from './WbPagination.vue'

/* 闲鱼上架 as a four-step wizard: choose products → edit listing details → confirm → done.
 * Only the current step's content is shown; the bar at the bottom moves between steps. */
defineOptions({ name: 'CatalogListingWizard' })
const route = useRoute()
const router = useRouter()

const STEPS = ['选择商品', '编辑信息', '确认发布', '完成']
const DRAFT_KEY = 'catalog.listing.draft'
const LOCATION_KEY = 'catalog.listing.location'
const conditions = ['全新', '几乎全新', '轻微使用痕迹', '明显使用痕迹']

const step = ref(0)
const contents = ref<ProductContent[]>([])
const loading = ref(false)
const query = ref('')
const page = ref(1)
const pageSize = ref(10)
const chosen = ref<string[]>([])
const listing = reactive({ condition: '全新', shippingFee: 0, location: '', includeGenerated: false })
const prices = reactive<Record<string, number | undefined>>({})
const submitting = ref(false)
const submittedIds = ref<string[]>([])
const submitted = ref<PublishRecord[]>([])

const filtered = computed(() => {
  const keyword = query.value.trim()
  return keyword
    ? contents.value.filter(item => item.product.name.includes(keyword) || (item.listing_title || item.title).includes(keyword))
    : contents.value
})
const pageItems = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const pageAllChosen = computed(() => pageItems.value.length > 0 && pageItems.value.every(item => chosen.value.includes(item.id)))
const pageSomeChosen = computed(() => !pageAllChosen.value && pageItems.value.some(item => chosen.value.includes(item.id)))
const chosenContents = computed(() => contents.value.filter(item => chosen.value.includes(item.id)))
const priceOf = (content: ProductContent) => prices[content.id] || (content.product.price ? Number(content.product.price) : undefined)
const missingPrice = computed(() => chosenContents.value.filter(item => !priceOf(item)))
const stepProblem = computed(() => {
  if (step.value === 0 && !chosen.value.length) {
    return '请至少选择一个商品'
  }
  if (step.value === 1 && !listing.location.trim()) {
    return '请填写宝贝所在地'
  }
  if (step.value === 1 && missingPrice.value.length) {
    return `${missingPrice.value.length} 个商品还没有售价`
  }
  return ''
})
const working = computed(() => submitted.value.some(record => ['queued', 'running', 'submitting'].includes(record.status)))

watch(query, () => {
  page.value = 1
})

async function load() {
  loading.value = true
  try {
    contents.value = (await catalogApi.contents({ page_size: 100 })).items
    const requested = String(route.query.ids || '').split(',').filter(Boolean)
    if (requested.length) {
      chosen.value = requested.filter(id => contents.value.some(item => item.id === id))
      step.value = 0
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

function toggle(content: ProductContent) {
  chosen.value = chosen.value.includes(content.id)
    ? chosen.value.filter(id => id !== content.id)
    : [...chosen.value, content.id]
}

function togglePage(value: boolean | string | number) {
  const ids = pageItems.value.map(item => item.id)
  chosen.value = value
    ? [...new Set([...chosen.value, ...ids])]
    : chosen.value.filter(id => !ids.includes(id))
}

function goTo(target: number) {
  // Earlier steps are always open; later ones only through 下一步, which checks the current step.
  if (target < step.value && step.value < 3) {
    step.value = target
  }
}

function next() {
  if (stepProblem.value) {
    ElMessage.warning(stepProblem.value)
    return
  }
  step.value = Math.min(step.value + 1, 2)
}

function saveDraft() {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      step: step.value,
      chosen: chosen.value,
      listing: { ...listing },
      prices: { ...prices },
    }))
    ElMessage.success('草稿已保存，下次打开会自动恢复')
  }
  catch {
    ElMessage.error('浏览器不允许保存草稿')
  }
}

function restoreDraft() {
  try {
    const draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null')
    if (draft && Array.isArray(draft.chosen)) {
      chosen.value = draft.chosen.filter((id: unknown) => typeof id === 'string')
      Object.assign(listing, draft.listing || {})
      Object.assign(prices, draft.prices || {})
      step.value = Math.min(Math.max(Number(draft.step) || 0, 0), 2)
      return true
    }
  }
  catch {}
  return false
}

function clearDraft() {
  try {
    localStorage.removeItem(DRAFT_KEY)
  }
  catch {}
}

async function submit() {
  if (submitting.value) {
    return
  }
  submitting.value = true
  try {
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
    const result = await catalogApi.publishListing({
      content_ids: chosen.value,
      condition: listing.condition,
      shipping_fee: listing.shippingFee,
      location: listing.location.trim(),
      prices: overrides,
      include_generated_images: listing.includeGenerated,
    })
    submittedIds.value = result.items
    submitted.value = []
    clearDraft()
    step.value = 3
    await loadSubmitted()
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '提交失败'))
  }
  finally {
    submitting.value = false
  }
}

async function loadSubmitted() {
  if (!submittedIds.value.length) {
    return
  }
  try {
    const records = (await catalogApi.records({ workflow: 'listing', page_size: 100 })).items
    submitted.value = submittedIds.value
      .map(id => records.find(record => record.id === id))
      .filter((record): record is PublishRecord => !!record)
  }
  catch {}
}

function restart() {
  chosen.value = []
  Object.keys(prices).forEach(key => delete prices[key])
  submittedIds.value = []
  submitted.value = []
  step.value = 0
  void load()
}

let timer: ReturnType<typeof setInterval> | undefined
let restored = false
onActivated(() => {
  if (!restored) {
    restored = true
    try {
      listing.location = localStorage.getItem(LOCATION_KEY) || ''
    }
    catch {}
    if (restoreDraft()) {
      ElMessage.info('已恢复上次保存的草稿')
    }
  }
  void load()
  clearInterval(timer)
  timer = setInterval(() => {
    if (step.value === 3 && (working.value || submitted.value.length < submittedIds.value.length)) {
      void loadSubmitted()
    }
  }, 5000)
})
onDeactivated(() => clearInterval(timer))
</script>

<template>
  <div class="wb-page listing-wizard">
    <ModuleTabs workflow="listing">
      <ElButton link @click="router.push('/agent/content/listing/rules')">已有商品上下架</ElButton>
    </ModuleTabs>

    <section class="wb-panel wizard">
      <ol class="wizard-steps" aria-label="上架步骤">
        <li
          v-for="(label, index) in STEPS"
          :key="label"
          :class="{ active: index === step, done: index < step }"
          :aria-current="index === step ? 'step' : undefined"
        >
          <button type="button" :disabled="index >= step || step === 3" @click="goTo(index)">
            <span class="dot">{{ index < step ? '✓' : index + 1 }}</span>
            <span>{{ label }}</span>
          </button>
        </li>
      </ol>

      <div class="wizard-body">
        <!-- ① 选择商品 -->
        <template v-if="step === 0">
          <div class="body-head">
            <ElCheckbox
              :model-value="pageAllChosen"
              :indeterminate="pageSomeChosen"
              :disabled="!pageItems.length"
              @change="togglePage"
            >
              全选本页
            </ElCheckbox>
            <span class="wb-muted">已选 {{ chosen.length }} 个商品</span>
            <ElInput
              v-model="query"
              :prefix-icon="Search"
              size="small"
              placeholder="按商品名或标题筛选"
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
              v-for="content in pageItems"
              :key="content.id"
              class="pick-row"
              :class="{ chosen: chosen.includes(content.id) }"
              role="checkbox"
              :aria-checked="chosen.includes(content.id)"
              tabindex="0"
              @click="toggle(content)"
              @keydown.space.prevent="toggle(content)"
            >
              <ElCheckbox :model-value="chosen.includes(content.id)" @click.prevent />
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
                <small class="wb-ellipsis wb-muted">{{ content.listing_title || content.title }}</small>
              </span>
              <span class="pick-side">
                <span class="wb-num" :class="{ 'wb-muted': !content.product.price }">{{ content.product.price ? yuan(content.product.price) : '未定价' }}</span>
                <span
                  v-for="item in content.publishes.filter(value => value.workflow === 'listing')"
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
          <WbPagination
            v-if="filtered.length > 10"
            v-model:page="page"
            v-model:page-size="pageSize"
            :total="filtered.length"
          />
        </template>

        <!-- ② 编辑信息 -->
        <template v-else-if="step === 1">
          <div class="edit-grid">
            <ElForm label-position="top" class="listing-form" @submit.prevent>
              <h3>上架设置 <small class="wb-muted">对本次选择的所有商品生效</small></h3>
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
            <div class="price-table">
              <h3>售价 <small class="wb-muted">不填则使用商品库里的价格</small></h3>
              <div v-for="content in chosenContents" :key="content.id" class="price-row">
                <span class="price-name">
                  <strong class="wb-ellipsis">{{ content.product.name }}</strong>
                  <small class="wb-ellipsis wb-muted">{{ content.listing_title || content.title }}</small>
                </span>
                <ElInputNumber
                  v-model="prices[content.id]"
                  size="small"
                  :min="0.01"
                  :precision="2"
                  controls-position="right"
                  :placeholder="content.product.price ? `${Number(content.product.price).toFixed(2)}` : '必填'"
                  :class="{ 'is-missing': !priceOf(content) }"
                />
              </div>
            </div>
          </div>
        </template>

        <!-- ③ 确认发布 -->
        <template v-else-if="step === 2">
          <div class="confirm-grid">
            <div>
              <h3>上架信息</h3>
              <dl class="summary">
                <dt>商品</dt><dd>{{ chosenContents.length }} 个</dd>
                <dt>成色</dt><dd>{{ listing.condition }}</dd>
                <dt>运费</dt><dd>{{ listing.shippingFee ? yuan(listing.shippingFee) : '包邮' }}</dd>
                <dt>所在地</dt><dd>{{ listing.location }}</dd>
                <dt>分类</dt><dd>其他闲置</dd>
                <dt>图片</dt><dd>{{ listing.includeGenerated ? '商品原图 + AI 宣传图' : '商品原图' }}</dd>
              </dl>
              <div class="confirm-list">
                <div v-for="content in chosenContents" :key="content.id" class="confirm-row">
                  <img
                    v-if="content.product.image_id"
                    class="wb-thumb"
                    :src="catalogMedia.productImage(content.product.id, content.product.image_id, true)"
                    alt=""
                    loading="lazy"
                  >
                  <span v-else class="wb-thumb wb-thumb-empty">无图</span>
                  <span class="pick-main">
                    <strong class="wb-ellipsis">{{ content.listing_title || content.title }}</strong>
                    <small class="wb-ellipsis wb-muted">{{ content.product.name }}</small>
                  </span>
                  <span class="wb-num">{{ yuan(priceOf(content)) }}</span>
                </div>
              </div>
            </div>
            <div>
              <h3>闲鱼账号</h3>
              <p class="wb-muted account-hint">提交后按顺序在发布浏览器里执行；没登录时会先等你扫码登录。</p>
              <ContentBrowserPanel :enabled="true" purpose="listing" class="account-panel" />
            </div>
          </div>
        </template>

        <!-- ④ 完成 -->
        <template v-else>
          <div class="done">
            <ElResult icon="success" :title="`已提交 ${submittedIds.length} 条上架`" sub-title="正在按顺序执行，状态会自动刷新。也可以随时在“上架记录”里查看。" />
            <div class="confirm-list">
              <div v-for="record in submitted" :key="record.id" class="done-row">
                <span class="pick-main">
                  <strong class="wb-ellipsis">{{ record.product_name }}</strong>
                  <small class="wb-ellipsis wb-muted">{{ record.title }}</small>
                </span>
                <span class="wb-pill" :class="recordStatusClass(record.status)">{{ recordStatusLabels[record.status] }}</span>
                <a
                  v-if="record.external_url"
                  :href="record.external_url"
                  target="_blank"
                  rel="noopener"
                >查看</a>
                <span v-else-if="record.error" class="wb-muted wb-ellipsis done-error" :title="record.error">{{ record.error }}</span>
                <span class="wb-muted wb-num">{{ shortTime(record.finished_at || record.created_at) }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>

      <footer class="wizard-bar">
        <template v-if="step < 3">
          <ElButton @click="saveDraft">保存草稿</ElButton>
          <span class="bar-hint wb-muted">{{ stepProblem }}</span>
          <ElButton v-if="step > 0" @click="step--">上一步</ElButton>
          <ElButton v-if="step < 2" type="primary" @click="next">下一步</ElButton>
          <ElButton
            v-else
            type="primary"
            :loading="submitting"
            @click="submit"
          >
            提交 {{ chosen.length }} 条上架
          </ElButton>
        </template>
        <template v-else>
          <span class="bar-hint" />
          <ElButton @click="router.push('/agent/content/listing/records')">查看上架记录</ElButton>
          <ElButton type="primary" @click="restart">继续上架</ElButton>
        </template>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.wizard {
  display: flex;
  flex-direction: column;
  min-height: 560px;
}
.wizard-steps {
  display: flex;
  margin: 0;
  padding: 16px 20px;
  list-style: none;
  border-bottom: 1px solid var(--xi-semantic-color-border-subtle);
}
.wizard-steps li {
  display: flex;
  flex: 1;
  align-items: center;
  min-width: 0;
}
.wizard-steps li:not(:last-child)::after {
  flex: 1;
  height: 1px;
  margin: 0 12px;
  content: '';
  background: var(--xi-semantic-color-border-subtle);
}
.wizard-steps li.done:not(:last-child)::after {
  background: var(--xi-semantic-color-action-primary-default);
}
.wizard-steps button {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  color: var(--xi-semantic-color-text-tertiary);
  font: inherit;
  font-size: 14px;
  white-space: nowrap;
  background: none;
  border: 0;
}
.wizard-steps button:not(:disabled) {
  cursor: pointer;
}
.wizard-steps .dot {
  display: grid;
  width: 24px;
  height: 24px;
  flex: none;
  place-items: center;
  font-size: 12px;
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 50%;
}
.wizard-steps li.active button {
  color: var(--xi-semantic-color-text-primary);
  font-weight: 600;
}
.wizard-steps li.active .dot {
  color: #fff;
  background: var(--xi-semantic-color-action-primary-default);
  border-color: var(--xi-semantic-color-action-primary-default);
}
.wizard-steps li.done button {
  color: var(--xi-semantic-color-text-secondary);
}
.wizard-steps li.done .dot {
  color: var(--xi-semantic-color-action-primary-default);
  border-color: var(--xi-semantic-color-action-primary-default);
}
.wizard-body {
  flex: 1;
  min-width: 0;
}
.body-head {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--xi-semantic-color-divider-default);
}
.pick-search {
  width: 220px;
  margin-left: auto;
}
.pick-list {
  min-height: 240px;
}
.pick-row,
.confirm-row,
.done-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--xi-semantic-color-divider-default);
}
.pick-row {
  cursor: pointer;
}
.pick-row:hover {
  background: var(--wb-hover);
}
.pick-row.chosen {
  background: var(--wb-selected);
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
h3 {
  margin: 0 0 12px;
  font-size: 14px;
  font-weight: 600;
}
h3 small {
  margin-left: 6px;
  font-size: 12px;
  font-weight: 400;
}
.edit-grid,
.confirm-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 32px;
  padding: 20px;
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
.price-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 150px;
  align-items: center;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px solid var(--xi-semantic-color-divider-default);
}
.price-name {
  display: grid;
  min-width: 0;
  font-size: 13px;
}
.price-name strong {
  font-weight: 500;
}
.price-name small {
  font-size: 12px;
}
.price-row .is-missing :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--xi-semantic-color-feedback-warning-default) inset;
}
.summary {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 8px 12px;
  margin: 0 0 16px;
  font-size: 13px;
}
.summary dt {
  color: var(--xi-semantic-color-text-tertiary);
}
.summary dd {
  margin: 0;
}
.confirm-list {
  max-height: 360px;
  overflow-y: auto;
  border: 1px solid var(--xi-semantic-color-divider-default);
  border-radius: 6px;
}
.confirm-row:last-child,
.done-row:last-child {
  border-bottom: 0;
}
.account-hint {
  margin: -4px 0 0;
  font-size: 12px;
}
.account-panel :deep(.browser-heading) {
  display: none;
}
.done {
  max-width: 760px;
  margin: 0 auto;
  padding: 0 20px 20px;
}
.done-row a {
  color: var(--xi-semantic-color-link-default);
  font-size: 13px;
}
.done-error {
  max-width: 260px;
  font-size: 12px;
}
.wizard-bar {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: #fff;
  border-top: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 0 0 6px 6px;
}
.wizard-bar .el-button {
  margin: 0;
}
.bar-hint {
  flex: 1;
  font-size: 12px;
  text-align: right;
}
@media (width < 900px) {
  .edit-grid,
  .confirm-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .wizard-steps button span:not(.dot) {
    display: none;
  }
  .wizard-steps li.active button span {
    display: inline;
  }
}
@media (width < 600px) {
  .body-head {
    flex-wrap: wrap;
  }
  .pick-search {
    width: 100%;
  }
}
</style>
