<script setup lang="ts">
import type {
  ApprovedContent,
  AutomationConfig,
  AutomationDraft,
  AutomationOperation,
  AutomationPlatform,
  AutomationPlatformDefinition,
  AutomationPurpose,
  AutomationRun,
  AutomationStatus,
  AutomationWorkflow,
  BrowserPlatformSession,
} from '@/api/agent/automation'
import type { LibraryItem } from '@/api/agent/content'
import { ElMessage } from 'element-plus'
import { computed, onActivated, onBeforeUnmount, onDeactivated, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { automationApi, automationPurpose } from '@/api/agent/automation'
import { compatiblePlatforms, executionMode, platformLimits, publishBlockReason, purposeExecutionMode, workflowForPurpose } from '@/api/agent/automationPolicy'
import { contentApi } from '@/api/agent/content'
import ContentAndroidPanel from './ContentAndroidPanel.vue'
import ContentBrowserPanel from './ContentBrowserPanel.vue'
import ContentProductPicker from './ContentProductPicker.vue'
import { relatedMedia } from './contentResourceCatalog'
import ContentResourcePicker from './ContentResourcePicker.vue'
import ContentResourcePreview from './ContentResourcePreview.vue'
import { compareContentDateTimeDescending, formatDate } from './contentWorkflow'
import { useLocalAccess } from './localAccess'

defineOptions({ name: 'ContentAutomationView' })
const props = defineProps<{ purpose?: AutomationPurpose }>()
const route = useRoute()
const router = useRouter()
const approvedContents = ref<ApprovedContent[]>([])
const selectedApproved = computed(() => approvedContents.value.find(item => item.id === form.approved_content_id))
const approvedContentId = computed({
  get: () => form.approved_content_id || '',
  set: (value: string) => {
    form.approved_content_id = value || null
  },
})
let appliedApprovedQuery = ''
const internalPurpose = ref<AutomationPurpose>('listing')
const currentPurpose = computed(() => props.purpose || internalPurpose.value)
const isListing = computed(() => currentPurpose.value === 'listing')
const businessTitle = computed(() => (isListing.value ? '商品上架' : '内容宣传'))
// The rules pages sit behind 商品上架 / 内容宣传; name them like their routes so the two never look like one page.
const pageTitle = computed(() => (isListing.value ? '上下架规则' : '宣传规则'))

const { access, hasPermission, refresh: refreshAccess } = useLocalAccess()
const canManage = computed(() => hasPermission('library.manage'))
const operationLabels: Record<AutomationOperation, string> = {
  publish: '新商品上架 / 视频发布',
  relist: '闲鱼重新上架',
  delist: '闲鱼下架',
}
const roleLabels: Record<string, string> = {
  main: '商品主图',
  detail: '商品细节',
  scene: '场景图',
  packaging: '包装图',
  logo: '品牌标识',
  other: '其他图片',
}
const stateLabels: Record<string, string> = {
  queued: '排队中',
  preparing: '准备内容',
  ready: '待发布',
  publishing: '发布中',
  succeeded: '发布成功',
  partial: '部分发布成功',
  failed: '执行失败',
  unknown: '发布结果待核实',
  cancelled: '已取消',
  pending: '待执行',
  submitting: '正在提交',
  published: '已发布',
  listed: '已上架',
  delisted: '已下架',
}
function initialConfig(purpose: AutomationPurpose = currentPurpose.value): AutomationConfig {
  return {
    name: '',
    purpose,
    execution_mode: purposeExecutionMode(purpose),
    device_id: null,
    operation: 'publish',
    target_url: '',
    target_title: '',
    source_mode: purpose === 'listing' ? 'custom' : 'library',
    copy_mode: purpose === 'listing' ? 'manual' : 'ai',
    custom_product_name: '',
    custom_product_description: '',
    product_id: '',
    approved_content_id: null,
    image_ids: [],
    video_id: '',
    platforms: [purpose === 'listing' ? 'xianyu' : 'douyin'],
    accounts: {},
    price: undefined,
    condition: purpose === 'listing' ? '全新' : '',
    shipping_fee: 0,
    location: '',
    instructions: '',
    category: '',
    auto_publish: false,
    require_review: true,
    image_mode: 'existing',
    generated_image_count: 3,
    video_mode: 'slideshow',
  }
}
const form = reactive<AutomationConfig>(initialConfig())
const workflows = ref<AutomationWorkflow[]>([])
const resources = ref<LibraryItem[]>([])
const runs = ref<AutomationRun[]>([])
const businessWorkflows = computed(() =>
  workflows.value.filter(item => automationPurpose(item.config) === currentPurpose.value),
)
const businessRuns = computed(() =>
  runs.value.filter(item => automationPurpose(item.config) === currentPurpose.value),
)
const legacyWorkflows = ref<AutomationWorkflow[]>([])
const legacyRuns = ref<AutomationRun[]>([])
const status = ref<AutomationStatus>()
const platformCatalog = ref<AutomationPlatformDefinition[]>([])
const browserSessions = ref<Partial<Record<AutomationPlatform, BrowserPlatformSession>>>({})
const inspectingAndroid = ref(false)
const executionNotice = ref('')
const workflowId = ref('')
const selectedRun = ref<AutomationRun>()
const draft = reactive<Pick<AutomationDraft, 'title' | 'description' | 'category' | 'price'>>({})
const loading = ref(false)
const saving = ref(false)
const uploadingImages = ref(false)
const imageInput = ref<HTMLInputElement>()
const actionLoading = ref(false)
const error = ref('')
const statusError = ref('')
const formError = ref('')
const activeWorkspace = ref<'configuration' | 'runs'>('configuration')
const activeStep = ref(0)
const runError = ref('')
const previewUrl = ref('')
const previewLoading = ref(false)
const imagePreviews = ref<string[]>([])
const reviewFeedback = ref('')
const revisionScope = ref<'copy' | 'images' | 'video' | 'all'>('copy')
watch(() => form.auto_publish, (value) => {
  form.require_review = !value
})
const pendingRequests = reactive<
  Partial<Record<AutomationPurpose, { workflowId: string, key: string }>>
>({})
const pendingRequest = computed({
  get: () => pendingRequests[currentPurpose.value],
  set: (value) => {
    pendingRequests[currentPurpose.value] = value
  },
})
// This Element Plus version sets the numeric input's aria-disabled only on mount.
// Remount monetary fields when form permissions/busy state change to keep it accurate.
const formDisabled = computed(() => saving.value || uploadingImages.value || !canManage.value || !!pendingRequest.value)
let pollTimer: ReturnType<typeof setInterval> | undefined
let polling = false
let active = true
let generation = 0
let runSelectionVersion = 0

const browserStatus = computed(() => status.value?.browser || status.value)
const isAndroid = computed(() => executionMode(form) === 'android')
const executorStatus = computed(() => isAndroid.value ? status.value?.android : browserStatus.value)
const isManagement = computed(() => isListing.value && (form.operation || 'publish') !== 'publish')
const isCustomProduct = computed(() => form.source_mode === 'custom')
const runIsManagement = computed(
  () => (selectedRun.value?.config.operation || 'publish') !== 'publish',
)
const availablePlatforms = computed(() => compatiblePlatforms(platformCatalog.value, currentPurpose.value, executionMode(form), form.operation))
const selectedPlatforms = computed(() => availablePlatforms.value.filter(item => form.platforms.includes(item.id)))
const availableDevices = computed(() => status.value?.android?.devices || [])
const selectedDeviceId = computed({
  get: () => form.device_id || '',
  set: (value: string) => {
    form.device_id = value || null
  },
})
watch([availableDevices, isAndroid], ([devices, android]) => {
  if (android && !form.device_id && devices.length === 1) {
    form.device_id = devices[0].device_id
  }
}, { immediate: true })
watch(() => [form.device_id, status.value?.android], () => {
  if (!isAndroid.value) {
    return
  }
  const device = availableDevices.value.find(item => item.device_id === form.device_id)
  for (const platform of form.platforms) {
    const account = device?.apps?.[platform]?.account_id
    if (!form.accounts?.[platform] && account) {
      form.accounts = { ...form.accounts, [platform]: account }
    }
  }
})
const androidDraftSaved = computed(() => selectedRun.value && ['title', 'description', 'price', 'category'].every((key) => {
  const field = key as keyof typeof draft
  return (draft[field] ?? '') === (selectedRun.value?.draft[field] ?? '')
}))
function browserLoginBlock(config: AutomationConfig) {
  if (executionMode(config) !== 'browser') {
    return ''
  }
  for (const platform of config.platforms) {
    const session = browserSessions.value[platform]
    if (session?.state !== 'logged_in' || !session.account_id) {
      return session?.message || (session?.state === 'unknown' && !session.connected
        ? '正在核对网页登录状态，完成后即可发布'
        : '请先打开网页登录并检测账号')
    }
    if (config.accounts?.[platform] && config.accounts[platform] !== session.account_id) {
      return '当前网页登录账号与发布规则不同，请重新选择账号'
    }
    const operation = config.operation || 'publish'
    if (operation !== 'publish' && !config.accounts?.[platform]) {
      return '请选择需要操作的闲鱼网页账号'
    }
    if (!(session.operations_ready?.[operation] ?? (operation === 'publish' && session.publish_ready))) {
      return operation === 'publish' ? '该平台暂未完成自动发布适配，可打开网页发布页手动发布' : '此网页账号的商品上下架操作尚未完成验证'
    }
  }
  return ''
}
const formPublishBlock = computed(() => publishBlockReason(form, status.value, platformCatalog.value) || browserLoginBlock(form))
const runPublishBlock = computed(() => selectedRun.value ? publishBlockReason(selectedRun.value.config, status.value, platformCatalog.value) || browserLoginBlock(selectedRun.value.config) : '')
const runUsesCurrentMode = computed(() => !selectedRun.value || executionMode(selectedRun.value.config) === purposeExecutionMode(currentPurpose.value))
const startBlock = computed(() => (isManagement.value || form.auto_publish) ? formPublishBlock.value : '')
const retryBlock = computed(() => selectedRun.value && (!runUsesCurrentMode.value || runIsManagement.value || selectedRun.value.config.auto_publish) ? runPublishBlock.value : '')
const draftLimits = computed(() => platformLimits(selectedRun.value?.config.platforms || [], platformCatalog.value))
const flowSteps = computed(() => [
  '平台与网页账号',
  isManagement.value ? '核对目标商品' : isListing.value ? '商品资料与图片' : '宣传资料与素材',
  isManagement.value ? '确认执行规则' : '文案与发布策略',
])
function changeStep(step: number) {
  if (saving.value || pendingRequest.value) {
    return
  }
  formError.value = ''
  activeStep.value = step
}
function nextStep() {
  formError.value = validationError(activeStep.value === 0 ? 'environment' : 'all')
  if (!formError.value) {
    activeStep.value = Math.min(2, activeStep.value + 1)
  }
  else if (validationError('environment')) {
    activeStep.value = 0
  }
}
const selectedProduct = computed(() => resources.value.find(item => item.id === form.product_id))
const selectedVideoIds = computed({
  get: () => (form.video_id ? [form.video_id] : []),
  set: (ids: string[]) => {
    form.video_id = ids[0] || ''
  },
})
const formPrice = computed({
  get: () => form.price ?? undefined,
  set: (value: number | undefined) => {
    form.price = value
  },
})
const draftPrice = computed({
  get: () => draft.price ?? undefined,
  set: (value: number | undefined) => {
    draft.price = value
  },
})
const includesXianyu = computed(() => form.platforms.includes('xianyu'))
const requiresVideo = computed(() => selectedPlatforms.value.some(item => item.requires_video))
const runBusy = computed(() =>
  Boolean(
    selectedRun.value && ['queued', 'preparing', 'publishing'].includes(selectedRun.value.status),
  ),
)
const resultUncertain = computed(
  () =>
    selectedRun.value?.status === 'unknown'
    || Object.values(selectedRun.value?.platform_results || {}).some(
      result => result?.status === 'unknown',
    ),
)
const mayRetry = computed(() =>
  Boolean(
    selectedRun.value
    && automationPurpose(selectedRun.value.config) === currentPurpose.value
    && ['failed', 'partial'].includes(selectedRun.value.status)
    && !resultUncertain.value,
  ),
)
const mayEditDraft = computed(
  () =>
    selectedRun.value?.status === 'ready'
    && canManage.value
    && !runIsManagement.value
    && automationPurpose(selectedRun.value.config) === currentPurpose.value,
)
const runButtonText = computed(() =>
  pendingRequest.value
    ? '重试获取本次运行结果'
    : isManagement.value
      ? form.operation === 'relist'
        ? '运行并重新上架商品'
        : '运行并下架商品'
      : form.auto_publish
        ? form.approved_content_id ? isListing.value ? '使用已审核内容上架' : '发布已审核内容' : publishActionText(form.platforms, '运行并')
        : form.approved_content_id ? '读取内容并核对信息' : '运行并生成草稿',
)

function message(reason: unknown) {
  return reason instanceof Error ? reason.message : '操作失败，请稍后重试'
}
function stateLabel(state: string, operation: AutomationOperation = 'publish') {
  if (operation !== 'publish') {
    const action = operation === 'relist' ? '重新上架' : '下架'
    const labels: Record<string, string> = {
      preparing: '核对目标商品',
      ready: '待执行',
      publishing: `${action}中`,
      succeeded: `已${action}`,
      partial: '部分执行成功',
      unknown: '操作结果待核实',
    }
    return labels[state] || stateLabels[state] || state
  }
  return stateLabels[state] || state
}
function operationLabel(
  operation?: AutomationOperation,
  purpose: AutomationPurpose = currentPurpose.value,
) {
  return !operation || operation === 'publish'
    ? purpose === 'listing'
      ? '新商品上架'
      : '宣传视频发布'
    : operationLabels[operation]
}
function publishActionText(platforms: AutomationPlatform[], prefix: string) {
  if (platforms.length === 1) {
    return `${prefix}${platforms[0] === 'xianyu' ? '上架新商品' : `发布${platformName(platforms[0])}视频`}`
  }
  return `${prefix}发布到所选平台`
}
function connectionStatus(platform: AutomationPlatform) {
  if (isAndroid.value) {
    const app = availableDevices.value.find(item => item.device_id === form.device_id)?.apps?.[platform]
    return {
      configured: !!app?.account_id,
      missing: [],
      accounts: app?.account_id ? [{ account_id: app.account_id, label: app.account_label || platformName(platform) }] : [],
    }
  }
  const session = browserSessions.value[platform]
  if (session?.state === 'logged_in' && session.account_id) {
    return {
      configured: session.operations_ready?.[form.operation || 'publish'] ?? ((form.operation || 'publish') === 'publish' && session.publish_ready),
      missing: [],
      accounts: [{ account_id: session.account_id, label: session.account_label || platformName(platform) }],
    }
  }
  const platformStatus = executorStatus.value?.platforms?.[platform]
  const operation = platform === 'xianyu' ? form.operation || 'publish' : 'publish'
  return (
    (platformStatus && 'operations' in platformStatus ? platformStatus.operations?.[operation] : undefined)
    || (operation === 'publish' ? platformStatus : undefined)
  )
}
function browserUpdated(session: BrowserPlatformSession) {
  browserSessions.value = { ...browserSessions.value, [session.platform]: session }
  if (session.state === 'logged_in' && session.account_id && !form.accounts?.[session.platform]) {
    form.accounts = { ...form.accounts, [session.platform]: session.account_id }
  }
}
function platformsChanged() {
  const allowed = availablePlatforms.value.map(item => item.id)
  form.platforms = form.platforms.filter(platform => allowed.includes(platform))
  form.accounts = Object.fromEntries(Object.entries(form.accounts || {}).filter(([platform]) => form.platforms.includes(platform as AutomationPlatform)))
  formError.value = ''
}
async function inspectAndroid() {
  if (inspectingAndroid.value || !canManage.value) {
    return
  }
  inspectingAndroid.value = true
  const current = generation
  try {
    const android = await automationApi.inspectAndroid()
    if (current === generation) {
      status.value = { ...status.value, android }
      statusError.value = ''
    }
  }
  catch (reason) {
    if (current === generation) {
      statusError.value = message(reason)
    }
  }
  finally {
    if (current === generation) {
      inspectingAndroid.value = false
    }
  }
}
function executionLabel(config: AutomationConfig) {
  return executionMode(config) === 'android' ? '安卓 App' : '网页版'
}
function deviceLabel(config: AutomationConfig) {
  if (executionMode(config) !== 'android') {
    return ''
  }
  return availableDevices.value.find(item => item.device_id === config.device_id)?.label || config.device_id || '未绑定设备'
}
function operationChanged() {
  if (isManagement.value) {
    form.platforms = ['xianyu']
    form.auto_publish = true
  }
  const account = form.accounts?.xianyu
  if (account && !connectionStatus('xianyu')?.accounts.some(item => item.account_id === account)) {
    form.accounts = { ...form.accounts, xianyu: '' }
  }
  formError.value = ''
}
function sourceChanged() {
  if (isCustomProduct.value) {
    form.product_id = ''
  }
  formError.value = ''
}
function approvedChanged() {
  const item = selectedApproved.value
  if (!item) {
    return
  }
  form.source_mode = 'library'
  form.copy_mode = 'ai'
  form.product_id = ''
  form.image_ids = []
  form.video_id = null
  form.video_mode = 'existing'
  form.image_mode = 'existing'
  form.category = item.category
  form.auto_publish = true
  form.require_review = false
  if (!form.name) {
    form.name = `${item.product_name} · ${businessTitle.value}`.slice(0, 120)
  }
  formError.value = ''
}
function applyApprovedQuery() {
  const id = typeof route.query.approvedContent === 'string' ? route.query.approvedContent : ''
  if (!id || id === appliedApprovedQuery || !approvedContents.value.some(item => item.id === id)) {
    return
  }
  editWorkflow()
  form.approved_content_id = id
  approvedChanged()
  appliedApprovedQuery = id
}
function stateType(state: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (['succeeded', 'published', 'listed', 'delisted'].includes(state)) {
    return 'success'
  }
  if (['failed'].includes(state)) {
    return 'danger'
  }
  if (['partial', 'unknown', 'ready'].includes(state)) {
    return 'warning'
  }
  if (['queued', 'preparing', 'publishing', 'submitting'].includes(state)) {
    return 'primary'
  }
  return 'info'
}
function receiptUrl(value?: string) {
  try {
    const parsed = new URL(value || '')
    return ['https:', 'http:'].includes(parsed.protocol) ? parsed.href : undefined
  }
  catch {
    return undefined
  }
}
function resourceName(id: string) {
  return sourceResource(id)?.name || '原始图片'
}
function sourceResource(id: string) {
  return resources.value.find(item => item.id === id || item.resource_entry_id === id)
}
function platformName(platform: string) {
  return (
    platformCatalog.value.find(item => item.id === platform)?.label || ({ xianyu: '闲鱼', douyin: '抖音', xiaohongshu: '小红书', kuaishou: '快手' } as Record<string, string>)[platform] || platform
  )
}
function releasePreview() {
  if (previewUrl.value) {
    URL.revokeObjectURL(previewUrl.value)
  }
  previewUrl.value = ''
  imagePreviews.value.forEach(url => URL.revokeObjectURL(url))
  imagePreviews.value = []
}
function updateRun(run: AutomationRun, resetDraft = false) {
  if (automationPurpose(run.config) !== currentPurpose.value) {
    return
  }
  if (selectedRun.value?.id !== run.id || selectedRun.value?.draft.review?.revision !== run.draft.review?.revision) {
    releasePreview()
    resetDraft = true
  }
  selectedRun.value = run
  runs.value = [run, ...runs.value.filter(item => item.id !== run.id)].sort((left, right) =>
    compareContentDateTimeDescending(left.created_at, right.created_at),
  )
  if (resetDraft) {
    Object.assign(draft, { title: '', description: '', category: '', price: null }, run.draft || {})
  }
}
function editWorkflow(workflow?: AutomationWorkflow) {
  if (saving.value || uploadingImages.value || pendingRequest.value) {
    return
  }
  if (workflow && automationPurpose(workflow.config) !== currentPurpose.value) {
    return
  }
  activeWorkspace.value = 'configuration'
  activeStep.value = 0
  workflowId.value = workflow?.id || ''
  const source = workflowForPurpose({ ...initialConfig(), ...workflow?.config }, currentPurpose.value)
  Object.assign(form, initialConfig(), source, {
    name: workflow?.name || '',
    image_ids: [...(workflow?.config.image_ids || [])],
    purpose: currentPurpose.value,
    platforms: [...(workflow?.config.platforms || initialConfig().platforms)],
    execution_mode: purposeExecutionMode(currentPurpose.value),
    device_id: source.device_id,
    operation: isListing.value ? workflow?.config.operation || 'publish' : 'publish',
    accounts: { ...source.accounts },
    copy_mode: workflow ? workflow.config.copy_mode || 'ai' : initialConfig().copy_mode,
  })
  executionNotice.value = workflow && executionMode(workflow.config) !== source.execution_mode
    ? '此工作流已切换为网页版发布，请重新选择网页账号后保存。'
    : ''
  formError.value = ''
}
function chooseProduct(_name: string, resourceId: string) {
  const item = resources.value.find(item => [item.id, item.resource_entry_id].includes(resourceId))
  form.product_id = item?.id || ''
}
async function uploadListingImages(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length || uploadingImages.value || saving.value || !canManage.value || pendingRequest.value) {
    return
  }
  if (files.length + form.image_ids.length > 9) {
    formError.value = '每件商品最多 9 张图片，请减少本次选择数量'
    return
  }
  if (files.some(file => !/\.(?:jpe?g|png|webp)$/iu.test(file.name) || !file.size || file.size > 20 * 1024 * 1024)) {
    formError.value = '请上传 JPG、PNG 或 WebP 图片，每张不超过 20 MB'
    return
  }
  uploadingImages.value = true
  formError.value = ''
  const current = generation
  let completed = 0
  try {
    for (const file of files) {
      if (current !== generation || !active) {
        break
      }
      const item = await contentApi.uploadLibrary({
        resource_kind: 'image',
        category: 'image',
        name: file.name.replace(/\.[^.]+$/u, '').trim().slice(0, 120) || '商品图片',
        data_level: 'internal',
        file,
      })
      if (current !== generation || !active) {
        break
      }
      resources.value = [item, ...resources.value.filter(existing => existing.id !== item.id)]
      form.image_ids = [...form.image_ids, item.id]
      completed++
    }
    if (completed && current === generation) {
      ElMessage.success(`已添加 ${completed} 张图片`)
    }
  }
  catch (reason) {
    if (current === generation) {
      formError.value = `已添加 ${completed} 张；${message(reason)}。成功的图片已保留，可继续添加剩余图片。`
    }
  }
  finally {
    uploadingImages.value = false
  }
}
function useProductImages() {
  if (!selectedProduct.value) {
    return
  }
  form.image_ids = relatedMedia(selectedProduct.value, resources.value)
    .filter(
      item =>
        ['image', 'logo'].includes(item.resource_kind)
        && Boolean(item.has_asset || item.storage_key || item.download_url),
    )
    .slice(0, 9)
    .map(item => item.id)
}
function validationError(scope: 'all' | 'environment' = 'all') {
  if (uploadingImages.value) {
    return '图片正在上传，请稍候'
  }
  if (!form.name.trim()) {
    return '请填写工作流名称'
  }
  if (!form.platforms.length) {
    return '请至少选择一个发布平台'
  }
  if (!platformCatalog.value.length) {
    return '平台目录尚未读取，请刷新状态后重试'
  }
  if (form.platforms.some(platform => !availablePlatforms.value.some(item => item.id === platform))) {
    return '所选平台不支持当前业务、执行方式或操作，请重新选择'
  }
  if (automationPurpose(form) !== currentPurpose.value) {
    return '请在对应业务页面新建工作流，商品上架与内容宣传分别执行'
  }
  if (scope === 'environment') {
    return ''
  }
  if (isManagement.value) {
    if (!form.target_title?.trim()) {
      return '请填写闲鱼商品当前的完整标题，用于精确核对目标商品'
    }
    if (form.target_title.trim().length > 120 || /[\r\n\t]/u.test(form.target_title)) {
      return '商品完整标题最多 120 个字符，且不能包含换行或制表符'
    }
    try {
      const target = new URL(form.target_url?.trim() || '')
      const queryKeys: string[] = []
      target.searchParams.forEach((_value, key) => queryKeys.push(key))
      if (
        target.protocol !== 'https:'
        || !['goofish.com', 'www.goofish.com'].includes(target.hostname)
        || target.pathname !== '/item'
        || !/^\d{1,30}$/u.test(target.searchParams.get('id') || '')
        || queryKeys.some(key => !['id', 'categoryId', 'spm'].includes(key))
        || target.searchParams.getAll('id').length !== 1
        || target.searchParams.getAll('categoryId').length > 1
        || target.searchParams.getAll('spm').length > 1
        || (target.searchParams.has('categoryId')
          && !/^\d{1,30}$/u.test(target.searchParams.get('categoryId') || ''))
        || (target.searchParams.has('spm')
          && !/^[\w.-]{1,200}$/u.test(target.searchParams.get('spm') || ''))
        || /\s/u.test(form.target_url?.trim() || '')
        || target.hash
        || target.username
        || target.password
        || target.port
      ) {
        return '请填写 https://www.goofish.com/item?id=商品编号 格式的闲鱼商品链接'
      }
    }
    catch {
      return '请填写有效的闲鱼商品链接'
    }
    return ''
  }
  if (form.approved_content_id) {
    if (!selectedApproved.value) {
      return '所选审核内容不可用，请重新选择'
    }
    if (includesXianyu.value && (!form.price || form.price <= 0 || !form.condition.trim() || !form.location.trim())) {
      return '请填写商品售价、成色和发货地'
    }
    return ''
  }
  if (isCustomProduct.value && !form.custom_product_name?.trim()) {
    return isListing.value ? '请填写自定义商品名称' : '请填写宣传对象名称'
  }
  if (isCustomProduct.value && !form.custom_product_description?.trim()) {
    return isListing.value
      ? '请填写自定义商品的真实规格、状态或其他事实描述'
      : '请填写宣传对象的真实特点、品牌故事或活动信息'
  }
  if (isCustomProduct.value && !form.image_ids.length) {
    return '自定义资料至少需要选择一张发布图片'
  }
  if (isListing.value && isCustomProduct.value && form.copy_mode === 'manual') {
    if ((form.custom_product_name?.trim().length || 0) > 30 || (form.custom_product_description?.trim().length || 0) > 1000) {
      return '商品名称最多 30 字，描述最多 1000 字'
    }
    if (!form.category?.trim()) {
      return '请填写闲鱼商品分类'
    }
  }
  if (!isCustomProduct.value && !form.product_id && !form.image_ids.length) {
    return isListing.value
      ? '请选择商品资料或图片，用于生成商品文案'
      : '请选择宣传资料或图片，用于生成宣传文案'
  }
  if (includesXianyu.value && (!form.price || form.price <= 0)) {
    return '请填写大于 0 的商品售价'
  }
  if (includesXianyu.value && !form.location.trim()) {
    return '请填写闲鱼商品发货地'
  }
  if (includesXianyu.value && !form.condition.trim()) {
    return '请填写商品成色'
  }
  if (includesXianyu.value && !form.product_id && !form.image_ids.length) {
    return '闲鱼发布需要商品图片或有关联图片的商品资料'
  }
  if (requiresVideo.value && form.video_mode === 'existing' && !form.video_id) {
    return '请选择要发布的视频'
  }
  if (
    requiresVideo.value
    && form.video_mode === 'slideshow'
    && !form.product_id
    && !form.image_ids.length
  ) {
    return '图片轮播视频需要选择图片或商品资料'
  }
  return ''
}
async function persistWorkflow() {
  const current = generation
  const payload: AutomationConfig = {
    ...form,
    name: form.name.trim(),
    purpose: currentPurpose.value,
    execution_mode: executionMode(form),
    device_id: isAndroid.value ? form.device_id || null : null,
    operation: isListing.value ? form.operation || 'publish' : 'publish',
    target_url: isManagement.value ? form.target_url?.trim() || null : null,
    target_title: isManagement.value ? form.target_title?.trim() || null : null,
    source_mode: isManagement.value ? 'library' : form.source_mode || 'library',
    copy_mode: isListing.value && isCustomProduct.value && !isManagement.value ? form.copy_mode || 'ai' : 'ai',
    custom_product_name:
      !isManagement.value && isCustomProduct.value ? form.custom_product_name?.trim() || '' : '',
    custom_product_description:
      !isManagement.value && isCustomProduct.value
        ? form.custom_product_description?.trim() || ''
        : '',
    product_id: isManagement.value || isCustomProduct.value ? null : form.product_id || null,
    video_id: !isListing.value && form.video_mode === 'existing' ? form.video_id || null : null,
    image_ids: isManagement.value ? [] : [...form.image_ids],
    auto_publish: isManagement.value || form.auto_publish,
    platforms: [...form.platforms],
    accounts: Object.fromEntries(
      Object.entries(form.accounts || {}).filter(
        ([platform, account]) => form.platforms.includes(platform as AutomationPlatform) && account,
      ),
    ),
    price: !isListing.value || isManagement.value ? null : form.price,
    condition: isListing.value ? form.condition : '',
    shipping_fee: isListing.value ? form.shipping_fee : 0,
    category: isManagement.value ? null : form.category?.trim() || null,
    instructions: isManagement.value ? '' : form.instructions.trim(),
    location: !isListing.value || isManagement.value ? '' : form.location.trim(),
  }
  const result = await automationApi.saveWorkflow(payload, workflowId.value || undefined)
  if (current !== generation) {
    throw new Error('业务或账号已切换，请重新加载工作流')
  }
  workflowId.value = result.id
  workflows.value = [result, ...workflows.value.filter(item => item.id !== result.id)]
  return result
}
async function saveWorkflow() {
  if (saving.value || !canManage.value || pendingRequest.value) {
    return
  }
  formError.value = validationError()
  if (formError.value) {
    activeStep.value = validationError('environment') ? 0 : 1
    return
  }
  saving.value = true
  const current = generation
  try {
    await persistWorkflow()
    if (current === generation) {
      ElMessage.success('工作流已保存')
    }
  }
  catch (reason) {
    if (current === generation) {
      formError.value = message(reason)
    }
  }
  finally {
    if (current === generation) {
      saving.value = false
    }
  }
}
async function startRun() {
  if (saving.value || !canManage.value) {
    return
  }
  formError.value = pendingRequest.value ? '' : validationError()
  if (formError.value) {
    activeStep.value = validationError('environment') ? 0 : 1
    return
  }
  if (!pendingRequest.value && isAndroid.value && !form.device_id) {
    formError.value = '请先选择执行设备，再生成可导入 App 的草稿。未绑定设备的规则可以先保存。'
    activeStep.value = 0
    return
  }
  if (!formError.value && !pendingRequest.value) {
    formError.value = startBlock.value
  }
  if (formError.value) {
    return
  }
  saving.value = true
  const current = generation
  try {
    if (!pendingRequest.value) {
      const workflow = await persistWorkflow()
      if (current !== generation) {
        return
      }
      pendingRequest.value = { workflowId: workflow.id, key: crypto.randomUUID() }
    }
    const request = pendingRequest.value
    const run = await automationApi.startRun(request.workflowId, request.key)
    if (current !== generation) {
      return
    }
    updateRun(run, true)
    pendingRequest.value = undefined
    runError.value = ''
    activeWorkspace.value = 'runs'
    ElMessage.success('运行任务已提交')
  }
  catch (reason) {
    if (current !== generation) {
      return
    }
    formError.value = message(reason)
    if (
      reason
      && typeof reason === 'object'
      && 'status' in reason
      && Number(reason.status) >= 400
      && Number(reason.status) < 500
    ) {
      pendingRequest.value = undefined
    }
  }
  finally {
    if (current === generation) {
      saving.value = false
    }
  }
}
async function selectRun(run: AutomationRun) {
  if (actionLoading.value || automationPurpose(run.config) !== currentPurpose.value) {
    return
  }
  const version = ++runSelectionVersion
  const current = generation
  updateRun(run, true)
  runError.value = ''
  try {
    const result = await automationApi.run(run.id)
    if (version === runSelectionVersion && current === generation) {
      updateRun(result, true)
    }
  }
  catch (reason) {
    if (version === runSelectionVersion && current === generation) {
      runError.value = message(reason)
    }
  }
}
async function runAction(action: 'retry' | 'cancel' | 'publish') {
  if (
    !selectedRun.value
    || actionLoading.value
    || !canManage.value
    || automationPurpose(selectedRun.value.config) !== currentPurpose.value
  ) {
    return
  }
  if (action === 'retry' && !mayRetry.value) {
    return
  }
  if (action === 'publish' && !mayEditDraft.value) {
    return
  }
  if ((action === 'publish' && runPublishBlock.value) || (action === 'retry' && retryBlock.value)) {
    runError.value = action === 'publish' ? runPublishBlock.value : retryBlock.value
    return
  }
  actionLoading.value = true
  const current = generation
  const id = selectedRun.value.id
  runError.value = ''
  try {
    if (action === 'publish') {
      if (!androidDraftSaved.value) {
        const saved = await automationApi.saveDraft(id, draftPayload())
        if (current === generation) {
          updateRun(saved, true)
        }
        runError.value = '修改已保存，请检查最新版本并重新审核。'
        return
      }
      if (selectedRun.value.config.require_review && selectedRun.value.draft.review?.state !== 'approved') {
        runError.value = '请先审核通过当前版本。'
        return
      }
    }
    if (current !== generation) {
      return
    }
    const result = await automationApi.action(id, action)
    if (current === generation) {
      updateRun(result, true)
    }
  }
  catch (reason) {
    if (current === generation) {
      runError.value = message(reason)
    }
  }
  finally {
    if (current === generation) {
      actionLoading.value = false
    }
  }
}
function draftPayload() {
  return {
    expected_revision: selectedRun.value?.draft.review?.revision || 1,
    title: draft.title || '',
    description: draft.description || '',
    category: draft.category || '',
    ...(isListing.value ? { price: draft.price ?? null } : {}),
  }
}
async function reviewAction(action: 'approve' | 'revise' | 'archive') {
  if (!selectedRun.value || !mayEditDraft.value || actionLoading.value) {
    return
  }
  if (!androidDraftSaved.value) {
    runError.value = '请先保存文案，再审核或提交修改意见。'
    return
  }
  const current = generation
  const run = selectedRun.value
  actionLoading.value = true
  runError.value = ''
  try {
    const version = run.draft.review?.revision || 1
    const result = action === 'approve' || action === 'archive'
      ? await automationApi[action](run.id, version)
      : await automationApi.revise(run.id, version, reviewFeedback.value.trim() || '按当前文案重新合成视频字幕', revisionScope.value)
    if (current === generation) {
      updateRun(result, true)
      if (action === 'revise') {
        reviewFeedback.value = ''
      }
    }
  }
  catch (reason) {
    if (current === generation) {
      runError.value = message(reason)
    }
  }
  finally {
    if (current === generation) {
      actionLoading.value = false
    }
  }
}
async function loadImages() {
  if (!selectedRun.value || previewLoading.value) {
    return
  }
  const run = selectedRun.value
  const version = run.draft.review?.revision
  const current = generation
  previewLoading.value = true
  try {
    const images = await Promise.all(Array.from({ length: run.draft.image_count || 0 }, (_, index) => automationApi.image(run.id, index)))
    if (current === generation && selectedRun.value?.id === run.id && selectedRun.value.draft.review?.revision === version && active) {
      imagePreviews.value.forEach(url => URL.revokeObjectURL(url))
      imagePreviews.value = images.map(result => URL.createObjectURL(result.blob))
    }
  }
  catch (reason) {
    if (current === generation) {
      runError.value = message(reason)
    }
  }
  finally {
    if (current === generation) {
      previewLoading.value = false
    }
  }
}
async function copyListingDraft() {
  const text = [draft.title, draft.description].filter(Boolean).join('\n')
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('商品文案已复制')
  }
  catch {
    runError.value = '复制失败，请在文案输入框中选择并复制。'
  }
}
async function openListingPage() {
  if (actionLoading.value || !canManage.value) {
    return
  }
  actionLoading.value = true
  const current = generation
  runError.value = ''
  try {
    const session = await automationApi.openBrowser('xianyu', 'publish')
    if (current === generation) {
      browserUpdated(session)
      if (session.state === 'unavailable') {
        runError.value = session.message || '闲鱼网页暂未打开，请重试。'
      }
    }
  }
  catch (reason) {
    if (current === generation) {
      runError.value = message(reason)
    }
  }
  finally {
    if (current === generation) {
      actionLoading.value = false
    }
  }
}
async function saveDraft() {
  if (!selectedRun.value || !mayEditDraft.value || actionLoading.value) {
    return
  }
  actionLoading.value = true
  const current = generation
  runError.value = ''
  try {
    const result = await automationApi.saveDraft(selectedRun.value.id, draftPayload())
    if (current !== generation) {
      return
    }
    updateRun(result, true)
    ElMessage.success('发布文案已保存')
  }
  catch (reason) {
    if (current === generation) {
      runError.value = message(reason)
    }
  }
  finally {
    if (current === generation) {
      actionLoading.value = false
    }
  }
}
async function loadPreview() {
  if (
    !selectedRun.value
    || previewLoading.value
    || isListing.value
    || automationPurpose(selectedRun.value.config) !== currentPurpose.value
  ) {
    return
  }
  previewLoading.value = true
  const current = generation
  const id = selectedRun.value.id
  try {
    const result = await automationApi.video(id)
    if (current === generation && selectedRun.value?.id === id && active) {
      releasePreview()
      previewUrl.value = URL.createObjectURL(result.blob)
    }
  }
  catch (reason) {
    if (current === generation) {
      runError.value = message(reason)
    }
  }
  finally {
    if (current === generation) {
      previewLoading.value = false
    }
  }
}
async function load() {
  if (loading.value) {
    return
  }
  loading.value = true
  const current = generation
  error.value = ''
  statusError.value = ''
  await refreshAccess()
  if (current !== generation) {
    return
  }
  const results = await Promise.allSettled([
    automationApi.workflows(currentPurpose.value),
    contentApi.listLibrary(),
    automationApi.status(),
    automationApi.runs(undefined, currentPurpose.value),
    automationApi.workflows('legacy'),
    automationApi.runs(undefined, 'legacy'),
    automationApi.platforms(),
    automationApi.approvedContents(),
  ])
  if (current === generation) {
    const [
      workflowResult,
      resourceResult,
      statusResult,
      runResult,
      legacyWorkflowResult,
      legacyRunResult,
      platformResult,
      approvedResult,
    ] = results
    if (approvedResult.status === 'fulfilled') {
      approvedContents.value = approvedResult.value.items
      applyApprovedQuery()
    }
    if (platformResult.status === 'fulfilled') {
      platformCatalog.value = platformResult.value.items
    }
    else {
      platformCatalog.value = []
      statusError.value = message(platformResult.reason)
    }
    if (legacyWorkflowResult.status === 'fulfilled') {
      legacyWorkflows.value = legacyWorkflowResult.value.items
    }
    if (legacyRunResult.status === 'fulfilled') {
      legacyRuns.value = legacyRunResult.value.items
    }
    if (workflowResult.status === 'fulfilled') {
      workflows.value = workflowResult.value.items
    }
    if (resourceResult.status === 'fulfilled') {
      resources.value = resourceResult.value.filter(item => item.active !== false)
    }
    if (statusResult.status === 'fulfilled') {
      status.value = statusResult.value
    }
    else {
      status.value = undefined
      statusError.value = message(statusResult.reason)
    }
    if (runResult.status === 'fulfilled') {
      runs.value = runResult.value.items
      if (!selectedRun.value && businessRuns.value[0]) {
        updateRun(businessRuns.value[0], true)
      }
      else {
        const selected = businessRuns.value.find(run => run.id === selectedRun.value?.id)
        if (selected) {
          updateRun(selected, selected.status !== 'ready' || selectedRun.value?.status !== 'ready')
        }
      }
    }
    error.value = [workflowResult, resourceResult, runResult, legacyWorkflowResult, legacyRunResult, approvedResult]
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map(result => message(result.reason))
      .join('；')
  }
  if (current === generation) {
    loading.value = false
  }
}
async function pollRuns() {
  if (
    polling
    || !active
    || !businessRuns.value.some(item => ['queued', 'preparing', 'publishing'].includes(item.status))
  ) {
    return
  }
  polling = true
  const current = generation
  try {
    const result = await automationApi.runs(undefined, currentPurpose.value)
    if (current !== generation) {
      return
    }
    runs.value = result.items
    const selected = businessRuns.value.find(item => item.id === selectedRun.value?.id)
    if (selected && (selectedRun.value?.status !== 'ready' || selected.status !== 'ready')) {
      updateRun(selected, true)
    }
    runError.value = ''
  }
  catch (reason) {
    if (current === generation) {
      runError.value = `运行状态刷新失败：${message(reason)}`
    }
  }
  finally {
    if (current === generation) {
      polling = false
    }
  }
}
function invalidateRequests() {
  generation++
  runSelectionVersion++
  loading.value = false
  saving.value = false
  actionLoading.value = false
  previewLoading.value = false
  polling = false
  inspectingAndroid.value = false
}
function resetBusinessView() {
  invalidateRequests()
  activeStep.value = 0
  activeWorkspace.value = 'configuration'
  workflowId.value = ''
  selectedRun.value = undefined
  Object.assign(form, initialConfig())
  Object.assign(draft, { title: '', description: '', category: '', price: null })
  formError.value = ''
  runError.value = ''
  error.value = ''
  executionNotice.value = ''
  browserSessions.value = {}
  releasePreview()
}
watch(
  currentPurpose,
  () => {
    resetBusinessView()
    if (active) {
      void load()
    }
  },
  { flush: 'sync' },
)
watch(() => route.query.approvedContent, () => {
  if (!route.query.approvedContent) {
    appliedApprovedQuery = ''
  }
  if (active) {
    applyApprovedQuery()
  }
})
watch(
  () => access.value?.subject,
  (subject, previous) => {
    if (previous && subject !== previous) {
      resetBusinessView()
      workflows.value = []
      resources.value = []
      runs.value = []
      legacyWorkflows.value = []
      legacyRuns.value = []
      status.value = undefined
      platformCatalog.value = []
      delete pendingRequests.listing
      delete pendingRequests.promotion
      if (active) {
        void load()
      }
    }
  },
)
onActivated(() => {
  active = true
  void load()
  clearInterval(pollTimer)
  pollTimer = setInterval(() => void pollRuns(), 4000)
})
onDeactivated(() => {
  active = false
  invalidateRequests()
  releasePreview()
  clearInterval(pollTimer)
})
onBeforeUnmount(() => {
  active = false
  generation++
  clearInterval(pollTimer)
  releasePreview()
})
</script>

