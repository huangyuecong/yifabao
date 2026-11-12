<script setup lang="ts">
import type { ApprovedContent, AutomationConfig, AutomationRun } from '@/api/agent/automation'
import type { LibraryItem } from '@/api/agent/content'
import { ElMessage } from 'element-plus'
import { computed, onActivated, onBeforeUnmount, onDeactivated, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { automationApi } from '@/api/agent/automation'
import { contentApi } from '@/api/agent/content'
import { isProductResource, relatedMedia } from './contentResourceCatalog'
import ContentResourcePicker from './ContentResourcePicker.vue'
import ContentResourcePreview from './ContentResourcePreview.vue'
import { useLocalAccess } from './localAccess'

defineOptions({ name: 'ContentProductionView' })
const router = useRouter()
const { refresh: refreshAccess, hasPermission } = useLocalAccess()
const canManage = computed(() => hasPermission('library.manage'))
const tab = ref('create')
const resources = ref<LibraryItem[]>([])
const runs = ref<AutomationRun[]>([])
const packages = ref<ApprovedContent[]>([])
const selected = ref<AutomationRun>()
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const search = ref('')
const feedback = ref('')
const scope = ref<'copy' | 'images' | 'video' | 'all' | 'originals'>('copy')
const images = ref<string[]>([])
const video = ref('')
const previewLoading = ref(false)
const fileInput = ref<HTMLInputElement>()
const form = reactive({ source: 'library', productIds: [] as string[], name: '', facts: '', imageIds: [] as string[], instructions: '', imageMode: 'existing' as 'existing' | 'generate', imageCount: 3 })
const productImages = reactive<Record<string, string[]>>({})
const uploadTarget = ref('')
const draft = reactive({ title: '', description: '', listing_title: '', listing_description: '', category: '' })
const fields = ['title', 'description', 'listing_title', 'listing_description', 'category'] as const
const products = computed(() => resources.value.filter(isProductResource))
const selectedProducts = computed(() => products.value.filter(item => form.productIds.includes(item.id)))
const selectedImages = computed(() => resources.value.filter(item => form.imageIds.includes(item.id)))
const dirty = computed(() => selected.value && fields.some(key => draft[key] !== (selected.value?.draft[key] || '')))
const editable = computed(() => selected.value?.status === 'ready' && canManage.value && !busy.value)
const readyToApprove = computed(() => editable.value && !dirty.value && !selected.value?.draft.video_stale && !selected.value?.draft.generation_error && selected.value?.draft.has_video && selected.value?.draft.review?.state !== 'approved')
const filteredPackages = computed(() => packages.value.filter(item => `${item.product_name} ${item.title}`.includes(search.value.trim())))
const pendingCount = computed(() => runs.value.filter(item => item.status === 'ready' && !item.draft.generation_error && item.draft.review?.state !== 'approved').length)
let active = false
let epoch = 0
let timer: ReturnType<typeof setInterval> | undefined
let polling = false
let selection = 0
const pendingStart = ref<{ items: AutomationConfig[], key: string }>()
const labels: Record<string, string> = { queued: '排队中', preparing: 'AI 创作中', ready: '待人工审核', failed: '生成失败', cancelled: '已取消' }

function state(run: AutomationRun) {
  if (run.draft.generation_error) {
    return '部分内容待完成'
  }
  if (run.draft.approved_content_id && run.draft.review?.state === 'approved') {
    return '已审核入库'
  }
  return labels[run.status] || run.status
}
function failure(reason: unknown) {
  return reason instanceof Error ? reason.message : '操作失败，请重试'
}
function clearMedia() {
  images.value.forEach(url => URL.revokeObjectURL(url))
  if (video.value) {
    URL.revokeObjectURL(video.value)
  }
  images.value = []
  video.value = ''
}
function update(run: AutomationRun, reset = true) {
  runs.value = [run, ...runs.value.filter(item => item.id !== run.id)]
  selected.value = run
  if (reset) {
    fields.forEach((key) => {
      draft[key] = run.draft[key] || ''
    })
  }
}
async function preview(run: AutomationRun) {
  const ticket = ++selection
  clearMedia()
  previewLoading.value = true
  const result = await Promise.allSettled([
    ...Array.from({ length: run.draft.image_count || 0 }, (_, index) => automationApi.image(run.id, index)),
    ...(run.draft.has_video ? [automationApi.video(run.id)] : []),
  ])
  if (ticket !== selection || !active) {
    return
  }
  result.forEach((item, index) => {
    if (item.status === 'fulfilled') {
      const url = URL.createObjectURL(item.value.blob)
      if (index < (run.draft.image_count || 0)) {
        images.value.push(url)
      }
      else {
        video.value = url
      }
    }
    else {
      error.value = failure(item.reason)
    }
  })
  previewLoading.value = false
}
async function choose(run: AutomationRun) {
  if (busy.value) {
    return
  }
  tab.value = 'review'
  feedback.value = ''
  error.value = ''
  update(run)
  await preview(run)
}
function productsChanged() {
  for (const item of selectedProducts.value) {
    if (!productImages[item.id]) {
      productImages[item.id] = relatedMedia(item, resources.value).filter(value => value.resource_kind === 'image').slice(0, 9).map(value => value.id)
    }
  }
}
function selectAllProducts() {
  form.productIds = products.value.slice(0, 20).map(item => item.id)
  productsChanged()
}
function chooseUpload(productId = '') {
  uploadTarget.value = productId
  fileInput.value?.click()
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  const productId = uploadTarget.value
  const ids = productId ? productImages[productId] || [] : form.imageIds
  input.value = ''
  if (!files.length) {
    return
  }
  if (files.length + ids.length > 9 || files.some(file => !/\.(?:png|jpe?g|webp)$/iu.test(file.name) || !file.size || file.size > 20 * 1024 * 1024)) {
    error.value = '最多选择 9 张 JPG、PNG 或 WebP 图片，每张不超过 20 MB'
    return
  }
  await perform(async () => {
    for (const file of files) {
      const item = await contentApi.uploadLibrary({ resource_kind: 'image', category: 'image', name: file.name, data_level: 'internal', file })
      resources.value.unshift(item)
      if (productId) {
        productImages[productId] = [...(productImages[productId] || []), item.id]
      }
      else {
        form.imageIds.push(item.id)
      }
    }
  })
}
async function perform(work: () => Promise<void>) {
  if (busy.value || !canManage.value) {
    return
  }
  busy.value = true
  error.value = ''
  try {
    await work()
  }
  catch (reason) {
    error.value = failure(reason)
  }
  finally {
    busy.value = false
  }
}
async function start() {
  await perform(async () => {
    if (!pendingStart.value) {
      if ((form.source === 'library' && !selectedProducts.value.length) || (form.source === 'custom' && (!form.name.trim() || !form.facts.trim() || !form.imageIds.length))) {
        throw new Error('请选择产品并添加原图，或填写产品名称、真实信息并添加图片')
      }
      const missing = selectedProducts.value.filter(item => !productImages[item.id]?.length)
      if (form.source === 'library' && missing.length) {
        throw new Error(`请先为这些产品添加原图：${missing.map(item => item.name).join('、')}`)
      }
      const config: AutomationConfig = {
        name: `${form.name} · 产品创作`.slice(0, 120),
        purpose: 'creation',
        source_mode: form.source as 'library' | 'custom',
        copy_mode: 'ai',
        product_id: null,
        image_ids: [...form.imageIds],
        custom_product_name: form.name,
        custom_product_description: form.facts,
        platforms: [],
        accounts: {},
        condition: '',
        shipping_fee: 0,
        location: '',
        instructions: form.instructions,
        image_mode: form.imageMode,
        generated_image_count: form.imageCount,
        video_mode: 'slideshow',
        require_review: true,
        auto_publish: false,
      }
      const items = form.source === 'library'
        ? selectedProducts.value.map(item => ({
            ...config,
            name: `${item.name} · 产品创作`.slice(0, 120),
            product_id: item.id,
            image_ids: [...productImages[item.id]],
            custom_product_name: '',
            custom_product_description: '',
          }))
        : [config]
      pendingStart.value = { items, key: crypto.randomUUID() }
    }
    const result = await automationApi.createBatch(pendingStart.value.items, pendingStart.value.key)
    pendingStart.value = undefined
    runs.value = [...result.items, ...runs.value.filter(run => !result.items.some(item => item.id === run.id))]
    update(result.items[0])
    clearMedia()
    tab.value = 'review'
    ElMessage.success(`已创建 ${result.total} 个独立产品任务，完成后分别审核入库`)
  })
}
async function save() {
  await perform(async () => {
    if (!selected.value) {
      return
    }
    const run = await automationApi.saveDraft(selected.value.id, { ...draft, expected_revision: selected.value.draft.review?.revision || 1 })
    update(run)
    await preview(run)
  })
}
async function revise() {
  await perform(async () => {
    if (!selected.value || dirty.value) {
      throw new Error('请先保存手动修改')
    }
    if (!feedback.value.trim()) {
      throw new Error('请填写具体修改意见')
    }
    update(await automationApi.revise(selected.value.id, selected.value.draft.review?.revision || 1, feedback.value.trim(), scope.value))
    feedback.value = ''
  })
}
async function approve() {
  if (!readyToApprove.value) {
    return
  }
  await perform(async () => {
    if (!selected.value) {
      return
    }
    update(await automationApi.approve(selected.value.id, selected.value.draft.review?.revision || 1))
    packages.value = (await automationApi.approvedContents()).items
    ElMessage.success('审核通过，文案、图片和视频已关联产品入库')
  })
}
async function retry() {
  await perform(async () => {
    if (selected.value) {
      update(await automationApi.action(selected.value.id, 'retry'))
    }
  })
}
function usePackage(item: ApprovedContent, purpose: 'listing' | 'promotion') {
  router.push({ path: `/agent/content/${purpose}`, query: { approvedContent: item.id } })
}
async function load() {
  const current = epoch
  loading.value = true
  try {
    await refreshAccess()
    const result = await Promise.all([contentApi.listLibrary(), automationApi.runs(undefined, 'creation'), automationApi.approvedContents()])
    if (current !== epoch) {
      return
    }
    resources.value = result[0]
    runs.value = result[1].items
    packages.value = result[2].items
    if (selected.value) {
      const latest = runs.value.find(item => item.id === selected.value?.id)
      if (latest && !dirty.value && !busy.value) {
        update(latest)
        await preview(latest)
      }
    }
  }
  catch (reason) {
    if (current === epoch) {
      error.value = failure(reason)
    }
  }
  finally {
    if (current === epoch) {
      loading.value = false
    }
  }
}
async function poll() {
  if (polling || busy.value || !active || !runs.value.some(item => ['queued', 'preparing'].includes(item.status))) {
    return
  }
  polling = true
  const current = epoch
  try {
    const result = await automationApi.runs(undefined, 'creation')
    if (current !== epoch) {
      return
    }
    runs.value = result.items
    const latest = result.items.find(item => item.id === selected.value?.id)
    if (latest && !busy.value && !dirty.value) {
      const changed = latest.status !== selected.value?.status || latest.draft.review?.revision !== selected.value?.draft.review?.revision
      update(latest)
      if (changed) {
        await preview(latest)
      }
    }
  }
  catch (reason) {
    if (current === epoch) {
      error.value = failure(reason)
    }
  }
  finally {
    polling = false
  }
}
function stop() {
  active = false
  epoch++
  selection++
  if (timer) {
    clearInterval(timer)
  }
  clearMedia()
}
onActivated(() => {
  active = true
  void load()
  timer = setInterval(poll, 3000)
})
onDeactivated(stop)
onBeforeUnmount(stop)
</script>

<template>
  <div class="production-view" :aria-busy="loading || busy">
    <header class="production-heading">
      <div><span class="eyebrow">从产品到可用内容</span><h1>产品创作与审核</h1><p>提供产品资料，让 AI 制作文案、图片和视频。审核通过后自动入库，随时用于宣传或售卖。</p></div>
      <ElButton :loading="loading" :disabled="busy" @click="load">刷新</ElButton>
    </header>
    <ol class="production-flow"><li><b>01</b> 产品与原图</li><li><b>02</b> AI 创作</li><li><b>03</b> 人工审核 / AI 修改</li><li><b>04</b> 审核入库</li><li><b>05</b> 选择平台发布</li></ol>
    <ElAlert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
    />
    <ElTabs v-model="tab" class="production-tabs">
      <ElTabPane label="新建创作" name="create">
        <div class="creation-layout">
          <ElForm
            class="production-card"
            label-position="top"
            :disabled="busy || !canManage || !!pendingStart"
            @submit.prevent
          >
            <h2>1. 产品与参考图片</h2>
            <ElFormItem label="产品来源"><ElRadioGroup v-model="form.source"><ElRadioButton value="library">数据库产品</ElRadioButton><ElRadioButton value="custom">填写新产品</ElRadioButton></ElRadioGroup></ElFormItem>
            <template v-if="form.source === 'library'">
              <ElFormItem label="批量选择产品" required>
                <ElSelect
                  v-model="form.productIds"
                  multiple
                  filterable
                  :multiple-limit="20"
                  collapse-tags
                  collapse-tags-tooltip
                  :max-collapse-tags="3"
                  placeholder="可多选产品，分别生成独立内容"
                  @change="productsChanged"
                >
                  <ElOption
                    v-for="item in products"
                    :key="item.id"
                    :label="item.name"
                    :value="item.id"
                  />
                </ElSelect>
              </ElFormItem>
              <div class="batch-toolbar">
                <span>已选 {{ form.productIds.length }} 个产品 · 每批最多 20 个</span><ElButton
                  link
                  type="primary"
                  :disabled="busy || !!pendingStart || !canManage"
                  @click="selectAllProducts"
                >
                  {{ products.length > 20 ? '选择前 20 个' : '全选产品' }}
                </ElButton><ElButton link :disabled="busy || !!pendingStart || !canManage" @click="form.productIds = []">清空</ElButton>
              </div>
              <ElCollapse class="batch-products">
                <ElCollapseItem v-for="item in selectedProducts" :key="item.id" :name="item.id">
                  <template #title><span class="batch-product-name">{{ item.name }}</span><ElTag size="small" :type="productImages[item.id]?.length ? 'info' : 'warning'">{{ productImages[item.id]?.length || 0 }} 张原图</ElTag></template>
                  <ContentResourcePicker
                    v-model="productImages[item.id]"
                    :resources="resources"
                    :kinds="['image']"
                    :max-selections="9"
                    :disabled="busy || !canManage || !!pendingStart"
                    :title="`为 ${item.name} 选择原图`"
                    button-text="调整此产品图片"
                  />
                  <ElButton :disabled="busy || !canManage || !!pendingStart" @click="chooseUpload(item.id)">为此产品上传图片</ElButton>
                </ElCollapseItem>
              </ElCollapse>
              <p class="batch-note">自动匹配各产品的原图。可展开逐一调整，每个产品独立生成、修改和审核。</p>
            </template>
            <template v-else>
              <ElFormItem label="产品名称" required><ElInput v-model="form.name" maxlength="120" placeholder="例如：实木桌面收纳盒" /></ElFormItem>
              <ElFormItem label="真实产品信息" required>
                <ElInput
                  v-model="form.facts"
                  type="textarea"
                  :rows="5"
                  maxlength="4000"
                  show-word-limit
                  placeholder="填写规格、材质、特点和使用场景，AI 将依据这些信息创作"
                />
              </ElFormItem>
            </template>
            <ElFormItem v-if="form.source === 'custom'" label="商品原图" required>
              <div class="source-picker">
                <ContentResourcePicker
                  v-model="form.imageIds"
                  :resources="resources"
                  :kinds="['image']"
                  :max-selections="9"
                  :disabled="busy || !canManage"
                  title="选择产品参考图片"
                  button-text="从数据库选图"
                /><ElButton :disabled="busy || !canManage" @click="chooseUpload()">上传图片</ElButton>
              </div>
              <div class="source-images"><ContentResourcePreview v-for="item in selectedImages" :key="item.id" :item="item" /></div>
            </ElFormItem>
            <input
              ref="fileInput"
              type="file"
              accept="image/png,image/jpeg,image/webp"
              multiple
              hidden
              @change="upload"
            >
          </ElForm>
          <section class="production-card">
            <h2>2. {{ form.source === 'library' ? '批量创作要求' : '创作要求' }}</h2>
            <ElForm label-position="top" :disabled="busy || !canManage || !!pendingStart">
              <ElFormItem label="宣传图片"><ElRadioGroup v-model="form.imageMode"><ElRadio value="existing">使用商品原图</ElRadio><ElRadio value="generate">AI 生成场景图</ElRadio></ElRadioGroup></ElFormItem>
              <ElFormItem v-if="form.imageMode === 'generate'" label="场景图数量"><ElInputNumber v-model="form.imageCount" :min="1" :max="9" /></ElFormItem>
              <ElFormItem label="受众、风格和创作要求">
                <ElInput
                  v-model="form.instructions"
                  type="textarea"
                  :rows="6"
                  maxlength="4000"
                  show-word-limit
                  placeholder="例如：面向租房人群，突出节省空间。文案简洁自然，图片背景干净。"
                />
              </ElFormItem>
            </ElForm>
            <div class="deliverables"><strong>本次生成</strong><p>宣传标题与文案 · 商品上架描述 · 宣传图片 · 竖屏字幕视频</p><small>视频由图片与字幕合成。AI 场景图用于宣传，上架保留商品原图。</small></div>
            <ElButton
              type="primary"
              size="large"
              :loading="busy"
              :disabled="!canManage"
              @click="start"
            >
              {{ pendingStart ? '继续创建任务' : form.source === 'library' ? `为 ${form.productIds.length} 个产品开始创作` : '开始 AI 创作' }}
            </ElButton>
          </section>
        </div>
      </ElTabPane>
      <ElTabPane :label="`创作与审核${pendingCount ? ` · ${pendingCount} 待审` : ''}`" name="review">
        <div v-if="runs.length" class="review-layout">
          <aside class="run-list">
            <button
              v-for="run in runs"
              :key="run.id"
              type="button"
              :disabled="busy"
              class="creation-run"
              :class="{ selected: selected?.id === run.id }"
              @click="choose(run)"
            >
              <strong>{{ run.config.name }}</strong><span>{{ state(run) }} · 第 {{ run.draft.review?.revision || 1 }} 版</span>
            </button>
          </aside>
          <section v-if="selected" class="production-card review-card">
            <div class="review-heading"><h2>{{ state(selected) }}</h2><ElTag :type="selected.draft.review?.state === 'approved' ? 'success' : 'info'">第 {{ selected.draft.review?.revision || 1 }} 版</ElTag></div>
            <ElAlert
              v-if="selected.error"
              :title="selected.error"
              type="error"
              :closable="false"
            />
            <ElButton v-if="selected.status === 'failed'" :loading="busy" @click="retry">重试生成</ElButton>
            <ElAlert
              v-if="selected.draft.generation_error"
              :title="selected.draft.generation_error.message"
              description="已完成的文案会保留。可以在下方单独重试失败步骤，或选择使用商品原图继续制作视频。"
              type="warning"
              :closable="false"
              show-icon
            />
            <p v-if="['queued', 'preparing'].includes(selected.status)" role="status">AI 正在处理，完成后会自动显示结果。你可以离开本页面，任务会继续执行。</p>
            <template v-if="selected.draft.title">
              <div class="review-media" :aria-busy="previewLoading">
                <div class="generated-images">
                  <img
                    v-for="(url, index) in images"
                    :key="url"
                    :src="url"
                    :alt="`宣传图片 ${index + 1}`"
                  ><p v-if="previewLoading">正在加载预览…</p>
                </div><video
                  v-if="video"
                  :src="video"
                  controls
                  preload="metadata"
                  aria-label="生成视频预览"
                />
              </div>
              <ElForm label-position="top" :disabled="!editable" class="copy-columns">
                <div>
                  <h3>宣传文案</h3><ElFormItem label="宣传标题"><ElInput v-model="draft.title" maxlength="20" show-word-limit /></ElFormItem><ElFormItem label="宣传正文">
                    <ElInput
                      v-model="draft.description"
                      type="textarea"
                      :rows="6"
                      maxlength="900"
                      show-word-limit
                    />
                  </ElFormItem>
                </div>
                <div>
                  <h3>商品上架文案</h3><ElFormItem label="商品标题"><ElInput v-model="draft.listing_title" maxlength="30" show-word-limit /></ElFormItem><ElFormItem label="商品描述">
                    <ElInput
                      v-model="draft.listing_description"
                      type="textarea"
                      :rows="6"
                      maxlength="1000"
                      show-word-limit
                    />
                  </ElFormItem>
                </div>
              </ElForm>
              <ElButton
                v-if="dirty"
                :disabled="!editable"
                :loading="busy"
                @click="save"
              >
                保存手动修改
              </ElButton>
              <ElAlert
                v-if="selected.draft.video_stale"
                title="宣传文案已修改，请选择“更新视频字幕”重新合成，再审核入库。"
                type="warning"
                :closable="false"
              />
              <section class="revision-box">
                <h3>需要调整？告诉 AI</h3><ElInput
                  v-model="feedback"
                  type="textarea"
                  :rows="3"
                  maxlength="2000"
                  :disabled="!editable"
                  placeholder="例如：文案压缩到 80 字，不要营销语气，突出收纳用途。"
                  aria-label="审核修改意见"
                /><div class="revision-actions"><ElSelect v-model="scope" :disabled="!editable" aria-label="修改范围"><ElOption label="修改文案并更新字幕" value="copy" /><ElOption label="重新生成宣传图片" value="images" /><ElOption label="使用商品原图并合成视频" value="originals" /><ElOption label="更新视频字幕" value="video" /><ElOption label="全部修改" value="all" /></ElSelect><ElButton :disabled="!editable || dirty || !feedback.trim()" :loading="busy" @click="revise">交给 AI 修改</ElButton></div>
              </section>
              <div class="approval-bar">
                <span>{{ dirty ? '请先保存修改' : selected.draft.review?.state === 'approved' ? '当前版本已入库，后续修改将产生待审的新版本。' : '确认文案、图片与视频符合要求后入库。' }}</span><ElButton
                  type="primary"
                  :disabled="!readyToApprove"
                  :loading="busy"
                  @click="approve"
                >
                  审核通过并入库
                </ElButton><ElButton v-if="selected.draft.approved_content_id" @click="tab = 'library'">查看内容库</ElButton>
              </div>
              <ElCollapse v-if="selected.draft.history?.length"><ElCollapseItem title="修改记录" name="history"><div v-for="item in selected.draft.history" :key="item.review.revision" class="history-item"><b>第 {{ item.review.revision }} 版 · {{ item.title }}</b><p>{{ item.review.feedback }}</p><p>{{ item.description }}</p></div></ElCollapseItem></ElCollapse>
            </template>
          </section><ElEmpty v-else description="选择左侧任务预览并审核" />
        </div><ElEmpty v-else description="还没有创作任务，先选择产品开始创作" />
      </ElTabPane>
      <ElTabPane :label="`已审核内容库 · ${packages.length}`" name="library">
        <div class="library-heading">
          <div><h2>可以直接复用的产品内容</h2><p>每份记录保留审核时的文案和素材，发布时直接读取这个版本。</p></div><ElInput
            v-model="search"
            clearable
            placeholder="搜索产品或标题"
            aria-label="搜索已审核内容"
          />
        </div>
        <div v-if="filteredPackages.length" class="package-grid"><article v-for="item in filteredPackages" :key="item.id" class="production-card package-card"><ElTag type="success" size="small">已审核 · 第 {{ item.revision }} 版</ElTag><h3>{{ item.product_name }}</h3><strong>{{ item.title }}</strong><p>{{ item.description }}</p><small>{{ item.image_ids.length }} 张宣传图 · {{ item.original_image_ids.length }} 张商品原图 · {{ item.video_id ? '含视频' : '无视频' }}</small><div class="package-actions"><ElButton type="primary" plain @click="usePackage(item, 'promotion')">用于内容宣传</ElButton><ElButton @click="usePackage(item, 'listing')">用于商品上架</ElButton></div></article></div><ElEmpty v-else description="人工审核通过后，内容会自动出现在这里" />
      </ElTabPane>
    </ElTabs>
  </div>
</template>

<style scoped>
.production-view {
  color: #0f172a;
}
.batch-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: #64748b;
  margin-bottom: 16px;
}
.batch-product-name {
  flex: 1;
  text-align: left;
  font-size: 13px;
  padding-right: 12px;
}
.batch-note {
  color: #64748b;
  font-size: 12px;
  line-height: 1.7;
}
.batch-products :deep(.el-collapse-item__content) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 12px;
}
.production-heading,
.review-heading,
.library-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  margin-bottom: 24px;
}
.eyebrow {
  font-size: 12px;
  color: #64748b;
}
h1 {
  font-size: 25px;
  margin: 8px 0;
}
h2 {
  font-size: 17px;
  margin: 0 0 20px;
}
h3 {
  font-size: 15px;
}
.production-heading p,
.library-heading p {
  color: #64748b;
  font-size: 13px;
  margin: 6px 0;
  line-height: 1.7;
}
.production-flow {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 28px;
  padding: 18px 22px;
  list-style: none;
  background: #f1f5f9;
  border-radius: 12px;
  margin: 0 0 24px;
  font-size: 13px;
  color: #475569;
}
.production-flow b {
  color: #94a3b8;
  margin-right: 8px;
}
.production-card {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 14px;
  padding: 24px;
  min-width: 0;
}
.creation-layout {
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  gap: 24px;
}
.source-picker,
.revision-actions,
.package-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
.source-images {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 16px;
  width: 100%;
}
.deliverables {
  background: #f8fafc;
  border-radius: 10px;
  padding: 18px;
  margin-bottom: 24px;
  font-size: 13px;
  line-height: 1.8;
}
small {
  color: #64748b;
  line-height: 1.7;
}
.review-layout {
  display: grid;
  grid-template-columns: 250px minmax(0, 1fr);
  gap: 24px;
}
.run-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.creation-run {
  text-align: left;
  border: 1px solid #e2e8f0;
  background: white;
  border-radius: 10px;
  padding: 16px;
  cursor: pointer;
  color: #334155;
}
.creation-run.selected {
  background: #eef2ff;
  border-color: #a5b4fc;
}
.creation-run strong,
.creation-run span {
  display: block;
  overflow-wrap: anywhere;
}
.creation-run span {
  font-size: 12px;
  color: #64748b;
  margin-top: 8px;
}
.review-heading h2 {
  margin: 0;
}
.review-media {
  display: grid;
  grid-template-columns: 1fr 210px;
  gap: 16px;
  margin-bottom: 24px;
}
.generated-images {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  align-content: start;
  gap: 8px;
}
.generated-images img {
  width: 100%;
  aspect-ratio: 1;
  object-fit: contain;
  background: #f8fafc;
  border-radius: 8px;
}
.review-media video {
  width: 100%;
  max-height: 330px;
  border-radius: 10px;
  background: #0f172a;
}
.copy-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
}
.revision-box {
  padding: 20px;
  background: #f8fafc;
  border-radius: 10px;
  margin: 20px 0;
}
.revision-box h3 {
  margin-top: 0;
}
.revision-actions {
  margin-top: 12px;
}
.revision-actions :deep(.el-select) {
  width: 220px;
}
.approval-bar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 12px;
  padding: 20px 0;
  border-top: 1px solid #e2e8f0;
}
.approval-bar span {
  color: #64748b;
  font-size: 12px;
  margin-right: auto;
}
.history-item {
  border-bottom: 1px solid #e2e8f0;
  padding: 12px 0;
}
.history-item p {
  white-space: pre-wrap;
}
.library-heading :deep(.el-input) {
  width: 260px;
}
.library-heading h2 {
  margin: 0;
}
.package-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 20px;
}
.package-card p {
  color: #64748b;
  font-size: 13px;
  line-height: 1.7;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.package-actions {
  margin-top: 20px;
}
.package-actions :deep(.el-button) {
  margin-left: 0;
}
@media (max-width: 1100px) {
  .package-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .review-layout {
    grid-template-columns: 200px minmax(0, 1fr);
  }
  .review-media {
    grid-template-columns: 1fr 170px;
  }
}
@media (max-width: 760px) {
  .creation-layout,
  .review-layout,
  .copy-columns,
  .package-grid {
    grid-template-columns: 1fr;
  }
  .production-card {
    padding: 18px;
  }
  .run-list {
    max-height: 220px;
    overflow: auto;
  }
  .production-heading,
  .library-heading {
    align-items: flex-start;
    flex-direction: column;
  }
  .review-media {
    grid-template-columns: 1fr;
  }
  .review-media video {
    max-width: 220px;
  }
  .production-flow {
    gap: 12px;
  }
  .source-images {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
