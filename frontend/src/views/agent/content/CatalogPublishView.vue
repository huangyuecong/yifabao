<script setup lang="ts">
import type { ProductContent, PromotionPlatform } from '@/api/agent/catalog'
import { Search, View } from '@element-plus/icons-vue'
import { ElMessage, ElNotification } from 'element-plus'
import { computed, h, onActivated, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { automationApi } from '@/api/agent/automation'
import { catalogApi, catalogMedia, recordStatusLabels } from '@/api/agent/catalog'
import { platformInitial, platformName } from './catalogPresentation'
import ContentBrowserPanel from './ContentBrowserPanel.vue'
import { friendlyContentError } from './contentWorkflow'
import { confirmLogins } from './loginCheck'
import ModuleTabs from './ModuleTabs.vue'
import { copyProblems, mergeLimits } from './promotionPreview'
import PromotionPreview from './PromotionPreview.vue'
import WbPagination from './WbPagination.vue'

/* 内容宣传 → 平台发布: pick reviewed contents that have a video, choose platforms, submit.
 * (闲鱼上架 has its own step-by-step page, CatalogListingWizard.) */
defineOptions({ name: 'CatalogPublishView' })
const route = useRoute()
const router = useRouter()
const contents = ref<ProductContent[]>([])
const query = ref('')
const page = ref(1)
const pageSize = ref(10)
const loading = ref(false)
const chosen = ref<string[]>([])
const submitting = ref(false)
const accountPanel = ref<HTMLElement>()
const platforms = ref<PromotionPlatform[]>(['douyin', 'xiaohongshu', 'kuaishou'])
const promotionPlatforms: PromotionPlatform[] = ['douyin', 'xiaohongshu', 'kuaishou']
const limits = ref(mergeLimits(undefined))
// The content shown in the preview. Only the eye button sets it: ticking several
// contents must not start loading their videos one after another.
const focusId = ref('')

const usable = (content: ProductContent) => content.has_video
const visible = computed(() => {
  const keyword = query.value.trim()
  return keyword ? contents.value.filter(item => item.product.name.includes(keyword) || item.title.includes(keyword)) : contents.value
})
const pageItems = computed(() => visible.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const pageUsable = computed(() => pageItems.value.filter(usable))
const pageAllChosen = computed(() => pageUsable.value.length > 0 && pageUsable.value.every(item => chosen.value.includes(item.id)))
const pageSomeChosen = computed(() => !pageAllChosen.value && pageUsable.value.some(item => chosen.value.includes(item.id)))
const chosenContents = computed(() => contents.value.filter(item => chosen.value.includes(item.id)))
// The server rejects the whole submission when any copy is too long; Kuaishou's shared
// title + body limit would only fail later at the publisher, so it is caught here too.
const copyIssues = computed(() => chosenContents.value.flatMap(content => platforms.value.flatMap(platform =>
  copyProblems(platform, platformName(platform), content, limits.value)
    .map(problem => `${content.product.name}：${problem}`))))
const recordCount = computed(() => chosen.value.length * platforms.value.length)
const canSubmit = computed(() => chosen.value.length > 0 && platforms.value.length > 0 && !copyIssues.value.length)
const previewContent = computed(() => contents.value.find(item => item.id === focusId.value) ?? null)
const previewList = computed(() => {
  const focused = previewContent.value
  return focused && !chosen.value.includes(focused.id) ? [...chosenContents.value, focused] : chosenContents.value
})
const previewPosition = computed(() => previewContent.value
  ? previewList.value.findIndex(item => item.id === previewContent.value!.id) + 1
  : 0)

watch(query, () => {
  page.value = 1
})

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
  chosen.value = chosen.value.includes(content.id)
    ? chosen.value.filter(id => id !== content.id)
    : [...chosen.value, content.id]
}

function togglePage(value: boolean | string | number) {
  const ids = pageUsable.value.map(item => item.id)
  chosen.value = value
    ? [...new Set([...chosen.value, ...ids])]
    : chosen.value.filter(id => !ids.includes(id))
}

function preview(content: ProductContent) {
  focusId.value = focusId.value === content.id ? '' : content.id
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
    // A missing login is a prompt now, not a failed record later.
    if (!await confirmLogins('promotion', platforms.value, () => accountPanel.value?.scrollIntoView({ behavior: 'smooth', block: 'center' }))) {
      return
    }
    const result = await catalogApi.publishPromotion({ content_ids: chosen.value, platforms: platforms.value })
    ElNotification({
      type: 'success',
      title: `已提交 ${result.total} 条发布`,
      message: h('span', ['正在按顺序发布，结果见', h('a', {
        href: '#',
        style: 'margin-left:4px',
        onClick: (event: Event) => {
          event.preventDefault()
          router.push('/agent/content/promotion/records')
        },
      }, '发布历史')]),
    })
    chosen.value = []
    focusId.value = ''
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '提交失败'))
  }
  finally {
    submitting.value = false
  }
}