<template>
  <section class="automation-page" aria-labelledby="automation-title">
    <header class="page-heading">
      <div>
        <h1 id="automation-title">{{ pageTitle }}</h1>
        <p>
          {{
            isListing
              ? '保存可重复执行的闲鱼规则：新商品上架，以及已有商品的重新上架和下架。'
              : '从商品资料生成文案、宣传图片和视频，按修改意见调整，人工审核后自动上传到所选平台。'
          }}
        </p>
      </div>
      <ElButton :loading="loading" @click="load">刷新状态</ElButton>
    </header>
    <ElRadioGroup
      v-if="!props.purpose"
      v-model="internalPurpose"
      class="business-tabs"
      aria-label="选择业务"
    >
      <ElRadioButton value="listing">商品上架</ElRadioButton>
      <ElRadioButton value="promotion">内容宣传</ElRadioButton>
    </ElRadioGroup>

    <div class="workspace-switcher" aria-label="工作流视图">
      <button
        type="button"
        :class="{ active: activeWorkspace === 'configuration' }"
        :aria-pressed="activeWorkspace === 'configuration'"
        aria-controls="workflow-configuration"
        @click="activeWorkspace = 'configuration'"
      >
        配置规则 <span>{{ businessWorkflows.length }}</span>
      </button>
      <button
        type="button"
        :class="{ active: activeWorkspace === 'runs' }"
        :aria-pressed="activeWorkspace === 'runs'"
        aria-controls="workflow-runs"
        @click="activeWorkspace = 'runs'"
      >
        运行记录 <span>{{ businessRuns.length }}</span>
      </button>
    </div>
    <ElAlert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
    />
    <ElAlert
      v-if="!canManage && !loading"
      title="当前账号没有自动化配置与执行权限，请由有资料管理权限的账号操作。"
      type="info"
      show-icon
      :closable="false"
    />

    <div v-show="activeWorkspace === 'configuration'" id="workflow-configuration" class="workspace-grid">
      <aside class="workflow-sidebar panel">
        <div class="section-heading">
          <h2>{{ businessTitle }}工作流</h2>
          <ElButton
            link
            type="primary"
            :disabled="saving || !!pendingRequest"
            @click="editWorkflow()"
          >
            新建
          </ElButton>
        </div>
        <p v-if="!businessWorkflows.length" class="workflow-empty">暂无已保存的工作流<br><span>配置完成后可保存并重复使用</span></p>
        <button
          v-for="workflow in businessWorkflows"
          :key="workflow.id"
          type="button"
          class="workflow-item"
          :class="{ selected: workflowId === workflow.id }"
          :disabled="saving || !!pendingRequest"
          :aria-pressed="workflowId === workflow.id"
          @click="editWorkflow(workflow)"
        >
          <strong>{{ workflow.name }}</strong>
          <span>{{ workflow.config.platforms.map(platformName).join(' / ') }}</span>
          <small>{{ executionLabel(workflow.config) }}<template v-if="deviceLabel(workflow.config)"> · {{ deviceLabel(workflow.config) }}</template></small>
          <small>
            {{
              (workflow.config.operation || 'publish') !== 'publish'
                ? operationLabel(workflow.config.operation)
                : workflow.config.auto_publish
                  ? '按规则直接发布'
                  : '人工审核后上传发布'
            }}
          </small>
        </button>
      </aside>

      <section class="panel workflow-editor" aria-labelledby="rules-title">
        <div class="section-heading">
          <h2 id="rules-title">{{ workflowId ? '编辑执行规则' : '新建执行规则' }}</h2>
          <ElTag effect="plain">{{ executionLabel(form) }}</ElTag>
        </div>
        <ol class="flow-summary" aria-label="配置步骤">
          <li v-for="(step, index) in flowSteps" :key="step">
            <button
              type="button"
              :class="{ current: activeStep === index, completed: activeStep > index }"
              :aria-current="activeStep === index ? 'step' : undefined"
              :disabled="saving || !!pendingRequest"
              @click="changeStep(index)"
            >
              <span>0{{ index + 1 }}</span>
              {{ step }}
            </button>
          </li>
        </ol>
        <ElForm label-position="top" :disabled="formDisabled">
          <div v-show="activeStep === 0" class="form-step">
            <div class="form-columns">
              <ElFormItem label="工作流名称" required>
                <ElInput
                  v-model="form.name"
                  maxlength="120"
                  :placeholder="isListing ? '例如：闲鱼新商品上架' : '例如：新品多平台宣传视频'"
                />
              </ElFormItem>
              <ElFormItem label="发布方式">
                <span>{{ isListing ? '闲鱼 · 网页版' : '抖音 / 小红书 / 快手 · 网页版' }}</span>
              </ElFormItem>
            </div>
            <div :class="{ 'form-columns': isListing }">
              <ElFormItem v-if="isListing" label="执行操作" required>
                <ElRadioGroup v-model="form.operation" @change="operationChanged">
                  <ElRadio value="publish">新商品上架</ElRadio>
                  <ElRadio v-if="isManagement" value="relist">重新上架</ElRadio>
                  <ElRadio v-if="isManagement" value="delist">下架</ElRadio>
                </ElRadioGroup>
              </ElFormItem>
              <ElFormItem :label="isListing ? '商品平台' : '宣传平台'" required>
                <ElCheckboxGroup v-if="availablePlatforms.length" v-model="form.platforms" @change="platformsChanged">
                  <ElCheckbox v-for="platform in availablePlatforms" :key="platform.id" :value="platform.id">{{ platform.label }}</ElCheckbox>
                </ElCheckboxGroup>
                <span v-else class="field-hint">{{ statusError || '正在读取可选平台；如未加载，请刷新状态。' }}</span>
              </ElFormItem>
            </div>
            <p v-if="executionNotice" class="field-hint execution-notice" role="status">{{ executionNotice }}</p>
            <section v-if="isAndroid" class="android-connection" aria-label="安卓执行环境">
              <div class="section-heading">
                <strong>安卓设备</strong>
                <ElButton :loading="inspectingAndroid" :disabled="!canManage" @click="inspectAndroid">检测设备</ElButton>
              </div>
              <ElFormItem label="执行设备">
                <ElSelect v-model="selectedDeviceId" clearable placeholder="选择安卓设备">
                  <ElOption
                    v-for="device in availableDevices"
                    :key="device.device_id"
                    :value="device.device_id"
                    :label="`${device.label} · ${device.connected === true ? '已连接' : device.connected === false ? '未连接' : '待检测'}`"
                  />
                </ElSelect>
              </ElFormItem>
              <p v-if="!availableDevices.some(device => device.connected)" class="field-hint" role="status">点击“检测设备”连接服务器安卓。</p>
              <ContentAndroidPanel
                :device="availableDevices.find(device => device.device_id === selectedDeviceId)"
                :enabled="canManage && !saving && !pendingRequest"
                :platforms="form.platforms"
                @updated="inspectAndroid"
              />
            </section>
            <ContentBrowserPanel
              v-else
              :key="currentPurpose"
              :purpose="currentPurpose"
              :enabled="canManage && !saving && !pendingRequest"
              @updated="browserUpdated"
            />
            <div v-if="form.accounts" class="form-columns">
              <ElFormItem
                v-for="platform in selectedPlatforms"
                :key="platform.id"
                :label="`${platformName(platform.id)}账号`"
                :required="isManagement"
              >
                <ElSelect
                  v-model="form.accounts[platform.id]"
                  clearable
                  :placeholder="isAndroid ? '请先打开 App 登录并绑定' : '请先打开网页登录并检测账号'"
                >
                  <ElOption
                    v-for="account in connectionStatus(platform.id)?.accounts || []"
                    :key="account.account_id"
                    :value="account.account_id"
                    :label="account.label"
                  />
                </ElSelect>
              </ElFormItem>
            </div>
          </div>
          <div v-show="activeStep === 1" class="form-step">
            <div class="step-heading"><h3>{{ flowSteps[1] }}</h3><p>{{ isManagement ? '使用商品链接与完整标题准确定位商品。' : '选用资料库素材，或填写本次创作的真实信息。' }}</p></div>
            <template v-if="isManagement">
              <p v-if="!browserSessions.xianyu?.operations_ready?.[form.operation || 'publish']" class="field-hint">
                闲鱼当前网页版“我发布的”仍提示待上线，暂不能通过网页完成上下架。可先保存规则，待平台开放并完成验证后执行。
              </p>
              <ElFormItem label="闲鱼商品链接" required>
                <ElInput
                  v-model="form.target_url"
                  maxlength="500"
                  placeholder="https://www.goofish.com/item?id=商品编号"
                />
              </ElFormItem>
              <ElFormItem label="商品当前的完整标题" required>
                <ElInput
                  v-model="form.target_title"
                  maxlength="120"
                  placeholder="与闲鱼页面显示的商品标题完全一致"
                />
              </ElFormItem>
              <p class="field-hint">
                执行时会核对所选账号、商品链接与完整标题，仅对匹配商品{{
                  form.operation === 'relist' ? '重新上架' : '下架'
                }}。运行结果以平台回执为准。
              </p>
            </template>
            <template v-else>
              <ElFormItem label="选择已审核内容">
                <ElSelect
                  v-model="approvedContentId"
                  clearable
                  filterable
                  placeholder="选择审核入库的产品内容"
                  @change="approvedChanged"
                >
                  <ElOption
                    v-for="item in approvedContents"
                    :key="item.id"
                    :value="item.id"
                    :label="`${item.product_name} · ${item.title} · 第 ${item.revision} 版`"
                  />
                </ElSelect>
              </ElFormItem>
              <div v-if="selectedApproved" class="approved-source">
                <ElTag type="success">已人工审核 · 第 {{ selectedApproved.revision }} 版</ElTag>
                <h4>{{ isListing ? selectedApproved.listing_title : selectedApproved.title }}</h4>
                <p>{{ isListing ? selectedApproved.listing_description : selectedApproved.description }}</p>
                <small>{{ isListing ? `使用 ${selectedApproved.original_image_ids.length} 张商品原图` : `使用 ${selectedApproved.image_ids.length} 张宣传图及已审核视频` }}，直接读取入库内容。</small>
              </div>
              <ElButton
                v-else
                link
                type="primary"
                @click="router.push('/agent/content/production')"
              >
                前往产品创作与审核，生成内容后入库
              </ElButton>
              <template v-if="!form.approved_content_id">
                <p v-if="includesXianyu" class="field-hint video-note">
                  闲鱼新商品上架对应「发闲置」：选择数据库商品或填写自定义资料，选好图片并填写售价与发货信息后发布新商品。
                </p>
                <ElFormItem :label="isListing ? '商品资料来源' : '宣传资料来源'" required>
                  <ElRadioGroup v-model="form.source_mode" @change="sourceChanged">
                    <ElRadio value="library">{{ isListing ? '数据库商品' : '数据库资料' }}</ElRadio>
                    <ElRadio value="custom">{{ isListing ? '自定义商品' : '自定义宣传资料' }}</ElRadio>
                  </ElRadioGroup>
                </ElFormItem>
                <template v-if="isCustomProduct">
                  <ElFormItem v-if="isListing" label="文案方式">
                    <ElRadioGroup v-model="form.copy_mode">
                      <ElRadio value="manual">使用填写的文案</ElRadio>
                      <ElRadio value="ai">AI 生成文案</ElRadio>
                    </ElRadioGroup>
                  </ElFormItem>
                  <ElFormItem :label="isListing ? '自定义商品名称' : '宣传对象名称'" required>
                    <ElInput
                      v-model="form.custom_product_name"
                      :maxlength="isListing && form.copy_mode === 'manual' ? 30 : 120"
                      show-word-limit
                      :placeholder="isListing ? '填写你要上架的商品名称' : '填写产品、品牌或活动名称'"
                    />
                  </ElFormItem>
                  <ElFormItem :label="isListing ? form.copy_mode === 'manual' ? '商品发布文案' : '商品事实描述' : '宣传事实与素材说明'" required>
                    <ElInput
                      v-model="form.custom_product_description"
                      type="textarea"
                      :rows="4"
                      :maxlength="isListing && form.copy_mode === 'manual' ? 1000 : 4000"
                      show-word-limit
                      :placeholder="
                        isListing
                          ? form.copy_mode === 'manual' ? '填写要直接发布的商品介绍，说明规格、成色、瑕疵及配件。' : '如实填写规格、材质、使用情况、瑕疵、配件等商品信息，AI 将据此生成文案。'
                          : '填写产品特点、品牌故事、活动信息等真实内容，AI 将据此生成宣传文案。'
                      "
                    />
                  </ElFormItem>
                </template>
                <ElFormItem v-else :label="isListing ? '数据库商品资料' : '数据库宣传资料'">
                  <ContentProductPicker
                    :model-value="selectedProduct?.name || ''"
                    :product-resource-id="form.product_id || ''"
                    :resources="resources"
                    @change="chooseProduct"
                  />
                </ElFormItem>
                <ElFormItem label="发布图片" :required="isCustomProduct">
                  <div class="full-width">
                    <div v-if="isListing" class="local-image-upload">
                      <input
                        ref="imageInput"
                        type="file"
                        accept="image/jpeg,image/png,image/webp"
                        multiple
                        hidden
                        :disabled="uploadingImages || saving || !canManage || !!pendingRequest"
                        @change="uploadListingImages"
                      >
                      <ElButton :loading="uploadingImages" :disabled="saving || !canManage || !!pendingRequest || form.image_ids.length >= 9" @click="imageInput?.click()">本地上传图片</ElButton>
                      <span class="field-hint">支持多选，最多 9 张；第一张作为主图</span>
                    </div>
                    <ContentResourcePicker
                      v-model="form.image_ids"
                      :resources="resources"
                      :kinds="['image', 'logo']"
                      title="选择发布图片"
                      button-text="从数据库选择图片"
                      :max-selections="9"
                      :disabled="saving || uploadingImages || !canManage || !!pendingRequest"
                    />
                    <div class="field-hint">
                      <span>
                        {{
                          isCustomProduct
                            ? isListing ? '上传后自动选中，也可从数据库选择已有图片。' : '选择 1–9 张发布图片。'
                            : '最多 9 张。留空时自动使用所选资料的关联图片，并在运行中分类排序。'
                        }}
                      </span>
                      <ElButton
                        v-if="selectedProduct && !isCustomProduct"
                        link
                        type="primary"
                        @click="useProductImages"
                      >
                        选用资料关联图片
                      </ElButton>
                    </div>
                  </div>
                </ElFormItem>
              </template>
              <template v-if="includesXianyu">
                <div class="form-columns">
                  <ElFormItem label="商品售价（元）" required>
                    <ElInputNumber
                      :key="String(formDisabled)"
                      v-model="formPrice"
                      :min="0"
                      :precision="2"
                      :controls="false"
                    />
                  </ElFormItem>
                  <ElFormItem label="运费（元，0 为包邮）">
                    <ElInputNumber
                      :key="String(formDisabled)"
                      v-model="form.shipping_fee"
                      :min="0"
                      :precision="2"
                      :controls="false"
                    />
                  </ElFormItem>
                  <ElFormItem label="商品成色" required>
                    <ElSelect v-model="form.condition" filterable allow-create>
                      <ElOption
                        v-for="condition in ['全新', '几乎全新', '轻微使用痕迹', '明显使用痕迹']"
                        :key="condition"
                        :label="condition"
                        :value="condition"
                      />
                    </ElSelect>
                  </ElFormItem>
                  <ElFormItem label="发货地" required>
                    <ElInput v-model="form.location" placeholder="与闲鱼网页可选地点一致的完整名称" maxlength="200" />
                  </ElFormItem>
                </div>
                <ElFormItem :label="isListing ? '商品分类' : '内容分类'">
                  <ElInput
                    v-model="form.category"
                    :placeholder="isListing && isCustomProduct && form.copy_mode === 'manual' ? '填写闲鱼可选分类，例如其他闲置（需符合实际商品）' : '留空由 AI 根据商品资料建议分类'"
                    maxlength="120"
                  />
                </ElFormItem>
              </template>
              <template v-if="requiresVideo && !form.approved_content_id">
                <ElFormItem label="宣传图片">
                  <ElRadioGroup v-model="form.image_mode">
                    <ElRadio value="existing">使用所选商品原图</ElRadio>
                    <ElRadio value="generate">AI 根据原图生成场景图</ElRadio>
                  </ElRadioGroup>
                </ElFormItem>
                <ElFormItem v-if="form.image_mode === 'generate'" label="生成张数">
                  <ElInputNumber v-model="form.generated_image_count" :min="1" :max="9" />
                </ElFormItem>
                <ElFormItem label="视频来源">
                  <ElRadioGroup v-model="form.video_mode">
                    <ElRadio value="slideshow">图片自动合成视频</ElRadio>
                    <ElRadio value="existing">数据库已有视频</ElRadio>
                  </ElRadioGroup>
                </ElFormItem>
                <ElFormItem v-if="form.video_mode === 'existing'" label="发布视频" required>
                  <ContentResourcePicker
                    v-model="selectedVideoIds"
                    :resources="resources"
                    :kinds="['video']"
                    :multiple="false"
                    :max-selections="1"
                    title="选择宣传视频"
                    button-text="从数据库选择视频"
                    :disabled="saving || !canManage || !!pendingRequest"
                  />
                </ElFormItem>
                <p v-else class="field-hint video-note">
                  使用所选图片制作 9:16 竖屏图文视频，静音带字幕，并自动填写发布标题与文案。
                </p>
              </template>
            </template>
          </div>
          <div v-show="activeStep === 2" class="form-step">
            <div class="step-heading"><h3>{{ flowSteps[2] }}</h3><p>{{ isManagement ? '核对本次操作，准备好后即可运行。' : form.approved_content_id ? '内容已经人工审核，核对目标账号和发布信息后即可上传。' : isListing && isCustomProduct && form.copy_mode === 'manual' ? '核对商品资料和发布账号，准备好后即可上架。' : '告诉 AI 表达重点，并选择生成草稿或直接发布。' }}</p></div>
            <dl class="workflow-summary">
              <div><dt>工作流</dt><dd>{{ form.name || '未命名' }}</dd></div>
              <div><dt>发布平台</dt><dd>{{ form.platforms.map(platformName).join(' / ') || '未选择' }}</dd></div>
              <div><dt>执行方式</dt><dd>{{ executionLabel(form) }}{{ isAndroid ? ` · ${deviceLabel(form)}` : '' }}</dd></div>
              <div><dt>{{ isManagement ? '执行操作' : '资料来源' }}</dt><dd>{{ isManagement ? operationLabel(form.operation) : isCustomProduct ? form.custom_product_name || '自定义资料' : selectedProduct?.name || `已选 ${form.image_ids.length} 张图片` }}</dd></div>
            </dl>
            <template v-if="!isManagement">
              <ElFormItem
                v-if="!form.approved_content_id && (!isListing || !isCustomProduct || form.copy_mode !== 'manual')"
                :label="
                  !isListing ? '宣传文案要求' : isCustomProduct ? 'AI 文案要求' : '商品事实与文案要求'
                "
              >
                <ElInput
                  v-model="form.instructions"
                  type="textarea"
                  :rows="4"
                  maxlength="4000"
                  show-word-limit
                  :placeholder="
                    !isListing
                      ? '填写目标受众、传播重点、品牌语气、发布场景或希望保留的关键词。'
                      : isCustomProduct
                        ? '填写文案风格、表达重点或希望保留的关键词；商品事实请填写在上方描述中。'
                        : '补充商品规格、实际使用情况、适合人群，以及文案风格。仅选图片时，请补充图片无法确认的商品信息。'
                  "
                />
              </ElFormItem>
              <p v-if="includesXianyu" class="field-hint video-note">
                闲鱼将标题与正文合并填写到「宝贝描述」（最多 1,500 字）。
              </p>
              <ElFormItem class="publish-rule" label="发布方式">
                <ElSwitch
                  v-model="form.auto_publish"
                  active-text="按规则直接发布"
                  :inactive-text="form.approved_content_id ? '先核对发布信息' : '生成后人工审核'"
                />
                <p class="field-hint">
                  {{
                    form.approved_content_id ? (form.auto_publish ? '将已审核的文案和素材上传到所选账号；不会重新生成内容。' : '读取已审核内容，核对平台及商品交易信息后发布。') : isAndroid && form.auto_publish
                      ? '直接发布规则可以先保存；设备和 App 发布适配器尚未就绪时不能运行。'
                      : form.auto_publish
                        ? isListing && isCustomProduct && form.copy_mode === 'manual' ? '直接上传所选图片和填写的商品文案，核对分类、价格、运费与地点后提交。' : '运行后会自动生成内容并提交到所选平台账号，结果以平台回执为准。'
                        : isListing
                          ? isCustomProduct && form.copy_mode === 'manual' ? '保存已填写的商品文案和图片顺序，可检查后再上架。' : '完成图片分类与商品文案后暂停，可编辑文案再上架商品。'
                          : '完成图片分类、宣传文案与视频后暂停，可编辑文案再发布视频。'
                  }}
                </p>
              </ElFormItem>
            </template>
            <p v-else class="field-hint">{{ form.target_title }}<br>{{ form.target_url }}</p>
          </div>
        </ElForm>
        <ElAlert
          v-if="activeStep === 2 && startBlock"
          :title="startBlock"
          type="warning"
          show-icon
          :closable="false"
        />
        <ElAlert
          v-if="formError"
          :title="formError"
          type="error"
          show-icon
          :closable="false"
        />
        <ElAlert
          v-if="pendingRequest"
          title="本次提交尚未收到结果。重试会使用同一请求编号，避免重复创建发布任务。"
          type="warning"
          show-icon
          :closable="false"
        />
        <div class="form-actions">
          <span class="step-count">第 {{ activeStep + 1 }} / 3 步</span>
          <div class="step-actions">
            <ElButton v-if="activeStep > 0" :disabled="saving || !!pendingRequest" @click="changeStep(activeStep - 1)">上一步</ElButton>
            <ElButton
              v-if="activeStep < 2"
              type="primary"
              :disabled="saving || !canManage || !!pendingRequest"
              @click="nextStep"
            >
              下一步
            </ElButton>
            <template v-else>
              <ElButton :disabled="!canManage || !!pendingRequest" :loading="saving" @click="saveWorkflow">保存工作流</ElButton>
              <ElButton
                type="primary"
                :disabled="!canManage || (!pendingRequest && !!startBlock)"
                :loading="saving"
                @click="startRun"
              >
                {{ runButtonText }}
              </ElButton>
            </template>
          </div>
        </div>
      </section>
    </div>

    <details v-if="activeWorkspace === 'runs' && (legacyWorkflows.length || legacyRuns.length)" class="panel legacy-records">
      <summary>
        旧版跨业务记录（只读） · {{ legacyWorkflows.length }} 个工作流 /
        {{ legacyRuns.length }} 次运行
      </summary>
      <p class="field-hint">
        旧版同时包含商品上架与内容宣传的配置需要拆分。请分别在两个业务中新建工作流；以下记录仅供追溯，不提供执行、重试或发布。
      </p>
      <ul>
        <li v-for="workflow in legacyWorkflows" :key="workflow.id">
          {{ workflow.name }} · {{ workflow.config.platforms.map(platformName).join(' / ') }}
        </li>
      </ul>
      <details v-for="run in legacyRuns" :key="run.id" class="legacy-run">
        <summary>
          {{ run.config.name }} · {{ stateLabel(run.status) }} · {{ formatDate(run.created_at) }}
        </summary>
        <p>{{ run.draft.title }}</p>
        <p class="legacy-copy">{{ run.draft.description }}</p>
        <p v-if="run.error">{{ run.error }}</p>
        <div
          v-for="(result, platform) in run.platform_results"
          :key="platform"
          class="legacy-receipt"
        >
          <p>{{ platformName(platform) }}：{{ stateLabel(result?.status || 'pending') }}</p>
          <p v-if="result?.error">{{ result.error }}</p>
          <a
            v-if="receiptUrl(result?.receipt?.url)"
            :href="receiptUrl(result?.receipt?.url)"
            target="_blank"
            rel="noopener noreferrer"
          >
            查看历史平台回执 ↗
          </a>
        </div>
        <ol v-if="run.events?.length" class="event-list">
          <li v-for="event in run.events" :key="event.id">
            <time>{{ formatDate(event.created_at) }}</time>
            <span>{{ event.message }}</span>
          </li>
        </ol>
      </details>
    </details>

    <section
      v-show="activeWorkspace === 'runs'"
      id="workflow-runs"
      class="panel runs-panel"
      aria-labelledby="runs-title"
    >
      <div class="section-heading">
        <h2 id="runs-title">{{ businessTitle }}运行记录</h2>
        <span class="muted">{{ businessRuns.length }} 次运行 · 执行期间自动刷新</span>
      </div>
      <ElEmpty
        v-if="!businessRuns.length"
        description="提交工作流后，在这里查看内容、执行步骤与发布结果"
      />
      <div v-else class="runs-grid">
        <div class="run-list" aria-label="选择运行记录">
          <button
            v-for="run in businessRuns"
            :key="run.id"
            type="button"
            class="run-item"
            :class="{ selected: selectedRun?.id === run.id }"
            :aria-pressed="selectedRun?.id === run.id"
            @click="selectRun(run)"
          >
            <div class="section-heading">
              <strong>
                {{
                  run.config.name
                    || workflows.find(item => item.id === run.workflow_id)?.name
                    || '自动化运行'
                }}
              </strong>
              <ElTag size="small" :type="stateType(run.status)">
                {{ stateLabel(run.status, run.config.operation) }}
              </ElTag>
            </div>
            <span>{{ formatDate(run.created_at) }}</span>
            <small>{{ executionLabel(run.config) }}<template v-if="deviceLabel(run.config)"> · {{ deviceLabel(run.config) }}</template></small>
            <small>{{ operationLabel(run.config.operation) }} · {{ run.id.slice(0, 12) }}</small>
          </button>
        </div>
        <article v-if="selectedRun" class="run-detail" aria-live="polite">
          <div class="section-heading">
            <div>
              <h3>运行详情</h3>
              <small class="muted">{{ selectedRun.id }}</small>
            </div>
            <div class="inline-actions">
              <ElButton
                v-if="mayRetry"
                :loading="actionLoading"
                :disabled="!canManage || !!retryBlock"
                @click="runAction('retry')"
              >
                重试未完成的平台
              </ElButton>
              <ElButton
                v-if="runBusy || selectedRun.status === 'ready'"
                :loading="actionLoading"
                :disabled="!canManage"
                @click="runAction('cancel')"
              >
                停止后续步骤
              </ElButton>
            </div>
          </div>
          <p class="field-hint">执行方式：{{ executionLabel(selectedRun.config) }}<template v-if="deviceLabel(selectedRun.config)"> · 设备：{{ deviceLabel(selectedRun.config) }}</template></p>
          <ElAlert
            v-if="(mayEditDraft || mayRetry) && runPublishBlock"
            :title="runPublishBlock"
            type="warning"
            show-icon
            :closable="false"
          />
          <ElAlert
            v-if="runError || selectedRun.error"
            :title="runError || selectedRun.error || ''"
            type="error"
            show-icon
            :closable="false"
          />
          <ElAlert
            v-if="resultUncertain"
            title="平台可能已收到执行请求，请先在平台核实结果。本次运行不提供自动重试，避免重复操作。"
            type="warning"
            show-icon
            :closable="false"
          />
          <div class="platform-results">
            <article
              v-for="platform in selectedRun.config.platforms"
              :key="platform"
              class="platform-result"
            >
              <div class="section-heading">
                <strong>{{ platformName(platform) }}</strong>
                <ElTag
                  :type="stateType(selectedRun.platform_results?.[platform]?.status || 'pending')"
                >
                  {{
                    stateLabel(
                      selectedRun.platform_results?.[platform]?.status || 'pending',
                      selectedRun.config.operation,
                    )
                  }}
                </ElTag>
              </div>
              <p v-if="selectedRun.platform_results?.[platform]?.error" class="result-error">
                {{ selectedRun.platform_results[platform]?.error }}
              </p>
              <span v-if="selectedRun.platform_results?.[platform]?.receipt?.account_label">
                账号：{{ selectedRun.platform_results[platform]?.receipt?.account_label }}
              </span>
              <span v-if="selectedRun.platform_results?.[platform]?.receipt?.external_id">
                平台记录：{{ selectedRun.platform_results[platform]?.receipt?.external_id }}
              </span>
              <span v-if="selectedRun.platform_results?.[platform]?.receipt?.status">
                平台回执：{{
                  stateLabel(
                    selectedRun.platform_results[platform]?.receipt?.status || '',
                    selectedRun.config.operation,
                  )
                }}
              </span>
              <a
                v-if="receiptUrl(selectedRun.platform_results?.[platform]?.receipt?.url)"
                :href="receiptUrl(selectedRun.platform_results?.[platform]?.receipt?.url)"
                target="_blank"
                rel="noopener noreferrer"
              >
                查看平台回执 ↗
              </a>
            </article>
          </div>
          <div v-if="runIsManagement" class="copy-preview">
            <h3>{{ operationLabel(selectedRun.config.operation) }}</h3>
            <strong>{{ selectedRun.config.target_title }}</strong>
            <span class="muted">账号标识：{{ selectedRun.config.accounts?.xianyu }}</span>
            <a
              v-if="receiptUrl(selectedRun.config.target_url || '')"
              :href="receiptUrl(selectedRun.config.target_url || '')"
              target="_blank"
              rel="noopener noreferrer"
            >
              查看指定的闲鱼商品 ↗
            </a>
          </div>
          <template
            v-if="!runIsManagement && (selectedRun.draft?.title || selectedRun.draft?.description)"
          >
            <div class="section-heading">
              <h3>{{ isListing ? '商品上架文案' : '内容宣传文案' }}</h3>
              <ElTag v-if="selectedRun.status === 'ready'" :type="selectedRun.draft.review?.state === 'approved' ? 'success' : 'warning'">
                第 {{ selectedRun.draft.review?.revision || 1 }} 版 · {{ selectedRun.draft.review?.state === 'approved' ? '审核通过' : '待人工审核' }}
              </ElTag>
            </div>
            <p v-if="selectedRun.config.platforms.includes('xianyu')" class="field-hint">
              闲鱼将以下标题与正文合并填写到「宝贝描述」（最多 1,500 字）。
            </p>
            <ElForm v-if="mayEditDraft" label-position="top" :disabled="actionLoading">
              <ElFormItem label="标题">
                <ElInput v-model="draft.title" :maxlength="draftLimits.title" show-word-limit />
              </ElFormItem>
              <ElFormItem label="文案">
                <ElInput
                  v-model="draft.description"
                  type="textarea"
                  :rows="5"
                  :maxlength="draftLimits.description"
                  show-word-limit
                />
              </ElFormItem>
              <p
                v-if="!isListing && (selectedRun.draft.has_video || selectedRun.draft.video_ready)"
                class="field-hint video-note"
              >
                {{ selectedRun.draft.video_stale ? '文案已修改，请选择「更新视频字幕」重新合成并预览后审核。' : '修改宣传文案后，可同步更新视频字幕。' }}
              </p>
              <div class="form-columns">
                <ElFormItem :label="isListing ? '商品分类' : '内容分类'">
                  <ElInput v-model="draft.category" maxlength="64" />
                </ElFormItem>
                <ElFormItem
                  v-if="selectedRun.config.platforms.includes('xianyu')"
                  label="售价（元）"
                >
                  <ElInputNumber
                    v-model="draftPrice"
                    :min="0"
                    :precision="2"
                    :controls="false"
                  />
                </ElFormItem>
              </div>
              <div class="inline-actions">
                <ElButton :loading="actionLoading" @click="saveDraft">保存文案</ElButton>
                <ElButton :loading="actionLoading" :disabled="!!selectedRun.draft.video_stale || !androidDraftSaved" @click="reviewAction('approve')">审核通过此版本</ElButton>
                <ElButton :loading="actionLoading" :disabled="selectedRun.draft.review?.state !== 'approved' || !androidDraftSaved || !!selectedRun.draft.library_ids?.length" @click="reviewAction('archive')">{{ selectedRun.draft.library_ids?.length ? '已保存到资料库' : '保存审核素材到资料库' }}</ElButton>
                <template v-if="isListing && runUsesCurrentMode">
                  <ElButton :disabled="actionLoading" @click="copyListingDraft">复制商品文案</ElButton>
                  <ElButton :loading="actionLoading" @click="openListingPage">打开闲鱼发布页</ElButton>
                </template>
                <ElButton
                  v-if="executionMode(selectedRun.config) !== 'android'"
                  type="primary"
                  :loading="actionLoading"
                  :disabled="!!runPublishBlock || !!selectedRun.draft.video_stale || !androidDraftSaved || (!!selectedRun.config.require_review && selectedRun.draft.review?.state !== 'approved')"
                  @click="runAction('publish')"
                >
                  {{ publishActionText(selectedRun.config.platforms, '上传并') }}
                </ElButton>
              </div>
              <ElFormItem label="修改意见" style="margin-top: 20px">
                <ElInput
                  v-model="reviewFeedback"
                  type="textarea"
                  :rows="2"
                  maxlength="2000"
                  placeholder="例如：文案更简短，保留真实规格；场景改为明亮的客厅。"
                />
              </ElFormItem>
              <div class="inline-actions">
                <ElSelect v-model="revisionScope" style="width: 180px" aria-label="修改范围">
                  <ElOption label="修改文案" value="copy" />
                  <ElOption v-if="!isListing" label="重绘宣传图片" value="images" />
                  <ElOption v-if="selectedRun.config.video_mode === 'slideshow' && selectedRun.draft.has_video" label="更新视频字幕" value="video" />
                  <ElOption label="全部重新生成" value="all" />
                </ElSelect>
                <ElButton :loading="actionLoading" :disabled="!androidDraftSaved || (!reviewFeedback.trim() && revisionScope !== 'video')" @click="reviewAction('revise')">按意见修改并重新送审</ElButton>
              </div>
            </ElForm>
            <p v-if="mayEditDraft && runUsesCurrentMode && executionMode(selectedRun.config) === 'android' && !androidDraftSaved" class="field-hint">请先保存文案，再导入 App。</p>
            <ContentAndroidPanel
              v-if="mayEditDraft && runUsesCurrentMode && executionMode(selectedRun.config) === 'android'"
              :key="selectedRun.id"
              :run="selectedRun"
              :device="availableDevices.find(device => device.device_id === selectedRun?.config.device_id)"
              :enabled="canManage && !actionLoading && !!androidDraftSaved"
              @updated="inspectAndroid"
              @published="updateRun($event, true)"
            />
            <p v-if="mayEditDraft && !runUsesCurrentMode" class="field-hint">此历史任务使用旧执行方式。请打开工作流重新生成草稿，历史记录保留原样。</p>
            <div v-if="!mayEditDraft" class="copy-preview">
              <strong>{{ selectedRun.draft.title }}</strong>
              <p>{{ selectedRun.draft.description }}</p>
              <span class="muted">
                {{ selectedRun.draft.category || '未指定分类' }}
                <template v-if="isListing && selectedRun.draft.price != null">
                  · ¥{{ selectedRun.draft.price }}
                </template>
              </span>
            </div>
          </template>
          <div v-if="selectedRun.draft?.image_count" class="copy-preview">
            <div class="section-heading"><h3>本次上传图片</h3><ElButton :loading="previewLoading" @click="loadImages">预览全部图片</ElButton></div>
            <div class="draft-images">
              <img
                v-for="(url, index) in imagePreviews"
                :key="url"
                :src="url"
                :alt="`上传图片 ${index + 1}`"
              >
            </div>
          </div>
          <details v-if="selectedRun.draft?.history?.length" class="copy-preview">
            <summary>修改历史（{{ selectedRun.draft.history.length }} 个版本）</summary>
            <article v-for="item in selectedRun.draft.history" :key="item.review.revision">
              <strong>第 {{ item.review.revision }} 版 · {{ item.title }}</strong><p>{{ item.description }}</p><small>{{ item.review.feedback }}</small>
            </article>
          </details>
          <template v-if="!runIsManagement && selectedRun.draft?.classifications?.length">
            <h3>{{ selectedRun.draft?.model === 'manual' ? '商品图片顺序' : '图片自动分类' }}</h3>
            <div class="classification-list">
              <article
                v-for="classification in selectedRun.draft.classifications"
                :key="classification.id"
              >
                <ContentResourcePreview
                  v-if="sourceResource(classification.id)"
                  :item="sourceResource(classification.id)!"
                  :preview="false"
                  class="classification-preview"
                />
                <span v-else class="classification-preview muted">图片</span>
                <div>
                  <strong>{{ resourceName(classification.id) }}</strong>
                  <p>{{ classification.reason }}</p>
                </div>
                <ElTag effect="plain">
                  {{
                    classification.label || roleLabels[classification.role] || classification.role
                  }}
                </ElTag>
              </article>
            </div>
          </template>
          <div
            v-if="
              !isListing
                && !runIsManagement
                && (selectedRun.draft?.has_video || selectedRun.draft?.video_ready)
            "
            class="video-preview"
          >
            <ElButton v-if="!previewUrl" :loading="previewLoading" @click="loadPreview">
              预览发布视频
            </ElButton>
            <video
              v-else
              :src="previewUrl"
              controls
              preload="metadata"
            />
          </div>
          <template v-if="selectedRun.events?.length">
            <h3>执行步骤</h3>
            <ol class="event-list">
              <li v-for="event in selectedRun.events" :key="event.id">
                <time>{{ formatDate(event.created_at) }}</time>
                <span>
                  {{ event.platform ? `${platformName(event.platform)} · ` : ''
                  }}{{ event.message }}
                </span>
              </li>
            </ol>
          </template>
        </article>
      </div>
    </section>
  </section>
