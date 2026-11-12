<script setup lang="ts">
import type { AgentStep, ChatArtifact, ChatAttachment, ChatConversation, ChatConversationSummary, ChatMessage, ChatModelCatalog, ChatRouting } from '@/api/agent/chat'
import {
  ArrowUp,
  ChatDotRound,
  Close,
  CopyDocument,
  Delete,
  Document,
  Download,
  Loading,
  Menu,
  Microphone,
  Operation,
  Paperclip,
  Picture,
  Plus,
  Refresh,
  VideoCamera,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownIt from 'markdown-it'
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { chatApi, streamReply } from '@/api/agent/chat'
import { saveBlob } from '@/api/agent/http'
import AgentTrace from './AgentTrace.vue'
import { formatDate, parseContentDateTime } from './contentWorkflow'
import { useVoiceInput } from './voiceInput'

defineOptions({ name: 'ContentChatView' })

interface DraftFile {
  key: string
  file: File
  previewUrl: string
  progress: number
  status: 'uploading' | 'ready' | 'failed'
  attachment?: ChatAttachment
  error?: string
  removed?: boolean
}
interface Draft { text: string, files: DraftFile[], reference?: ChatArtifact }

const router = useRouter()
const route = useRoute()
const catalog = ref<ChatModelCatalog>({ models: [], max_attachment_bytes: 50 * 1024 * 1024, supported_extensions: [] })
const conversations = ref<ChatConversationSummary[]>([])
const historyTotal = ref(0)
const historyLoading = ref(false)
const current = ref<ChatConversation>()
const loading = ref(false)
const loadingConversation = ref(false)
const sending = ref(false)
const loadError = ref('')
const sendError = ref('')
const checkingSubmission = ref(false)
const retryErrors = ref<Record<string, string>>({})
const historyOpen = ref(false)
const search = ref('')
const dragDepth = ref(0)
const composing = ref(false)
const messageList = ref<HTMLElement>()
const composer = ref<HTMLTextAreaElement>()
const fileInput = ref<HTMLInputElement>()
const drafts = ref<Record<string, Draft>>({ new: { text: '', files: [] } })
const previewOpen = ref(false)
const previewBusy = ref(false)
const previewError = ref('')
const previewFile = ref<ChatAttachment | ChatArtifact>()
const previewUrl = ref('')
const previewIsArtifact = ref(false)
const artifactPreviewText = ref('')
const artifactMediaUrls = ref<Record<string, string>>({})
const artifactMediaErrors = ref<Record<string, string>>({})
const artifactMediaLoading = ref<Record<string, boolean>>({})
const artifactDownloadBusy = ref('')
const pollingError = ref('')
let ownedPreviewUrl = ''
let disposed = false
let viewActive = true
let firstActivation = true
let selectionSequence = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let streamController: AbortController | undefined
let streamingId = ''
const liveReplies = reactive(new Map<string, { text: string, stage: string, plan: string[], steps: AgentStep[] }>())
// Send to the operations-assistant Agent directly instead of letting the model choose.
const agentMode = ref(false)
// Voice input appends what is said to the text that was in the box when it started.
let voiceBase = ''
const {
  state: voiceState,
  seconds: voiceSeconds,
  active: voiceActive,
  supported: voiceSupported,
  toggle: toggleVoiceInput,
  cancel: cancelVoice,
} = useVoiceInput({
  onText: (text) => {
    draft.value.text = voiceBase + text
  },
  transcribe: () => (catalog.value.speech_input ? recording => chatApi.transcribe(recording) : undefined),
  notify: (level, message) => ElMessage[level](message),
})
const voiceLabel = computed(() => {
  if (voiceState.value === 'transcribing') {
    return '识别中…'
  }
  return voiceActive.value ? `停止（${voiceSeconds.value} 秒）` : '语音输入'
})

function toggleVoice() {
  if (!voiceActive.value) {
    voiceBase = draft.value.text
  }
  toggleVoiceInput()
  composer.value?.focus()
}
let pollSequence = 0
const objectUrls = new Set<string>()
const boundAttachments = new Set<string>()
const pendingDrafts = reactive(new Map<string, { draft: Draft, previousUserId?: string }>())
let previewSequence = 0
let followLatest = true
const contentObservers = new WeakMap<HTMLElement, ResizeObserver>()
const mediaObservers = new WeakMap<HTMLElement, IntersectionObserver>()
const mediaRequests = new Map<string, Promise<void>>()
const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content || '图片')
const defaultLinkOpen = markdown.renderer.rules.link_open
markdown.renderer.rules.link_open = (tokens, index, options, environment, renderer) => {
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen
    ? defaultLinkOpen(tokens, index, options, environment, renderer)
    : renderer.renderToken(tokens, index, options)
}

// CommonMark does not close "**下一步：**请" (punctuation before the closing marker and
// a CJK character after it), so move such trailing punctuation outside the bold text.
function renderMarkdown(text: string) {
  return markdown.render(text.replace(/\*\*([^*\n]+?)([：:，,。！!？?；;、])\*\*/gu, '**$1**$2'))
}

const draftKey = computed(() => current.value?.id || 'new')
const draft = computed(() => drafts.value[draftKey.value])
const historyItems = computed(() => conversations.value.filter(item => item.title.toLowerCase().includes(search.value.toLowerCase())))
const busy = computed(() => sending.value || loadingConversation.value)
const uploading = computed(() => draft.value.files.some(item => item.status === 'uploading'))
const submissionUncertain = computed(() => !sending.value && Boolean(current.value && pendingDrafts.has(current.value.id)))
const canSend = computed(() => !busy.value && !loading.value && !uploading.value
  && !checkingSubmission.value && !submissionUncertain.value
  && !remotePending.value
  && !draft.value.files.some(item => item.status === 'failed')
  && Boolean(draft.value.text.trim() || draft.value.files.length || draft.value.reference))
const acceptFiles = computed(() => (catalog.value.supported_extensions.length
  ? catalog.value.supported_extensions.map(extension => extension.startsWith('.') ? extension : `.${extension}`)
  : ['.txt', '.md', '.markdown', '.doc', '.docx', '.pdf', '.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.m4v', '.webm', '.mkv']).join(','))
const lastAssistant = computed(() => [...(current.value?.messages || [])].reverse().find(item => item.role === 'assistant'))
const lastUser = computed(() => [...(current.value?.messages || [])].reverse().find(item => item.role === 'user'))
const remotePending = computed(() => Boolean(current.value?.busy || lastAssistant.value?.status === 'pending'))
const sendErrorHint = computed(() => submissionUncertain.value
  ? '接收状态尚未确认，输入已保留。请先刷新对话确认结果，避免重复提交。'
  : draft.value.reference || draft.value.files.length
    ? '输入和参考资料已保留；请按提示检查或重新上传文件，再点击发送。'
    : '输入已保留；请按提示调整后再点击发送。')
const examples = ['工作台现在有多少待审核的内容？', '给名称含“雅集”的商品生成宣传文案', '帮我梳理这份资料的重点', '根据产品特点写一段推广文案']

function artifactReady(artifact: ChatArtifact) {
  return artifact.status === 'ready' || artifact.status === 'completed'
}

function errorMessage(error: unknown, fallback = '操作失败，请稍后重试') {
  return error instanceof Error ? error.message : fallback
}