onActivated(() => {
  void load()
  void loadLimits()
})
</script>

<template>
  <div class="wb-page">
    <ModuleTabs workflow="promotion" />

    <div class="publish-layout with-preview">
      <section class="wb-panel pick-panel">
        <div class="wb-panel-head">
          <h2>1. 选择内容 <small class="wb-muted">已选 {{ chosen.length }}</small></h2>
          <div class="pick-tools">
            <ElCheckbox
              :model-value="pageAllChosen"
              :indeterminate="pageSomeChosen"
              :disabled="!pageUsable.length"
              @change="togglePage"
            >
              全选本页
            </ElCheckbox>
            <ElInput
              v-model="query"
              :prefix-icon="Search"
              size="small"
              placeholder="筛选"
              clearable
              class="pick-search"
            />
          </div>
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
            :class="{ chosen: chosen.includes(content.id), disabled: !usable(content), previewing: previewContent?.id === content.id }"
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
              <small class="wb-ellipsis wb-muted">{{ content.title }}</small>
            </span>
            <span class="pick-side">
              <ElButton
                v-if="usable(content)"
                link
                :icon="View"
                :type="previewContent?.id === content.id ? 'primary' : undefined"
                :title="previewContent?.id === content.id ? '关闭预览' : '预览发布效果'"
                :aria-label="previewContent?.id === content.id ? '关闭预览' : '预览发布效果'"
                :aria-pressed="previewContent?.id === content.id"
                @click.stop="preview(content)"
                @keydown.space.stop
              />
              <span v-else class="wb-muted">无视频</span>
              <span
                v-for="item in content.publishes.filter(value => value.workflow === 'promotion')"
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
          v-if="visible.length > 10"
          v-model:page="page"
          v-model:page-size="pageSize"
          :total="visible.length"
          compact
        />
      </section>

      <div class="side-column">
        <section class="wb-panel settings-panel">
          <div class="wb-panel-head"><h2>2. 发布平台</h2></div>
          <div class="wb-panel-body">
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
              {{ chosen.length ? `提交 ${recordCount} 条发布` : '请先选择内容' }}
            </ElButton>
          </div>
        </section>

        <PromotionPreview
          :content="previewContent"
          :position="previewPosition"
          :total="previewList.length"
          :chosen="!!previewContent && chosen.includes(previewContent.id)"
          :platforms="platforms"
          :limits="limits"
          @step="stepPreview"
        />

        <section ref="accountPanel" class="wb-panel account-panel">
          <div class="wb-panel-head"><h2>平台账号</h2></div>
          <div class="wb-panel-body">
            <ContentBrowserPanel :enabled="true" purpose="promotion" />
          </div>
        </section>
      </div>
    </div>
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
.pick-tools {
  display: flex;
  align-items: center;
  gap: 12px;
}
.pick-search {
  width: 180px;
}
.pick-row.previewing {
  box-shadow: inset 3px 0 0 var(--xi-semantic-color-action-primary-default);
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