</template>

<style scoped>
.approved-source {
  padding: 20px;
  margin: 0 0 24px;
  border: 1px solid #cbd5e1;
  border-radius: 12px;
  background: #f8fafc;
}
.approved-source p {
  white-space: pre-wrap;
  line-height: 1.8;
  color: #475569;
}
.approved-source small {
  color: #64748b;
}
.automation-page {
  display: grid;
  gap: 20px;
}
.page-heading,
.section-heading,
.form-actions,
.inline-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.page-heading h1 {
  margin: 0;
  font-size: var(--xi-semantic-typography-heading1-font-size);
  font-weight: 600;
  line-height: 28px;
}
.page-heading p {
  margin: 2px 0 0;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 13px;
}
.panel {
  padding: 24px;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}
h2,
h3 {
  margin: 0;
  font-size: 17px;
}
h3 {
  font-size: 15px;
}
/* Same flat tabs as the workbench pages (.wb-tabs in catalog.css). */
.workspace-switcher {
  display: flex;
  align-items: center;
  gap: 2px;
  width: fit-content;
  max-width: 100%;
}
.workspace-switcher button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 12px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--xi-semantic-color-text-secondary);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition:
    color 180ms ease,
    background 180ms ease;
}
.workspace-switcher button:hover {
  background: var(--wb-hover);
}
.workspace-switcher button.active {
  background: var(--wb-selected);
  color: var(--xi-semantic-color-text-primary);
  font-weight: 600;
}
.workspace-switcher span {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  font-weight: 400;
}
.flow-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  padding: 0 0 20px;
  margin: 0 0 24px;
  list-style: none;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.flow-summary button {
  display: flex;
  gap: 10px;
  align-items: center;
  width: 100%;
  min-height: 44px;
  padding: 10px 12px;
  border: 0;
  border-radius: 8px;
  color: var(--el-text-color-secondary);
  background: transparent;
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition:
    color 180ms ease,
    background 180ms ease;
}
.flow-summary button:hover,
.flow-summary button.current {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.flow-summary button.current {
  font-weight: 600;
}
.flow-summary span {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--el-fill-color);
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 600;
}
.flow-summary .current span,
.flow-summary .completed span {
  color: var(--el-color-white);
  background: var(--el-color-primary);
}
.step-heading {
  margin-bottom: 22px;
}
.step-heading h3 {
  font-size: 16px;
}
.step-heading p {
  margin: 7px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}