function temporaryId() {
  return typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function kindOf(item: DraftFile) {
  if (item.attachment) {
    return item.attachment.kind
  }
  const extension = item.file.name.split('.').pop()?.toLowerCase() || ''
  if (item.file.type.startsWith('image/') || ['jpg', 'jpeg', 'png', 'webp'].includes(extension)) {
    return 'image'
  }
  return item.file.type.startsWith('video/') || ['mp4', 'mov', 'm4v', 'webm', 'mkv'].includes(extension) ? 'video' : 'document'
}

function fileIcon(kind: string) {
  return kind === 'image' ? Picture : kind === 'video' ? VideoCamera : Document
}

function fileSize(size: number) {
  // eslint-disable-next-line financial/no-division -- Byte size conversion, not financial arithmetic.
  return size < 1024 * 1024 ? `${Math.max(1, Math.round(size / 1024))} KB` : `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function modelName(id: string) {
  if (id === 'document-local') {
    return '文档转换'
  }
  const model = catalog.value.models.find(item => item.id === id || item.model === id)
  const name = model?.name.trim() || ''
  // Provider service IDs and UUIDs are not meaningful names for a chat speaker.
  return name && !/^(?:[a-z0-9]{16,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i.test(name) ? name : 'AI 助手'
}

async function scrollToBottom() {
  followLatest = true
  await nextTick()
  messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'auto' })
}

function trackMessageScroll() {
  const list = messageList.value
  if (list && !followLatest) {
    followLatest = list.scrollHeight - list.scrollTop - list.clientHeight < 4
  }
}

function handleMessageWheel(event: WheelEvent) {
  if (event.deltaY < 0) {
    followLatest = false
  }
}

function handleMessagePointer(event: PointerEvent) {
  if (event.pointerType === 'touch' || event.target === messageList.value) {
    followLatest = false
  }
}

function handleMessageKey(event: KeyboardEvent) {
  if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) {
    followLatest = false
  }
}

function followMediaResize() {
  if (followLatest && viewActive && !disposed) {
    void scrollToBottom()
  }
}

const vFollowLatest = {
  mounted(element: HTMLElement) {
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(followMediaResize)
      observer.observe(element)
      contentObservers.set(element, observer)
    }
  },
  unmounted(element: HTMLElement) {
    contentObservers.get(element)?.disconnect()
    contentObservers.delete(element)
  },
}

async function refreshHistory() {
  const result = await chatApi.conversations()
  conversations.value = result.items
  historyTotal.value = result.total
}

async function loadMoreHistory() {
  if (historyLoading.value) {
    return
  }
  historyLoading.value = true
  try {
    const result = await chatApi.conversations(conversations.value.length)
    const existingIds = new Set(conversations.value.map(item => item.id))
    conversations.value.push(...result.items.filter(item => !existingIds.has(item.id)))
    historyTotal.value = result.total
  }
  catch (error) {
    ElMessage.error(errorMessage(error, '对话历史读取失败'))
  }
  finally {
    historyLoading.value = false
  }
}

function revokeUrl(url: string) {
  if (objectUrls.delete(url)) {
    URL.revokeObjectURL(url)
  }
}

function createObjectUrl(file: Blob) {
  const url = URL.createObjectURL(file)
  objectUrls.add(url)
  return url
}

function clearDraft(key: string) {
  drafts.value[key]?.files.forEach((item) => {
    item.removed = true
    revokeUrl(item.previewUrl)
    cleanupAttachment(item.attachment)
  })
  drafts.value[key] = { text: '', files: [] }
  pendingDrafts.delete(key)
}

function cleanupAttachment(attachment?: ChatAttachment) {
  if (attachment && !boundAttachments.has(attachment.id)) {
    void chatApi.deleteAttachment(attachment.id).catch(() => {})
  }
}

function consumeSubmittedDraft(value: ChatConversation) {
  const submitted = pendingDrafts.get(value.id)
  const user = [...value.messages].reverse().find(message => message.role === 'user')
  if (!submitted || !user || user.id === submitted.previousUserId) {
    return false
  }
  const sent = submitted.draft
  const expectedContent = sent.text.trim() || (sent.reference ? '请分析参考文件' : '')
  const fileIds = sent.files.flatMap(item => item.attachment ? [item.attachment.id] : []).sort()
  if (user.content.trim() !== expectedContent
    || JSON.stringify(user.attachments.map(item => item.id).sort()) !== JSON.stringify(fileIds)) {
    return false
  }
  const activeDraft = drafts.value[value.id]
  if (activeDraft) {
    // Consume only this submission, retaining text and attachments composed later.
    if (activeDraft.text === sent.text) {
      activeDraft.text = ''
    }
    const sentKeys = new Set(sent.files.map(item => item.key))
    activeDraft.files = activeDraft.files.filter(item => !sentKeys.has(item.key))
    sent.files.forEach((item) => {
      item.removed = true
      revokeUrl(item.previewUrl)
    })
    if (activeDraft.reference?.id === sent.reference?.id) {
      activeDraft.reference = undefined
    }
  }
  pendingDrafts.delete(value.id)
  sendError.value = ''
  return true
}

function adoptConversation(value: ChatConversation) {
  drafts.value[value.id] ||= { text: '', files: [] }
  current.value = value
  value.messages.forEach((message) => {
    message.attachments?.forEach(attachment => boundAttachments.add(attachment.id))
    if (message.status !== 'pending') {
      liveReplies.delete(message.id)
    }
  })
  const accepted = consumeSubmittedDraft(value)
  schedulePolling()
  followReply()
  return accepted
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = undefined
  }
  pollSequence++
  stopStreaming()
}

function stopStreaming() {
  streamController?.abort()
  streamController = undefined
  streamingId = ''
}

// Show the reply while it is written. Polling still delivers the saved result,
// so a missed or interrupted stream only loses the preview, never the answer.
function followReply() {
  const conversation = current.value
  const reply = lastAssistant.value
  if (!conversation || !reply || reply.status !== 'pending' || streamingId === reply.id || disposed || !viewActive) {
    return
  }
  streamController?.abort()
  const controller = new AbortController()
  streamController = controller
  streamingId = reply.id
  liveReplies.set(reply.id, { text: '', stage: '', plan: [], steps: [] })
  void streamReply(conversation.id, reply.id, (event) => {
    const state = liveReplies.get(reply.id)
    if (!state) {
      return
    }
    clock.value = Date.now()
    if (event.type === 'stage') {
      state.stage = event.label
    }
    else if (event.type === 'plan') {
      state.plan = event.items
    }
    else if (event.type === 'step') {
      const index = state.steps.findIndex(step => step.index === event.step.index)
      state.steps = index < 0
        ? [...state.steps, event.step]
        : state.steps.map((step, position) => position === index ? event.step : step)
    }
    else if (event.type === 'delta') {
      const nearBottom = messageList.value
        ? messageList.value.scrollHeight - messageList.value.scrollTop - messageList.value.clientHeight < 160
        : true
      state.text += event.text
      state.stage = '正在输出回复…'
      if (nearBottom) {
        void scrollToBottom()
      }
    }
    else if (event.type === 'reset') {
      state.text = ''
    }
  }, controller.signal).then(() => {
    if (streamController !== controller) {
      return
    }
    streamController = undefined
    // The reply is saved before the stream ends: fetch it now instead of waiting.
    if (current.value?.id === conversation.id && pollTimer) {
      clearTimeout(pollTimer)
      pollTimer = undefined
      schedulePolling(0)
    }
  }, () => {
    if (streamController === controller) {
      streamController = undefined
    }
  })
}

const toolLabels: Record<ChatRouting['tool'], string> = {
  answer_in_chat: '文字回答',
  create_document: '生成文档',
  generate_image: '生成图片',
  generate_video: '生成视频',
  operate_workbench: '运营助手',
}

function routingOf(message: ChatMessage) {
  const messages = current.value?.messages || []
  const index = messages.indexOf(message)
  return index > 0 && messages[index - 1].role === 'user' ? messages[index - 1].routing : undefined
}

function routingLabel(routing: ChatRouting) {
  return `${routing.method === 'function_calling' ? '模型选择' : '规则匹配'}：${toolLabels[routing.tool]}`
}

// Refreshed by each status poll and stream event, so a slow model never looks frozen.
const clock = ref(Date.now())

function waitedSeconds(message: ChatMessage) {
  const started = parseContentDateTime(message.created_at)
  if (!started) {
    return 0
  }
  // Whole seconds read from a UTC clock face (integer arithmetic only).
  const waited = new Date(Math.max(0, clock.value - started.getTime()))
  return waited.getUTCHours() * 3600 + waited.getUTCMinutes() * 60 + waited.getUTCSeconds()
}

function schedulePolling(delay = 2500) {
  if (disposed || !viewActive || !remotePending.value || pollTimer || !current.value) {
    return
  }
  const id = current.value.id
  const sequence = pollSequence
  pollTimer = setTimeout(async () => {
    pollTimer = undefined
    clock.value = Date.now()
    const nearBottom = messageList.value
      ? messageList.value.scrollHeight - messageList.value.scrollTop - messageList.value.clientHeight < 160
      : true
    try {
      const conversation = await chatApi.conversation(id)
      if (disposed || !viewActive || sequence !== pollSequence || current.value?.id !== id) {
        return
      }
      pollingError.value = ''
      adoptConversation(conversation)
      if (!remotePending.value) {
        await refreshHistory().catch(() => {})
      }
      if (nearBottom) {
        await scrollToBottom()
      }
    }
    catch (error) {
      if (disposed || sequence !== pollSequence || current.value?.id !== id) {
        return
      }
      pollingError.value = errorMessage(error, '生成状态暂时无法读取，系统会继续尝试')
      schedulePolling(8000)
    }
  }, delay)
}

async function loadArtifactMedia(artifact: ChatArtifact) {
  if (!artifactReady(artifact) || !['image', 'video'].includes(artifact.kind) || artifactMediaUrls.value[artifact.id]) {
    return
  }
  const existing = mediaRequests.get(artifact.id)
  if (existing) {
    return existing
  }
  artifactMediaLoading.value[artifact.id] = true
  delete artifactMediaErrors.value[artifact.id]
  const request = (async () => {
    try {
      const blob = await chatApi.artifact(artifact.id)
      if (!disposed) {
        artifactMediaUrls.value[artifact.id] = createObjectUrl(blob)
      }
    }
    catch (error) {
      artifactMediaErrors.value[artifact.id] = errorMessage(error, '产物预览暂时不可用')
    }
    finally {
      artifactMediaLoading.value[artifact.id] = false
      mediaRequests.delete(artifact.id)
    }
  })()
  mediaRequests.set(artifact.id, request)
  return request
}

const vArtifactMedia = {
  mounted(element: HTMLElement, binding: { value: ChatArtifact }) {
    const observer = new IntersectionObserver((entries) => {
      if (entries.some(entry => entry.isIntersecting)) {
        void loadArtifactMedia(binding.value)
        observer.disconnect()
      }
    }, { rootMargin: '160px' })
    observer.observe(element)
    mediaObservers.set(element, observer)
  },
  unmounted(element: HTMLElement) {
    mediaObservers.get(element)?.disconnect()
    mediaObservers.delete(element)
  },
}

async function useArtifact(artifact: ChatArtifact) {
  if (!artifactReady(artifact) || busy.value) {
    return
  }
  draft.value.reference = artifact
  await nextTick()
  composer.value?.focus()
}

async function previewArtifact(artifact: ChatArtifact, message?: ChatMessage) {
  if (!artifactReady(artifact)) {
    return
  }
  closePreview()
  const sequence = previewSequence
  previewIsArtifact.value = true
  previewFile.value = artifact
  artifactPreviewText.value = artifact.text_preview || message?.content || ''
  previewOpen.value = true
  previewError.value = ''
  if (artifact.kind === 'docx' || artifact.kind === 'doc') {
    return
  }
  previewBusy.value = true
  try {
    if (['image', 'video'].includes(artifact.kind)) {
      await loadArtifactMedia(artifact)
      if (sequence !== previewSequence || !previewOpen.value || previewFile.value?.id !== artifact.id) {
        return
      }
      previewUrl.value = artifactMediaUrls.value[artifact.id] || ''
      previewError.value = artifactMediaErrors.value[artifact.id] || ''
    }
    else {
      const blob = await chatApi.artifact(artifact.id)
      if (!disposed && sequence === previewSequence && previewOpen.value && previewFile.value?.id === artifact.id) {
        ownedPreviewUrl = createObjectUrl(blob)
        previewUrl.value = ownedPreviewUrl
      }
    }
  }
  catch (error) {
    if (sequence === previewSequence) {
      previewError.value = errorMessage(error, '产物预览读取失败')
    }
  }
  finally {
    if (sequence === previewSequence) {
      previewBusy.value = false
    }
  }
}

async function downloadArtifact(artifact: ChatArtifact) {
  if (artifactDownloadBusy.value || !artifactReady(artifact)) {
    return
  }
  artifactDownloadBusy.value = artifact.id
  try {
    saveBlob(await chatApi.artifact(artifact.id), artifact.name)
  }
  catch (error) {
    ElMessage.error(errorMessage(error, '产物下载失败，请重试'))
  }
  finally {
    artifactDownloadBusy.value = ''
  }
}

async function selectConversation(id: string) {
  if (busy.value || uploading.value) {
    return
  }
  const sequence = ++selectionSequence
  stopPolling()
  historyOpen.value = false
  loadingConversation.value = true
  sendError.value = ''
  pollingError.value = ''
  try {
    const result = await chatApi.conversation(id)
    if (disposed || sequence !== selectionSequence) {
      return
    }
    adoptConversation(result)
    await router.replace({ query: { ...route.query, conversation: id } })
  }
  catch (error) {
    loadError.value = errorMessage(error, '会话读取失败，请重试')
  }
  finally {
    if (!disposed && sequence === selectionSequence) {
      loadingConversation.value = false
      // The message DOM replaces the skeleton only after loading ends.
      await scrollToBottom()
    }
  }
}

async function newConversation() {
  if (busy.value || uploading.value) {
    return
  }
  selectionSequence++
  stopPolling()
  current.value = undefined
  drafts.value.new ||= { text: '', files: [] }
  sendError.value = ''
  pollingError.value = ''
  historyOpen.value = false
  const { conversation: _conversation, ...query } = route.query
  await router.replace({ query })
  await nextTick()
  composer.value?.focus()
}

async function deleteConversation(item: ChatConversationSummary) {
  if (busy.value || uploading.value) {
    return
  }
  try {
    await ElMessageBox.confirm(`删除“${item.title}”及其中的消息？`, '删除对话', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  }
  catch {
    return
  }
  try {
    await chatApi.deleteConversation(item.id)
    clearDraft(item.id)
    conversations.value = conversations.value.filter(row => row.id !== item.id)
    historyTotal.value = Math.max(0, historyTotal.value - 1)
    if (current.value?.id === item.id) {
      await newConversation()
    }
    ElMessage.success('对话已删除')
  }
  catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function load() {
  if (loading.value) {
    return
  }
  loading.value = true
  loadError.value = ''
  const results = await Promise.allSettled([chatApi.models(), chatApi.conversations()])
  if (disposed) {
    return
  }
  if (results[0].status === 'fulfilled') {
    catalog.value = results[0].value
  }
  if (results[1].status === 'fulfilled') {
    conversations.value = results[1].value.items
    historyTotal.value = results[1].value.total
  }
  loadError.value = results.filter(result => result.status === 'rejected')
    .map(result => result.status === 'rejected' ? errorMessage(result.reason) : '')
    .join('；')
  loading.value = false
  const id = typeof route.query.conversation === 'string' ? route.query.conversation : ''
  if (id) {
    await selectConversation(id)
  }
}

async function uploadFile(item: DraftFile) {
  item.status = 'uploading'
  item.error = ''
  item.progress = 0
  try {
    const attachment = await chatApi.upload(item.file, (event) => {
      if (event.total) {
        // eslint-disable-next-line financial/no-division -- Upload progress percentage, not financial arithmetic.
        item.progress = Math.min(99, Math.round(event.loaded / event.total * 100))
      }
    })
    if (disposed || item.removed) {
      cleanupAttachment(attachment)
      return
    }
    item.attachment = attachment
    item.progress = 100
    item.status = 'ready'
  }
  catch (error) {
    item.status = 'failed'
    item.error = errorMessage(error, '附件上传失败')
  }
}

function addFiles(files: File[]) {
  if (busy.value || loading.value) {
    return
  }
  const extensions = acceptFiles.value.split(',').map(item => item.slice(1))
  for (const file of files) {
    if (draft.value.files.length >= 8) {
      ElMessage.warning('每条消息最多添加 8 个附件')
      break
    }
    const extension = file.name.split('.').pop()?.toLowerCase() || ''
    if (!extensions.includes(extension)) {
      ElMessage.warning(`暂不支持 ${file.name}，请上传 PDF、Word、文本、图片或视频`)
      continue
    }
    if (file.size > catalog.value.max_attachment_bytes) {
      ElMessage.warning(`${file.name} 超过单个附件 ${fileSize(catalog.value.max_attachment_bytes)} 的上限`)
      continue
    }
    const item: DraftFile = {
      key: temporaryId(),
      file,
      previewUrl: createObjectUrl(file),
      progress: 0,
      status: 'uploading',
    }
    draft.value.files.push(item)
    // Use the reactive proxy so progress changes update the attachment cards.
    void uploadFile(draft.value.files[draft.value.files.length - 1])
  }
}

function selectFiles(event: Event) {
  const input = event.target as HTMLInputElement
  addFiles(Array.from(input.files || []))
  input.value = ''
}

function removeFile(item: DraftFile) {
  if (sending.value) {
    return
  }
  draft.value.files = draft.value.files.filter(row => row.key !== item.key)
  item.removed = true
  revokeUrl(item.previewUrl)
  cleanupAttachment(item.attachment)
}

function handleDragEnter(event: DragEvent) {
  if (event.dataTransfer?.types.includes('Files')) {
    event.preventDefault()
    dragDepth.value++
  }
}

function handleDragOver(event: DragEvent) {
  if (event.dataTransfer?.types.includes('Files')) {
    event.preventDefault()
    event.dataTransfer.dropEffect = busy.value ? 'none' : 'copy'
  }
}

function handleDrop(event: DragEvent) {
  dragDepth.value = 0
  if (event.dataTransfer?.files.length) {
    event.preventDefault()
    addFiles(Array.from(event.dataTransfer.files))
  }
}

function handlePaste(event: ClipboardEvent) {
  const images = Array.from(event.clipboardData?.items || [])
    .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
    .map(item => item.getAsFile())
    .filter((file): file is File => Boolean(file))
  if (images.length) {
    event.preventDefault()
    addFiles(images)
    const text = event.clipboardData?.getData('text/plain') || ''
    if (text && composer.value) {
      const start = composer.value.selectionStart
      const end = composer.value.selectionEnd
      draft.value.text = draft.value.text.slice(0, start) + text + draft.value.text.slice(end)
    }
  }
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !composing.value && event.keyCode !== 229) {
    event.preventDefault()
    void send()
  }
}

async function reloadAfterFailure(id: string) {
  try {
    const conversation = await chatApi.conversation(id)
    if (!disposed && current.value?.id === id) {
      return adoptConversation(conversation)
    }
  }
  catch {
    // Keep the draft and the previous conversation if recovering the response also fails.
  }
  return false
}

async function checkSubmission() {
  if (!current.value || checkingSubmission.value) {
    return
  }
  checkingSubmission.value = true
  const id = current.value.id
  try {
    const conversation = await chatApi.conversation(id)
    if (disposed || current.value?.id !== id) {
      return
    }
    const accepted = adoptConversation(conversation)
    if (!accepted && !conversation.busy) {
      pendingDrafts.delete(id)
      sendError.value = '对话中尚未收到这条消息，输入已保留，可再次发送。'
    }
  }
  catch (error) {
    if (!disposed && current.value?.id === id) {
      sendError.value = errorMessage(error, '暂时无法确认接收状态，请稍后刷新')
    }
  }
  finally {
    checkingSubmission.value = false
  }
}

async function send() {
  if (!canSend.value) {
    return
  }
  cancelVoice()
  const editableDraft = draft.value
  const savedDraft: Draft = { text: editableDraft.text, files: [...editableDraft.files], reference: editableDraft.reference }
  const previousUserId = lastUser.value?.id
  let id = current.value?.id
  sending.value = true
  sendError.value = ''
  try {
    if (!current.value) {
      const conversation = await chatApi.createConversation({})
      drafts.value[conversation.id] = editableDraft
      drafts.value.new = { text: '', files: [] }
      adoptConversation(conversation)
      id = conversation.id
      await router.replace({ query: { ...route.query, conversation: id } })
    }
    if (!id) {
      throw new Error('会话创建失败，请重试')
    }
    pendingDrafts.set(id, { draft: savedDraft, previousUserId })
    const response = await chatApi.send(id, {
      content: savedDraft.text.trim(),
      attachment_ids: savedDraft.files.flatMap(item => item.attachment ? [item.attachment.id] : []),
      output: agentMode.value ? 'agent' : 'auto',
      reference_artifact_id: savedDraft.reference?.id,
    })
    adoptConversation(response)
    await scrollToBottom()
    await refreshHistory().catch(() => {})
  }
  catch (error) {
    const accepted = id ? await reloadAfterFailure(id) : false
    if (!accepted) {
      sendError.value = errorMessage(error, '消息尚未确认接收，输入和参考资料已保留，请稍后检查对话状态')
      const status = (error as { status?: number })?.status
      if (id && status && status >= 400 && status < 500) {
        pendingDrafts.delete(id)
      }
    }
    await refreshHistory().catch(() => {})
    await scrollToBottom()
  }
  finally {
    sending.value = false
    await nextTick()
    composer.value?.focus()
  }
}

async function retry() {
  if (!current.value || busy.value || remotePending.value || !lastAssistant.value) {
    return
  }
  const replyId = lastAssistant.value.id
  const id = current.value.id
  sending.value = true
  delete retryErrors.value[replyId]
  try {
    adoptConversation(await chatApi.retry(id))
    await refreshHistory().catch(() => {})
    await scrollToBottom()
  }
  catch (error) {
    await reloadAfterFailure(id)
    if (!remotePending.value && lastAssistant.value?.id === replyId) {
      retryErrors.value[replyId] = errorMessage(error, '重试请求未成功，请稍后再试')
    }
  }
  finally {
    sending.value = false
  }
}

async function copyMessage(message: ChatMessage) {
  try {
    await navigator.clipboard.writeText(message.content)
    ElMessage.success('回复已复制')
  }
  catch {
    ElMessage.warning('复制失败，请选中回复文字复制')
  }
}

async function showPreview(attachment: ChatAttachment, localUrl?: string) {
  closePreview()
  const sequence = previewSequence
  previewFile.value = attachment
  previewIsArtifact.value = false
  artifactPreviewText.value = ''
  previewUrl.value = localUrl || ''
  previewError.value = ''
  previewOpen.value = true
  if (localUrl || (attachment.kind === 'document' && !attachment.mime_type.includes('pdf'))) {
    return
  }
  previewBusy.value = true
  try {
    const blob = await chatApi.attachment(attachment.id)
    if (disposed || sequence !== previewSequence || previewFile.value?.id !== attachment.id || !previewOpen.value) {
      return
    }
    ownedPreviewUrl = createObjectUrl(blob)
    previewUrl.value = ownedPreviewUrl
  }
  catch (error) {
    if (sequence === previewSequence) {
      previewError.value = errorMessage(error, '附件预览读取失败')
    }
  }
  finally {
    if (sequence === previewSequence) {
      previewBusy.value = false
    }
  }
}

function previewDraft(item: DraftFile) {
  if (item.attachment) {
    void showPreview(item.attachment, item.previewUrl)
  }
}

function closePreview() {
  previewSequence++
  previewBusy.value = false
  previewError.value = ''
  if (ownedPreviewUrl) {
    revokeUrl(ownedPreviewUrl)
    ownedPreviewUrl = ''
  }
  previewUrl.value = ''
  previewFile.value = undefined
  artifactPreviewText.value = ''
}

async function downloadAttachment() {
  if (!previewFile.value || previewBusy.value) {
    return
  }
  previewBusy.value = true
  const sequence = previewSequence
  try {
    const file = previewFile.value
    saveBlob(await (previewIsArtifact.value ? chatApi.artifact(file.id) : chatApi.attachment(file.id)), file.name)
  }
  catch (error) {
    if (sequence === previewSequence) {
      previewError.value = errorMessage(error)
    }
  }
  finally {
    if (sequence === previewSequence) {
      previewBusy.value = false
    }
  }
}

onMounted(() => void load())
onActivated(() => {
  viewActive = true
  if (firstActivation) {
    firstActivation = false
    return
  }
  void load()
  schedulePolling()
})
onDeactivated(() => {
  viewActive = false
  cancelVoice()
  stopPolling()
})
onBeforeUnmount(() => {
  disposed = true
  cancelVoice()
  stopPolling()
  Object.values(drafts.value).forEach(value => value.files.forEach((item) => {
    item.removed = true
    cleanupAttachment(item.attachment)
  }))
  objectUrls.forEach(url => URL.revokeObjectURL(url))
  objectUrls.clear()
})
</script>

<template>
  <section
    class="content-chat-view"
    aria-label="AI 对话"
    :aria-busy="loading"
    @dragenter="handleDragEnter"
    @dragleave="dragDepth = Math.max(0, dragDepth - 1)"
    @dragover="handleDragOver"
    @drop="handleDrop"
  >
    <aside class="chat-history" aria-label="对话历史">
      <ElButton
        class="new-chat-button"
        :icon="Plus"
        :disabled="busy || uploading"
        @click="newConversation"
      >
        开启新对话
      </ElButton>
      <ElInput
        v-model="search"
        placeholder="搜索对话"
        aria-label="搜索对话历史"
        clearable
      />
      <div class="history-heading"><span>历史对话</span><span>{{ historyTotal }}</span></div>
      <div class="history-list">
        <p v-if="!historyItems.length" class="history-empty">{{ search ? '没有匹配的对话' : '开始对话后会自动保存在这里' }}</p>
        <div
          v-for="item in historyItems"
          :key="item.id"
          class="history-row"
          :class="{ selected: current?.id === item.id }"
        >
          <button
            type="button"
            class="history-item"
            :disabled="busy || uploading"
            :title="item.title"
            @click="selectConversation(item.id)"
          >
            <span>{{ item.title }}</span><small>{{ formatDate(item.updated_at).slice(5, 10) }}</small>
          </button>
          <ElButton
            text
            class="history-delete"
            :icon="Delete"
            :disabled="busy || uploading"
            :aria-label="`删除对话：${item.title}`"
            @click="deleteConversation(item)"
          />
        </div>
      </div>
      <ElButton
        v-if="conversations.length < historyTotal"
        text
        :loading="historyLoading"
        @click="loadMoreHistory"
      >
        加载更多对话
      </ElButton>
      <p class="history-note">随时继续你的想法</p>
    </aside>

    <div class="chat-main">
      <header class="chat-topbar">
        <div class="chat-title">
          <ElButton
            text
            class="mobile-history-button"
            :icon="Menu"
            aria-label="打开对话历史"
            @click="historyOpen = true"
          />
          <div><h2>{{ current?.title || 'AI 对话' }}</h2><p>保留当前对话上下文，按需生成文字、文件和媒体</p></div>
        </div>
        <ElButton
          text
          class="refresh-chat-button"
          :icon="Refresh"
          :loading="loading"
          :disabled="busy"
          aria-label="刷新对话历史与可用能力"
          @click="load"
        />
      </header>

      <div v-if="loadError" class="chat-load-error">
        <ElAlert
          :title="loadError"
          type="error"
          :closable="false"
          show-icon
        />
        <ElButton text :disabled="loading || busy" @click="load">重新加载</ElButton>
      </div>

      <div
        ref="messageList"
        class="message-list"
        role="log"
        aria-label="对话消息"
        aria-live="polite"
        tabindex="0"
        :aria-busy="sending || loadingConversation"
        @scroll="trackMessageScroll"
        @wheel.passive="handleMessageWheel"
        @pointerdown="handleMessagePointer"
        @keydown="handleMessageKey"
      >
        <ElSkeleton
          v-if="loadingConversation || loading"
          :rows="5"
          animated
          class="chat-skeleton"
        />
        <div v-else-if="!current?.messages.length" class="chat-welcome">
          <div class="welcome-icon"><ElIcon :size="32"><ChatDotRound /></ElIcon></div>
          <h3>今天，有什么想一起聊聊？</h3>
          <p>直接提问，或添加资料，生成文档、图片和视频，再一起修改。</p>
          <div class="welcome-suggestions">
            <button
              v-for="example in examples"
              :key="example"
              type="button"
              :disabled="busy"
              @click="draft.text = example; composer?.focus()"
            >
              {{ example }}
            </button>
          </div>
          <p v-if="!catalog.models.length" class="no-model-note">{{ catalog.message || '尚未配置创作模型，请联系管理员；已有文档仍可直接转换格式。' }}</p>
        </div>
        <div v-else v-follow-latest class="message-content">
          <article
            v-for="message in current.messages"
            :key="message.id"
            class="chat-message"
            :class="message.role"
          >
            <div class="message-label">{{ message.role === 'user' ? '你' : modelName(message.model_id) }}</div>
            <div
              v-if="message.role === 'assistant' && routingOf(message)"
              class="routing-tag"
              :title="routingOf(message)!.method === 'function_calling' ? '由大模型通过函数调用选择工具' : '模型未返回工具调用时，按规则选择工具'"
            >
              {{ routingLabel(routingOf(message)!) }}
            </div>
            <div v-if="message.attachments?.length" class="message-attachments">
              <button
                v-for="attachment in message.attachments"
                :key="attachment.id"
                type="button"
                class="sent-attachment"
                @click="showPreview(attachment)"
              >
                <ElIcon :size="22"><component :is="fileIcon(attachment.kind)" /></ElIcon>
                <span><strong>{{ attachment.name }}</strong><small>{{ fileSize(attachment.size) }}{{ attachment.kind === 'video' ? ' · 关键帧识别，不含音轨' : '' }}</small></span>
              </button>
            </div>
            <AgentTrace
              v-if="message.role === 'assistant' && message.status === 'pending' && (liveReplies.get(message.id)?.plan.length || liveReplies.get(message.id)?.steps.length)"
              :plan="liveReplies.get(message.id)!.plan"
              :steps="liveReplies.get(message.id)!.steps"
              live
            />
            <AgentTrace
              v-else-if="message.role === 'assistant' && message.agent && (message.agent.plan.length || message.agent.steps.length || message.agent.actions.length)"
              :plan="message.agent.plan"
              :steps="message.agent.steps"
              :actions="message.agent.actions"
            />
            <div v-if="message.role === 'user' && message.content" class="user-message-text">{{ message.content }}</div>
            <div v-else-if="message.content" v-dompurify-html="renderMarkdown(message.content)" class="assistant-markdown" />
            <div
              v-else-if="message.status === 'pending' && liveReplies.get(message.id)?.text"
              v-dompurify-html="renderMarkdown(liveReplies.get(message.id)!.text)"
              class="assistant-markdown streaming"
              aria-live="polite"
            />
            <p v-if="message.status === 'pending'" class="message-pending" role="status"><ElIcon class="is-loading"><Loading /></ElIcon>{{ liveReplies.get(message.id)?.stage || '正在生成，完成后会自动显示…' }}<span v-if="waitedSeconds(message) >= 5" class="pending-elapsed">已等待 {{ waitedSeconds(message) }} 秒</span></p>
            <div v-if="message.artifacts?.length" class="message-artifacts">
              <section
                v-for="artifact in message.artifacts"
                :key="artifact.id"
                class="artifact-card"
                :class="{ 'artifact-media-card': ['image', 'video'].includes(artifact.kind) }"
                :aria-label="artifact.name"
              >
                <div v-if="['image', 'video'].includes(artifact.kind) && artifactReady(artifact)" v-artifact-media="artifact" class="artifact-media">
                  <button
                    v-if="artifact.kind === 'image' && artifactMediaUrls[artifact.id]"
                    type="button"
                    class="artifact-image-button"
                    :aria-label="`放大查看 ${artifact.name}`"
                    @click="previewArtifact(artifact, message)"
                  >
                    <img
                      :src="artifactMediaUrls[artifact.id]"
                      :alt="artifact.name"
                      loading="lazy"
                      @load="followMediaResize"
                    >
                  </button>
                  <video
                    v-else-if="artifact.kind === 'video' && artifactMediaUrls[artifact.id]"
                    :src="artifactMediaUrls[artifact.id]"
                    controls
                    preload="metadata"
                    @loadedmetadata="followMediaResize"
                  />
                  <div v-else class="artifact-media-placeholder">
                    <ElIcon v-if="artifactMediaLoading[artifact.id]" class="is-loading"><Loading /></ElIcon>
                    <ElIcon v-else :size="32"><component :is="fileIcon(artifact.kind)" /></ElIcon>
                    <span>{{ artifactMediaErrors[artifact.id] || '正在读取预览' }}</span>
                    <ElButton v-if="artifactMediaErrors[artifact.id]" text @click="loadArtifactMedia(artifact)">重新加载</ElButton>
                  </div>
                </div>
                <div class="artifact-summary"><span class="artifact-file-icon"><ElIcon :size="24"><component :is="fileIcon(artifact.kind)" /></ElIcon></span><div><strong>{{ artifact.name }}</strong><small>{{ artifact.kind === 'doc' || artifact.kind === 'docx' ? 'Word 文档' : artifact.kind === 'pdf' ? 'PDF 文档' : artifact.kind === 'image' ? '图片' : '视频' }} · {{ artifact.size ? fileSize(artifact.size) : '生成中' }}</small></div></div>
                <p v-if="artifact.status === 'pending'" class="artifact-status" role="status">正在生成文件…</p>
                <p v-else-if="artifact.status === 'failed'" class="artifact-status artifact-failed" role="alert">{{ artifact.error || '文件生成失败，请重试' }}</p>
                <div v-else class="artifact-actions">
                  <ElButton text :disabled="!artifactReady(artifact)" @click="previewArtifact(artifact, message)">预览</ElButton>
                  <ElButton
                    text
                    :icon="Download"
                    :loading="artifactDownloadBusy === artifact.id"
                    :disabled="!artifactReady(artifact) || Boolean(artifactDownloadBusy)"
                    @click="downloadArtifact(artifact)"
                  >
                    下载
                  </ElButton>
                  <ElButton
                    text
                    type="primary"
                    :disabled="busy || !artifactReady(artifact)"
                    @click="useArtifact(artifact)"
                  >
                    {{ artifact.kind === 'video' ? '调整后重做' : '继续修改' }}
                  </ElButton>
                </div>
              </section>
            </div>
            <div v-if="message.status === 'failed' || message.error || retryErrors[message.id]" class="message-failure" role="alert">
              <span>{{ retryErrors[message.id] || message.error || '回复失败，请重试' }}</span>
            </div>
            <div v-if="message.role === 'assistant'" class="message-actions">
              <ElButton
                v-if="message.content"
                text
                :icon="CopyDocument"
                aria-label="复制回复"
                @click="copyMessage(message)"
              >
                复制
              </ElButton>
              <ElButton
                v-if="message.id === lastAssistant?.id"
                text
                :icon="Refresh"
                :disabled="busy || remotePending"
                @click="retry"
              >
                {{ message.status === 'failed' || message.error ? '重试回复' : '重新生成' }}
              </ElButton>
            </div>
          </article>
        </div>
        <div v-if="sending" class="thinking-indicator" role="status"><ElIcon class="is-loading"><Loading /></ElIcon><span>AI 正在处理…</span></div>
      </div>

      <div class="composer-area">
        <div v-if="pollingError" class="capability-error" role="status">{{ pollingError }}，正在自动重试。</div>
        <div v-if="sendError || submissionUncertain" class="send-error" role="alert">
          <span>{{ sendError || '消息接收状态待确认' }}</span><small>{{ sendErrorHint }}</small>
          <ElButton
            v-if="submissionUncertain"
            text
            :loading="checkingSubmission"
            @click="checkSubmission"
          >
            刷新确认接收状态
          </ElButton>
        </div>
        <div class="chat-composer" :class="{ disabled: busy }">
          <div v-if="draft.reference" class="artifact-reference">
            <ElIcon :size="22"><component :is="fileIcon(draft.reference.kind)" /></ElIcon><span><small>正在参考</small><strong>{{ draft.reference.name }}</strong></span>
            <ElButton
              text
              :icon="Close"
              :disabled="sending"
              aria-label="移除参考产物"
              @click="draft.reference = undefined"
            />
          </div>
          <div v-if="draft.files.length" class="draft-attachments" aria-label="待发送附件">
            <div
              v-for="item in draft.files"
              :key="item.key"
              class="draft-attachment"
              :class="{ failed: item.status === 'failed' }"
            >
              <button
                type="button"
                class="draft-preview"
                :disabled="item.status !== 'ready'"
                :aria-label="`预览 ${item.file.name}`"
                @click="previewDraft(item)"
              >
                <img v-if="kindOf(item) === 'image'" :src="item.previewUrl" :alt="item.file.name">
                <ElIcon v-else :size="24"><component :is="fileIcon(kindOf(item))" /></ElIcon>
                <span><strong>{{ item.file.name }}</strong><small>{{ item.status === 'uploading' ? `${item.progress}% · 上传与解析中` : item.status === 'failed' ? '上传失败' : fileSize(item.file.size) }}</small></span>
              </button>
              <ElButton
                v-if="item.status === 'failed'"
                text
                :icon="Refresh"
                :disabled="busy"
                :aria-label="`重新上传 ${item.file.name}`"
                @click="uploadFile(item)"
              />
              <ElButton
                text
                :icon="Close"
                :disabled="sending"
                :aria-label="`移除 ${item.file.name}`"
                @click="removeFile(item)"
              />
              <p v-if="item.error" class="attachment-error">{{ item.error }}</p>
              <p v-else-if="kindOf(item) === 'video'" class="attachment-hint">关键帧识别，不含音轨</p>
            </div>
          </div>
          <label for="chat-prompt" class="sr-only">发送给 AI 的文字</label>
          <textarea
            id="chat-prompt"
            ref="composer"
            v-model="draft.text"
            rows="3"
            :disabled="loadingConversation || loading"
            maxlength="16000"
            placeholder="发送消息，或将资料拖到这里…"
            @keydown="handleKeydown"
            @paste="handlePaste"
            @compositionstart="composing = true"
            @compositionend="composing = false"
          />
          <div class="composer-toolbar">
            <div class="composer-tools">
              <ElButton
                text
                :icon="Paperclip"
                :disabled="busy || loading || draft.files.length >= 8"
                aria-label="添加文档、图片或视频附件"
                @click="fileInput?.click()"
              >
                添加附件
              </ElButton>
              <ElButton
                text
                :icon="Operation"
                class="agent-toggle"
                :class="{ 'is-active': agentMode }"
                :aria-pressed="agentMode"
                title="开启后直接交给运营助手：查询商品、审核和发布数据，发起生成，准备发布"
                @click="agentMode = !agentMode"
              >
                运营助手
              </ElButton>
              <ElButton
                v-if="voiceSupported"
                text
                :icon="Microphone"
                class="voice-toggle"
                :class="{ 'is-active': voiceActive }"
                :aria-pressed="voiceActive"
                :disabled="busy || voiceState === 'transcribing'"
                title="说话自动转成文字，最长 1 分钟；再点一次停止"
                @click="toggleVoice"
              >
                {{ voiceLabel }}
              </ElButton>
            </div>
            <ElButton
              type="primary"
              class="send-button"
              :icon="ArrowUp"
              :loading="sending"
              :disabled="!canSend"
              aria-label="发送消息"
              @click="send"
            >
              发送
            </ElButton>
          </div>
          <input
            ref="fileInput"
            class="sr-only"
            type="file"
            :accept="acceptFiles"
            multiple
            tabindex="-1"
            aria-hidden="true"
            @change="selectFiles"
          >
        </div>
        <div class="composer-hint"><span>拖入 DOC、DOCX、PDF、图片或视频，直接说明需求 · 附件单个 {{ fileSize(catalog.max_attachment_bytes) }} 以内</span><span class="keyboard-hint">Enter 发送 · Shift + Enter 换行</span></div>
      </div>
    </div>

    <div v-if="dragDepth && !busy" class="drop-overlay" aria-hidden="true"><ElIcon :size="40"><Paperclip /></ElIcon><strong>松开即可添加资料</strong><span>DOC、DOCX、PDF、图片或视频，最多 8 个附件</span></div>

    <ElDrawer
      v-model="historyOpen"
      title="对话历史"
      direction="ltr"
      size="min(340px, 88vw)"
      class="chat-history-drawer"
    >
      <ElButton
        class="new-chat-button"
        :icon="Plus"
        :disabled="busy || uploading"
        @click="newConversation"
      >
        开启新对话
      </ElButton>
      <ElInput
        v-model="search"
        placeholder="搜索对话"
        aria-label="搜索对话历史"
        clearable
      />
      <p v-if="!historyItems.length" class="history-empty">{{ search ? '没有匹配的对话' : '开始对话后会自动保存在这里' }}</p>
      <div
        v-for="item in historyItems"
        :key="item.id"
        class="history-row"
        :class="{ selected: current?.id === item.id }"
      >
        <button
          type="button"
          class="history-item"
          :disabled="busy || uploading"
          @click="selectConversation(item.id)"
        >
          <span>{{ item.title }}</span><small>{{ formatDate(item.updated_at).slice(5, 10) }}</small>
        </button>
        <ElButton
          text
          class="history-delete"
          :icon="Delete"
          :disabled="busy || uploading"
          :aria-label="`删除对话：${item.title}`"
          @click="deleteConversation(item)"
        />
      </div>
      <ElButton
        v-if="conversations.length < historyTotal"
        text
        :loading="historyLoading"
        @click="loadMoreHistory"
      >
        加载更多对话
      </ElButton>
    </ElDrawer>

    <ElDialog
      v-model="previewOpen"
      :title="previewFile?.name || '附件预览'"
      width="min(880px, 94vw)"
      destroy-on-close
      @closed="closePreview"
    >
      <div v-loading="previewBusy" class="attachment-preview">
        <ElAlert
          v-if="previewError"
          :title="previewError"
          type="error"
          :closable="false"
          show-icon
        />
        <img v-else-if="previewFile?.kind === 'image' && previewUrl" :src="previewUrl" :alt="previewFile.name">
        <video
          v-else-if="previewFile?.kind === 'video' && previewUrl"
          :src="previewUrl"
          controls
          preload="metadata"
        />
        <iframe
          v-else-if="previewFile?.mime_type.includes('pdf') && previewUrl"
          :src="previewUrl"
          :title="previewFile.name"
          sandbox="allow-same-origin"
        />
        <pre v-else-if="previewFile?.text_preview || artifactPreviewText">{{ previewFile?.text_preview || artifactPreviewText }}</pre>
        <ElEmpty v-else-if="!previewBusy" description="此文件可下载查看" />
      </div>
      <p v-if="previewFile?.kind === 'video' && !previewIsArtifact" class="preview-note">AI 通过视频关键帧识别画面内容，不解析音轨。</p>
      <p v-else-if="previewFile?.kind === 'document' && previewFile.text_preview" class="preview-note">预览展示已提取的部分文字。</p>
      <template #footer><ElButton @click="previewOpen = false">关闭</ElButton><ElButton type="primary" :loading="previewBusy" @click="downloadAttachment">{{ previewIsArtifact ? '下载文件' : '下载原文件' }}</ElButton></template>
    </ElDialog>
  </section>
</template>

<style scoped>
.content-chat-view {
  position: relative;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  height: 100%;
  min-height: 0;
  overflow: hidden;
  color: var(--xi-semantic-color-text-primary);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}
.chat-history {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 0;
  padding: 24px 16px;
  background: var(--xi-semantic-color-background-page);
  border-right: var(--xi-component-content-border);
}
.new-chat-button {
  width: 100%;
  min-height: 44px;
  margin-bottom: 4px;
}
.history-heading {
  display: flex;
  justify-content: space-between;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  padding-inline: 8px;
}
.history-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.history-empty,
.history-note {
  margin: 8px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  line-height: 1.8;
}
.history-row {
  display: flex;
  align-items: center;
  border-radius: var(--xi-component-content-radius-card);
  margin-bottom: 4px;
}
.history-row.selected {
  background: var(--xi-semantic-color-background-brand-light);
  color: var(--xi-semantic-color-text-brand);
}
.history-row:hover {
  background: var(--xi-semantic-color-background-brand-subtle);
}
.history-item {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  min-height: 60px;
  padding: 10px 12px;
  color: inherit;
  text-align: left;
  background: transparent;
  border: 0;
  cursor: pointer;
}
.history-item span {
  width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}
.history-item small {
  font-size: 12px;
  color: var(--xi-semantic-color-text-tertiary);
}
.history-delete {
  min-height: 44px;
  width: 40px;
  flex-shrink: 0;
  padding: 8px;
}
.chat-main {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}
.chat-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 24px;
  flex-shrink: 0;
  border-bottom: var(--xi-component-content-border);
}
.chat-title {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 8px;
}
.chat-title h2 {
  margin: 0;
  font-size: 18px;
  line-height: 26px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 360px;
}
.chat-title p {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--xi-semantic-color-text-tertiary);
}
.refresh-chat-button {
  width: 44px;
  height: 44px;
  flex-shrink: 0;
}
.mobile-history-button {
  display: none;
}
.chat-load-error {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 24px 0;
}
.chat-load-error .el-alert {
  flex: 1;
}
.message-list {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 28px 32px 24px;
  overscroll-behavior: contain;
}
.chat-skeleton {
  max-width: 800px;
  margin: 32px auto;
}
.chat-welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  max-width: 740px;
  min-height: 100%;
  margin: auto;
  text-align: center;
}
.welcome-icon {
  display: grid;
  place-items: center;
  width: 64px;
  height: 64px;
  margin-bottom: 20px;
  color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-brand-light);
  border-radius: var(--xi-component-content-radius-overlay);
}
.chat-welcome h3 {
  margin: 0;
  font-size: 26px;
  line-height: 38px;
  font-weight: 600;
}
.chat-welcome > p {
  margin: 12px 0 28px;
  color: var(--xi-semantic-color-text-secondary);
  font-size: 14px;
  line-height: 1.8;
}
.welcome-suggestions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 12px;
}
.welcome-suggestions button {
  min-height: 44px;
  padding: 12px 16px;
  color: var(--xi-semantic-color-text-secondary);
  text-align: left;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
  cursor: pointer;
  transition: background 160ms;
}
.welcome-suggestions button:hover {
  background: var(--xi-semantic-color-background-brand-subtle);
}
.chat-welcome > .no-model-note {
  margin-top: 24px;
  color: var(--xi-semantic-color-feedback-warning-text);
}
.message-content {
  max-width: 1120px;
  margin: 0 auto;
}
.chat-message {
  min-width: 0;
  margin-bottom: 32px;
}
.chat-message.user {
  max-width: 86%;
  margin-left: auto;
  text-align: right;
}
.message-label {
  margin-bottom: 8px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
}
.voice-toggle.is-active {
  color: var(--xi-semantic-color-feedback-danger-text);
  background: var(--xi-semantic-color-feedback-danger-background-subtle);
}
.agent-toggle.is-active {
  color: var(--xi-semantic-color-feedback-info-text);
  background: var(--xi-semantic-color-feedback-info-background-subtle);
}
.routing-tag {
  display: inline-block;
  margin: -2px 0 10px;
  padding: 1px 8px;
  font-size: 12px;
  border: var(--xi-component-content-border);
  border-radius: 999px;
  color: var(--xi-semantic-color-text-secondary);
  background: var(--xi-semantic-color-background-page);
}
.assistant-markdown.streaming > :deep(:last-child)::after {
  content: '';
  display: inline-block;
  width: 7px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--xi-semantic-color-text-brand);
  animation: stream-caret 1s steps(1) infinite;
}
@keyframes stream-caret {
  50% {
    opacity: 0;
  }
}
.user-message-text {
  display: inline-block;
  max-width: 100%;
  padding: 14px 20px;
  text-align: left;
  line-height: 1.8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: var(--xi-semantic-color-background-brand-light);
  border-radius: var(--xi-component-content-radius-overlay);
}
.assistant-markdown {
  line-height: 1.85;
  font-size: 15px;
  overflow-wrap: anywhere;
}
.assistant-markdown :deep(p) {
  margin: 0 0 16px;
}
.assistant-markdown :deep(ul),
.assistant-markdown :deep(ol) {
  padding-left: 24px;
  margin: 12px 0 16px;
  list-style: revert;
}
.assistant-markdown :deep(li) {
  margin: 6px 0;
}
.assistant-markdown :deep(h1),
.assistant-markdown :deep(h2),
.assistant-markdown :deep(h3) {
  font-size: 20px;
  line-height: 1.5;
  font-weight: 600;
  margin: 24px 0 12px;
}
.assistant-markdown :deep(h4),
.assistant-markdown :deep(h5),
.assistant-markdown :deep(h6) {
  font-size: 16px;
  font-weight: 600;
  margin: 20px 0 8px;
}
.assistant-markdown :deep(pre) {
  max-width: 100%;
  padding: 16px;
  overflow-x: auto;
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}
.assistant-markdown :deep(code) {
  font-family: Consolas, monospace;
  font-size: 13px;
}
.assistant-markdown :deep(a) {
  color: var(--xi-semantic-color-link-default);
  text-decoration: underline;
}
.assistant-markdown :deep(blockquote) {
  margin: 16px 0;
  padding-left: 16px;
  border-left: 3px solid var(--xi-semantic-color-border-brand-subtle);
  color: var(--xi-semantic-color-text-secondary);
}
.assistant-markdown :deep(table) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  margin: 16px 0;
}
.assistant-markdown :deep(th),
.assistant-markdown :deep(td) {
  border: var(--xi-component-content-border);
  padding: 8px 12px;
}
.message-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.message-actions .el-button {
  min-height: 44px;
  margin: 0;
  color: var(--xi-semantic-color-text-tertiary);
}
.message-pending {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0;
  color: var(--xi-semantic-color-text-brand);
  font-size: 14px;
}
.pending-elapsed {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.message-artifacts {
  display: grid;
  gap: 16px;
  margin-top: 20px;
}
.artifact-card {
  width: 100%;
  max-width: 520px;
  overflow: hidden;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-overlay);
}
.artifact-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
}
.artifact-summary > div {
  min-width: 0;
}
.artifact-summary strong {
  display: block;
  font-size: 14px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.artifact-summary small {
  display: block;
  margin-top: 4px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
}
.artifact-file-icon {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 44px;
  height: 44px;
  color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-brand-light);
  border-radius: var(--xi-component-content-radius-card);
}
.artifact-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  padding: 0 12px 8px;
}
.artifact-actions .el-button {
  min-height: 44px;
  margin: 0;
}
.artifact-status {
  margin: 0;
  padding: 0 16px 16px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 13px;
}
.artifact-failed {
  color: var(--xi-semantic-color-feedback-danger-text);
}
.artifact-media {
  background: var(--xi-semantic-color-background-page);
}
.artifact-media img,
.artifact-media video {
  display: block;
  width: 100%;
  max-height: 400px;
  object-fit: contain;
}
.artifact-image-button {
  display: block;
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: zoom-in;
}
.artifact-media-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  min-height: 200px;
  padding: 20px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 13px;
}
.artifact-reference {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  padding: 8px 12px;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand);
  border-radius: var(--xi-component-content-radius-card);
  color: var(--xi-semantic-color-text-brand);
}
.artifact-reference > span {
  flex: 1;
  min-width: 0;
}
.artifact-reference small,
.artifact-reference strong {
  display: block;
  font-size: 12px;
  font-weight: 400;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.artifact-reference strong {
  margin-top: 4px;
  font-size: 13px;
  font-weight: 600;
}
.artifact-reference .el-button {
  width: 44px;
  height: 44px;
  padding: 8px;
}
.message-failure,
.send-error,
.capability-error {
  color: var(--xi-semantic-color-feedback-danger-text);
  font-size: 13px;
  line-height: 1.7;
}
.message-failure {
  margin-top: 8px;
  padding: 12px 16px;
  border-radius: var(--xi-component-content-radius-card);
  background: var(--xi-semantic-color-feedback-danger-background-subtle);
}
.thinking-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 840px;
  margin: 20px auto 0;
  color: var(--xi-semantic-color-text-brand);
  font-size: 14px;
}
.message-attachments {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 12px;
}
.sent-attachment {
  display: flex;
  align-items: center;
  gap: 12px;
  max-width: 100%;
  min-height: 64px;
  padding: 12px 16px;
  text-align: left;
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
  cursor: pointer;
}
.sent-attachment .el-icon {
  color: var(--xi-semantic-color-text-brand);
  flex-shrink: 0;
}
.sent-attachment > span {
  min-width: 0;
}
.sent-attachment strong,
.draft-preview strong {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 500;
  max-width: 320px;
}
.sent-attachment small,
.draft-preview small {
  display: block;
  font-size: 12px;
  margin-top: 4px;
  color: var(--xi-semantic-color-text-tertiary);
}
.composer-area {
  width: 100%;
  max-width: 1184px;
  flex-shrink: 0;
  margin-inline: auto;
  padding: 8px 32px 12px;
}
.send-error {
  display: flex;
  flex-direction: column;
  margin-bottom: 12px;
}
.send-error small {
  color: var(--xi-semantic-color-text-secondary);
}
.capability-error {
  margin-bottom: 12px;
  color: var(--xi-semantic-color-feedback-warning-text);
}
.chat-composer {
  padding: 16px 16px 12px;
  border: var(--xi-component-content-border-strong);
  border-radius: var(--xi-component-content-radius-overlay);
  background: var(--xi-semantic-color-background-container);
  transition: border-color 160ms;
}
.chat-composer:focus-within {
  border-color: var(--xi-semantic-color-border-brand-default);
}
.chat-composer textarea {
  display: block;
  width: 100%;
  min-height: 64px;
  max-height: 240px;
  padding: 0;
  border: 0;
  outline: 0;
  resize: vertical;
  color: var(--xi-semantic-color-text-primary);
  background: transparent;
  font: inherit;
  font-size: 15px;
  line-height: 1.8;
}
.chat-composer textarea::placeholder {
  color: var(--xi-semantic-color-text-placeholder);
}
.chat-composer.disabled {
  background: var(--xi-semantic-color-background-page);
}
.composer-toolbar,
.composer-tools {
  display: flex;
  align-items: center;
  gap: 12px;
}
.composer-toolbar {
  justify-content: space-between;
  margin-top: 8px;
}
.composer-tools {
  flex-wrap: wrap;
  min-width: 0;
}
.composer-tools .el-button,
.send-button {
  min-height: 44px;
}
.composer-hint {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 10px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  line-height: 1.7;
}
.draft-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.draft-attachment {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  max-width: 100%;
  padding: 4px 8px;
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
  background: var(--xi-semantic-color-background-page);
}
.draft-attachment.failed {
  border-color: var(--xi-semantic-color-feedback-danger-border);
}
.draft-preview {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 52px;
  min-width: 0;
  max-width: 260px;
  padding: 4px;
  color: var(--xi-semantic-color-text-brand);
  background: transparent;
  border: 0;
  text-align: left;
  cursor: pointer;
}
.draft-preview > span {
  min-width: 0;
}
.draft-preview img {
  width: 44px;
  height: 44px;
  object-fit: cover;
  border-radius: var(--xi-component-content-radius-control);
}
.draft-preview strong {
  max-width: 170px;
  color: var(--xi-semantic-color-text-primary);
}
.draft-attachment .el-button {
  width: 44px;
  height: 44px;
  padding: 8px;
  margin: 0;
}
.attachment-error,
.attachment-hint {
  width: 100%;
  margin: 0 4px 4px;
  font-size: 12px;
  line-height: 1.6;
}
.attachment-error {
  color: var(--xi-semantic-color-feedback-danger-text);
}
.attachment-hint {
  color: var(--xi-semantic-color-text-tertiary);
}
.drop-overlay {
  position: absolute;
  inset: 0;
  z-index: 4;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  background: var(--xi-semantic-color-background-brand-subtle);
  color: var(--xi-semantic-color-text-brand);
  border: 2px dashed var(--xi-semantic-color-border-brand-default);
  pointer-events: none;
}
.drop-overlay strong {
  font-size: 24px;
}
.drop-overlay span {
  font-size: 14px;
}
.attachment-preview {
  min-height: 200px;
  max-height: 65dvh;
  overflow: auto;
  text-align: center;
}
.attachment-preview img,
.attachment-preview video {
  display: block;
  max-width: 100%;
  max-height: 60dvh;
  margin: auto;
}
.attachment-preview iframe {
  width: 100%;
  height: 60dvh;
  border: var(--xi-component-content-border);
}
.attachment-preview pre {
  padding: 16px;
  margin: 0;
  text-align: left;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  line-height: 1.8;
  color: var(--xi-semantic-color-text-primary);
  background: var(--xi-semantic-color-background-page);
}
.preview-note {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  margin-top: 12px;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
button:disabled {
  cursor: not-allowed;
}
@media (width <= 1100px) {
  .content-chat-view {
    grid-template-columns: 208px minmax(0, 1fr);
  }
  .chat-topbar {
    padding: 16px 20px;
  }
  .chat-title p,
  .chat-title h2 {
    max-width: 200px;
  }
  .message-list {
    padding: 24px;
  }
  .composer-area {
    padding-inline: 24px;
  }
  .keyboard-hint {
    display: none;
  }
}
@media (width <= 768px) {
  .content-chat-view {
    grid-template-columns: minmax(0, 1fr);
  }
  .chat-history {
    display: none;
  }
  .mobile-history-button {
    display: inline-flex;
    width: 44px;
    height: 44px;
    padding: 8px;
  }
  .chat-topbar {
    gap: 8px;
    padding: 12px;
    flex-wrap: wrap;
  }
  .chat-title {
    flex: 1;
  }
  .chat-title h2 {
    max-width: 220px;
    font-size: 16px;
  }
  .chat-load-error {
    margin-inline: 12px;
  }
  .message-list {
    padding: 20px 16px;
  }
  .chat-welcome h3 {
    font-size: 22px;
    line-height: 32px;
  }
  .welcome-suggestions {
    flex-direction: column;
    width: 100%;
    max-width: 320px;
  }
  .chat-message.user {
    max-width: 94%;
  }
  .assistant-markdown,
  .chat-composer textarea {
    font-size: 16px;
  }
  .composer-area {
    padding: 8px 12px max(12px, env(safe-area-inset-bottom));
  }
  .chat-composer {
    padding: 12px;
  }
  .composer-toolbar {
    gap: 4px;
  }
  .composer-hint {
    font-size: 12px;
  }
  .draft-preview {
    max-width: 210px;
  }
  .draft-preview strong {
    max-width: 120px;
  }
}
@media (width <= 600px) {
  .chat-topbar {
    gap: 4px 12px;
    padding-block: 8px;
  }
  .chat-title > div {
    min-width: 0;
    flex: 1;
  }
  .chat-title h2 {
    max-width: none;
  }
  .mobile-history-button {
    flex-shrink: 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .chat-composer,
  .welcome-suggestions button {
    transition: none;
  }
  .is-loading,
  .assistant-markdown.streaming > :deep(:last-child)::after {
    animation: none;
  }
}
</style>