.form-step {
  min-width: 0;
}
.form-step > .el-form-item :deep(.el-input:not(.el-textarea)),
.form-step > .el-form-item :deep(.el-select),
.form-step > .el-form-item :deep(.product-picker) {
  max-width: 520px;
}
.form-step :deep(.el-checkbox-group),
.form-step :deep(.el-radio-group) {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
}
.form-step :deep(.el-checkbox),
.form-step :deep(.el-radio) {
  margin-right: 0;
}
.workflow-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px 24px;
  margin: 0 0 24px;
  padding: 20px;
  background: var(--el-fill-color-light);
  border-radius: 10px;
}
.workflow-summary dt {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.workflow-summary dd {
  margin: 6px 0 0;
  line-height: 1.6;
  font-size: 14px;
  overflow-wrap: anywhere;
}
.step-actions {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
.step-actions .el-button {
  margin-left: 0;
}
.step-count {
  flex: none;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.form-columns,
.platform-results {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.android-connection {
  display: grid;
  gap: 12px;
  padding: 16px 0;
  margin-bottom: 20px;
  border-top: 1px solid var(--el-border-color-lighter);
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: transparent;
  border-radius: var(--xi-component-content-radius-control);
}
.android-connection .el-form-item {
  max-width: 520px;
  margin-bottom: 0;
}
.execution-notice {
  margin: -8px 0 20px;
}
.workspace-grid {
  display: grid;
  grid-template-columns: 228px minmax(0, 1fr);
  gap: 0;
  align-items: stretch;
  overflow: hidden;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
}
.workflow-sidebar {
  padding: 20px 14px;
  border: 0;
  border-radius: 0;
  border-right: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-extra-light);
}
.workflow-empty {
  margin: 24px 0;
  padding: 12px 4px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.8;
}
.workflow-empty span {
  font-size: 12px;
}
.workflow-sidebar h2 {
  font-size: 15px;
}
.workflow-sidebar > .section-heading {
  margin-bottom: 16px;
}
.workflow-item,
.run-item {
  display: grid;
  width: 100%;
  gap: 6px;
  padding: 14px;
  margin-top: 8px;
  color: inherit;
  text-align: left;
  font: inherit;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--xi-component-content-radius-control);
  cursor: pointer;
  overflow-wrap: anywhere;
}
.workflow-item span,
.workflow-item small,
.run-item > span,
.run-item > small,
.muted,
.field-hint {
  color: var(--xi-semantic-color-text-secondary);
  font-size: 12px;
}
.workflow-item.selected,
.run-item.selected {
  background: var(--xi-semantic-color-background-brand-subtle);
  border-color: var(--el-color-primary-light-7);
  box-shadow: inset 3px 0 0 var(--el-color-primary);
}
.workflow-item:focus-visible,
.run-item:focus-visible {
  outline: 2px solid var(--xi-semantic-color-border-brand-default);
  outline-offset: 2px;
}
.workflow-item:disabled {
  opacity: 0.6;
  cursor: default;
}
.workflow-editor {
  min-width: 0;
  border: 0;
  border-radius: 0;
}
.workflow-editor > .section-heading {
  margin-bottom: 20px;
}
.workflow-editor :deep(.el-select),
.workflow-editor :deep(.el-input-number),
.run-detail :deep(.el-input-number),
.full-width {
  width: 100%;
}
.local-image-upload {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.field-hint {
  margin: 8px 0 0;
  line-height: 1.7;
}
.video-note {
  margin: -12px 0 18px;
}
.publish-rule :deep(.el-form-item__content) {
  display: block;
}
.form-actions {
  justify-content: space-between;
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.workflow-editor > .el-alert {
  margin-top: 12px;
}
.runs-panel > .section-heading {
  margin-bottom: 20px;
}
.runs-grid {
  display: grid;
  grid-template-columns: 290px minmax(0, 1fr);
  gap: 24px;
}
.run-list {
  max-height: 900px;
  overflow: auto;
  padding: 2px;
}
.run-item:first-child {
  margin-top: 0;
}
.run-item .section-heading {
  align-items: flex-start;
}
.run-item strong {
  font-size: 13px;
}
.run-item .el-tag {
  flex: none;
}
.run-detail {
  display: grid;
  gap: 18px;
  min-width: 0;
  align-content: start;
}
.run-detail > .section-heading {
  align-items: flex-start;
}
.inline-actions {
  justify-content: flex-start;
  flex-wrap: wrap;
}
.platform-result,
.copy-preview {
  display: grid;
  gap: 10px;
  padding: 16px;
  background: var(--xi-semantic-color-background-page);
  border-radius: var(--xi-component-content-radius-control);
}
.platform-result p,
.copy-preview p {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.platform-result a {
  color: var(--xi-semantic-color-text-brand);
}
.result-error {
  color: var(--xi-semantic-color-feedback-danger-default);
  font-size: 12px;
}
.classification-list {
  display: grid;
  gap: 8px;
}
.classification-list article {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 10px;
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
}
.classification-preview {
  width: 64px;
  height: 64px;
  min-height: 64px;
}
.classification-list article > div:not(.classification-preview) {
  min-width: 0;
}
.classification-list strong {
  font-size: 13px;
  overflow-wrap: anywhere;
}
.classification-list p {
  margin: 4px 0 0;
  color: var(--xi-semantic-color-text-secondary);
  font-size: 12px;
}
.video-preview video {
  display: block;
  width: 100%;
  max-height: 360px;
  background: #161616;
  border-radius: var(--xi-component-content-radius-control);
}
.event-list {
  display: grid;
  gap: 12px;
  padding: 0;
  margin: 0;
  list-style: none;
}
.event-list li {
  display: grid;
  grid-template-columns: 145px minmax(0, 1fr);
  gap: 12px;
  font-size: 12px;
}
.event-list time {
  color: var(--xi-semantic-color-text-secondary);
}
@media (max-width: 1100px) {
  .workspace-grid {
    grid-template-columns: 210px minmax(0, 1fr);
  }
  .runs-grid {
    grid-template-columns: 240px minmax(0, 1fr);
  }
  .run-detail > .section-heading {
    flex-wrap: wrap;
  }
}
@media (max-width: 767px) {
  .workspace-grid,
  .runs-grid,
  .form-columns,
  .platform-results {
    grid-template-columns: minmax(0, 1fr);
  }
  .workflow-sidebar {
    border-right: 0;
    border-bottom: 1px solid var(--el-border-color-lighter);
    max-height: 250px;
    overflow-y: auto;
  }
  .flow-summary {
    gap: 4px;
  }
  .flow-summary button {
    flex-direction: column;
    gap: 6px;
    padding: 8px 4px;
    text-align: center;
    font-size: 12px;
  }
  .workspace-switcher button {
    padding: 8px 12px;
  }
  .workflow-summary {
    grid-template-columns: minmax(0, 1fr);
  }
  .panel {
    padding: 16px;
  }
  .page-heading {
    align-items: flex-start;
  }
  .page-heading h1 {
    font-size: 18px;
  }
  .run-list {
    max-height: 280px;
  }
  .classification-list article {
    grid-template-columns: 48px minmax(0, 1fr);
  }
  .classification-preview {
    width: 48px;
    height: 48px;
    min-height: 48px;
  }
  .classification-list article > .el-tag {
    grid-column: 2;
    justify-self: start;
  }
  .event-list li {
    grid-template-columns: minmax(0, 1fr);
    gap: 2px;
  }
  .form-actions {
    flex-wrap: wrap;
  }
}
@media (prefers-reduced-motion: reduce) {
  .workspace-switcher button,
  .flow-summary button {
    transition: none;
  }
}
.draft-images {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.draft-images img {
  width: 100%;
  max-height: 320px;
  object-fit: contain;
  border-radius: 10px;
  background: #f8fafc;
}
</style>
