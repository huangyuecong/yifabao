<script setup lang="ts">
import type { AgentStep, ChatArtifact, ChatConversation, ChatMessage, ChatModelCatalog } from '@/api/agent/chat'
import { ArrowUp, ChatDotRound, Document, Download, Loading, Microphone, Minus, Picture, Plus, Refresh, VideoCamera } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { chatApi, streamReply } from '@/api/agent/chat'
import { saveBlob } from '@/api/agent/http'
import AgentTrace from './AgentTrace.vue'
import { useVoiceInput } from './voiceInput'

/*
 * The floating assistant's dialog. It is an ordinary AI 对话 conversation (auto routing,
 * so it can query and operate the workbench, write documents, and make images or videos);
 * the same conversation shows up in AI 对话 history.
 */
const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  close: []
  status: [value: { busy: boolean, unread: boolean }]
}>()

const STORAGE_KEY = 'yifabao.assistant.conversation'
const SUGGESTIONS = [
  '今天发布了哪些内容？',
  '把今天的宣传情况整理成 Word',
  '有哪些内容在等我审核？',
  '帮我给最近的商品做宣传视频',
]
const TOOL_LABELS: Record<string, string> = {
  answer_in_chat: '文字回答',
  create_document: '生成文档',
  generate_image: '生成图片',
  generate_video: '生成视频',
  operate_workbench: '运营助手',
}

const router = useRouter()
const conversation = ref<ChatConversation>()
const catalog = ref<ChatModelCatalog>()
const text = ref('')
const sending = ref(false)
const outgoing = ref('')
const loading = ref(false)
const error = ref('')
const unread = ref(false)
const composing = ref(false)
const list = ref<HTMLElement>()
const input = ref<HTMLTextAreaElement>()
const live = reactive({ id: '', text: '', stage: '', plan: [] as string[], steps: [] as AgentStep[] })
const thumbnails = reactive(new Map<string, string>())
let controller: AbortController | undefined
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content || '图片')
const defaultLinkOpen = markdown.renderer.rules.link_open
markdown.renderer.rules.link_open = (tokens, index, options, environment, renderer) => {
  tokens[index].attrSet('rel', 'noopener noreferrer')
  tokens[index].attrSet('target', '_blank')
  return defaultLinkOpen
    ? defaultLinkOpen(tokens, index, options, environment, renderer)
    : renderer.renderToken(tokens, index, options)
}
function renderMarkdown(value: string) {
  return markdown.render(value.replace(/\*\*([^*\n]+?)([：:，,。！!？?；;、])\*\*/gu, '**$1**$2'))
}

const messages = computed(() => conversation.value?.messages ?? [])
const pending = computed(() => messages.value.find(message => message.role === 'assistant' && message.status === 'pending'))
const busy = computed(() => sending.value || Boolean(pending.value))
const statusLine = computed(() => {
  if (sending.value) {
    return '正在发送…'
  }
  if (pending.value) {
    return live.stage || '正在处理…'
  }
  return '查数据 · 做内容 · 准备发布'
})

let voiceBase = ''
const {
  state: voiceState,
  seconds: voiceSeconds,
  active: voiceActive,
  supported: voiceSupported,
  toggle: toggleVoiceInput,
  cancel: cancelVoice,
} = useVoiceInput({
  onText: (value) => {
    text.value = voiceBase + value
  },
  transcribe: () => (catalog.value?.speech_input ? recording => chatApi.transcribe(recording) : undefined),
  notify: (level, message) => ElMessage[level](message),
})

function toggleVoice() {
  if (!voiceActive.value) {
    voiceBase = text.value
  }
  toggleVoiceInput()
  void nextTick(() => input.value?.focus())
}

function errorMessage(reason: unknown, fallback = '操作失败，请稍后重试') {
  return reason instanceof Error && reason.message ? reason.message : fallback
}

function storedId() {
  try {
    return localStorage.getItem(STORAGE_KEY) || ''
  }
  catch {
    return ''
  }
}
function remember(id: string) {
  try {
    if (id) {
      localStorage.setItem(STORAGE_KEY, id)
    }
    else {
      localStorage.removeItem(STORAGE_KEY)
    }
  }
  catch {
    // Private windows may refuse storage; the dialog still works for this visit.
  }
}

async function scrollToBottom() {
  await nextTick()
  if (list.value) {
    list.value.scrollTop = list.value.scrollHeight
  }
}

function stopFollowing() {
  controller?.abort()
  controller = undefined
  clearTimeout(pollTimer)
  pollTimer = undefined
}

async function refresh() {
  const id = conversation.value?.id
  if (!id || disposed) {
    return
  }
  try {
    const latest = await chatApi.conversation(id)
    if (disposed || conversation.value?.id !== id) {
      return
    }
    const wasBusy = Boolean(pending.value)
    conversation.value = latest
    if (pending.value) {
      follow()
    }
    else {
      stopFollowing()
      live.id = ''
      if (wasBusy && !props.open) {
        unread.value = true
      }
    }
    void scrollToBottom()
  }
  catch {
    schedulePoll()
  }
}

function schedulePoll() {
  clearTimeout(pollTimer)
  // The stream is the fast path; polling keeps the saved conversation authoritative.
  pollTimer = setTimeout(() => void refresh(), 4000)
}

function follow() {
  const message = pending.value
  const id = conversation.value?.id
  schedulePoll()
  if (!message || !id || live.id === message.id) {
    return
  }
  controller?.abort()
  Object.assign(live, { id: message.id, text: '', stage: '', plan: [], steps: [] })
  controller = new AbortController()
  const { signal } = controller
  void streamReply(id, message.id, (event) => {
    if (event.type === 'stage') {
      live.stage = event.label
    }
    else if (event.type === 'plan') {
      live.plan = event.items
    }
    else if (event.type === 'step') {
      const index = live.steps.findIndex(step => step.index === event.step.index)
      live.steps = index < 0
        ? [...live.steps, event.step]
        : live.steps.map((step, position) => position === index ? event.step : step)
      void scrollToBottom()
    }
    else if (event.type === 'delta') {
      live.text += event.text
      live.stage = '正在输出回复…'
      void scrollToBottom()
    }
    else if (event.type === 'reset') {
      live.text = ''
    }
  }, signal).catch(() => {}).finally(() => {
    if (!signal.aborted) {
      void refresh()
    }
  })
}

async function load() {
  const id = storedId()
  loading.value = true
  const [models, saved] = await Promise.allSettled([
    chatApi.models(),
    id ? chatApi.conversation(id) : Promise.resolve(undefined),
  ])
  if (disposed) {
    return
  }
  loading.value = false
  if (models.status === 'fulfilled') {
    catalog.value = models.value
  }
  if (saved.status === 'fulfilled' && saved.value) {
    conversation.value = saved.value
    follow()
    void scrollToBottom()
  }
  else if (id) {
    remember('') // deleted in AI 对话, or no longer reachable: start fresh next time
  }
}

async function send(content = text.value) {
  const question = content.trim()
  if (!question || busy.value) {
    return
  }
  cancelVoice()
  sending.value = true
  error.value = ''
  // Show the question at once: choosing the tool takes a model call before the reply starts.
  outgoing.value = question
  text.value = ''
  void scrollToBottom()
  try {
    let id = conversation.value?.id
    if (!id) {
      const created = await chatApi.createConversation({})
      id = created.id
      conversation.value = created
      remember(id)
    }
    conversation.value = await chatApi.send(id, { content: question, attachment_ids: [], output: 'auto' })
    follow()
    void scrollToBottom()
  }
  catch (reason) {
    error.value = errorMessage(reason, '发送失败，请稍后重试')
    if (!text.value) {
      text.value = question // keep what was typed or said, so it can be sent again
    }
  }
  finally {
    outgoing.value = ''
    sending.value = false
    void nextTick(() => input.value?.focus())
  }
}

async function retry() {
  const id = conversation.value?.id
  if (!id || busy.value) {
    return
  }
  sending.value = true
  error.value = ''
  try {
    conversation.value = await chatApi.retry(id)
    follow()
  }
  catch (reason) {
    error.value = errorMessage(reason, '重试失败，请稍后再试')
  }
  finally {
    sending.value = false
  }
}

function startNew() {
  if (busy.value) {
    return
  }
  stopFollowing()
  conversation.value = undefined
  remember('')
  error.value = ''
  text.value = ''
  void nextTick(() => input.value?.focus())
}

function openInChat() {
  const id = conversation.value?.id
  void router.push(id ? { path: '/agent/content/chat', query: { conversation: id } } : '/agent/content/chat')
  emit('close')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !composing.value && !event.isComposing) {
    event.preventDefault()
    void send()
  }
}

function autoGrow() {
  const element = input.value
  if (element) {
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, 140)}px`
  }
}

function artifactIcon(artifact: ChatArtifact) {
  if (artifact.kind === 'image') {
    return Picture
  }
  return artifact.kind === 'video' ? VideoCamera : Document
}

function artifactLabel(artifact: ChatArtifact) {
  const kind = { doc: 'Word', docx: 'Word', pdf: 'PDF', image: '图片', video: '视频' }[artifact.kind]
  const size = artifact.size >= 1024 * 1024
    // eslint-disable-next-line financial/no-division -- File size for display, not financial arithmetic.
    ? `${(artifact.size / 1024 / 1024).toFixed(1)} MB`
    // eslint-disable-next-line financial/no-division -- File size for display, not financial arithmetic.
    : `${Math.max(1, Math.round(artifact.size / 1024))} KB`
  return `${kind} · ${size}`
}

async function download(artifact: ChatArtifact) {
  try {
    saveBlob(await chatApi.artifact(artifact.id), artifact.name)
  }
  catch (reason) {
    ElMessage.error(errorMessage(reason, '下载失败，请到 AI 对话中查看'))
  }
}

async function loadThumbnail(artifact: ChatArtifact) {
  if (artifact.kind !== 'image' || thumbnails.has(artifact.id)) {
    return
  }
  thumbnails.set(artifact.id, '')
  try {
    thumbnails.set(artifact.id, URL.createObjectURL(await chatApi.artifact(artifact.id)))
  }
  catch {
    thumbnails.delete(artifact.id)
  }
}

function routingLabel(message: ChatMessage) {
  const question = messages.value[messages.value.indexOf(message) - 1]
  const tool = question?.routing?.tool
  return tool ? TOOL_LABELS[tool] ?? '' : ''
}

watch(messages, (value) => {
  value.forEach(message => message.artifacts?.forEach(artifact => void loadThumbnail(artifact)))
}, { immediate: true })
watch([busy, unread], ([isBusy, isUnread]) => emit('status', { busy: isBusy, unread: isUnread }), { immediate: true })
watch(() => props.open, (open) => {
  if (open) {
    unread.value = false
    void scrollToBottom()
    void nextTick(() => input.value?.focus())
  }
  else {
    cancelVoice()
  }
})
watch(text, () => void nextTick(autoGrow))

onMounted(() => {
  void load()
  void nextTick(() => input.value?.focus())
})
onBeforeUnmount(() => {
  disposed = true
  cancelVoice()
  stopFollowing()
  thumbnails.forEach(url => url && URL.revokeObjectURL(url))
})
</script>

<template>
  <section
    class="assistant-panel"
    role="dialog"
    aria-label="运营助手"
    @keydown.esc.stop="emit('close')"
  >
    <header class="panel-head">
      <span class="panel-avatar" :class="{ 'is-busy': busy }" aria-hidden="true"><i /><i /><i /></span>
      <div class="panel-title">
        <strong>运营助手</strong>
        <span :title="statusLine">{{ statusLine }}</span>
      </div>
      <button
        type="button"
        class="head-button"
        title="新对话"
        aria-label="新对话"
        :disabled="busy"
        @click="startNew"
      >
        <ElIcon><Plus /></ElIcon>
      </button>
      <button
        type="button"
        class="head-button"
        title="在 AI 对话中查看"
        aria-label="在 AI 对话中查看"
        @click="openInChat"
      >
        <ElIcon><ChatDotRound /></ElIcon>
      </button>
      <button
        type="button"
        class="head-button"
        title="收起（Esc）"
        aria-label="收起"
        @click="emit('close')"
      >
        <ElIcon><Minus /></ElIcon>
      </button>
    </header>

    <div ref="list" class="panel-body" aria-live="polite">
      <div v-if="loading && !conversation" class="panel-empty">
        <ElIcon class="is-loading"><Loading /></ElIcon>
      </div>
      <div v-else-if="!messages.length && !outgoing" class="panel-welcome">
        <p class="welcome-title">你好，我是运营助手</p>
        <p class="welcome-text">
          可以帮你查发布记录、审核进度和内容库，给商品生成文案和宣传视频，把结果整理成 Word，或者准备好发布让你确认。打字或点麦克风说话都可以。
        </p>
        <div class="suggestions">
          <button
            v-for="item in SUGGESTIONS"
            :key="item"
            type="button"
            @click="send(item)"
          >
            {{ item }}
          </button>
        </div>
      </div>

      <template v-for="message in messages" :key="message.id">
        <div v-if="message.role === 'user'" class="bubble-row is-user">
          <div class="bubble">{{ message.content }}</div>
        </div>
        <div v-else class="bubble-row is-assistant">
          <div class="answer">
            <span v-if="routingLabel(message)" class="answer-tag">{{ routingLabel(message) }}</span>
            <template v-if="message.status === 'pending'">
              <AgentTrace
                v-if="live.id === message.id && (live.plan.length || live.steps.length)"
                :plan="live.plan"
                :steps="live.steps"
                live
              />
              <!-- eslint-disable-next-line vue/no-v-html -- markdown-it with html disabled -->
              <div v-if="live.id === message.id && live.text" class="markdown" v-html="renderMarkdown(live.text)" />
              <p v-else class="answer-waiting">
                <span class="dots" aria-hidden="true"><i /><i /><i /></span>
                {{ live.id === message.id && live.stage ? live.stage : '正在处理…' }}
              </p>
            </template>
            <template v-else-if="message.status === 'failed'">
              <p class="answer-error">{{ message.error || '这次没有完成' }}</p>
              <ElButton
                v-if="message === messages[messages.length - 1]"
                size="small"
                :icon="Refresh"
                :disabled="busy"
                @click="retry"
              >
                重试
              </ElButton>
            </template>
            <template v-else>
              <AgentTrace
                v-if="message.agent && (message.agent.plan.length || message.agent.steps.length)"
                :plan="message.agent.plan"
                :steps="message.agent.steps"
                :actions="message.agent.actions"
              />
              <!-- eslint-disable-next-line vue/no-v-html -- markdown-it with html disabled -->
              <div class="markdown" v-html="renderMarkdown(message.content)" />
              <div v-if="message.artifacts?.length" class="files">
                <button
                  v-for="artifact in message.artifacts"
                  :key="artifact.id"
                  type="button"
                  class="file"
                  :title="`下载 ${artifact.name}`"
                  @click="download(artifact)"
                >
                  <img v-if="thumbnails.get(artifact.id)" :src="thumbnails.get(artifact.id)" alt="">
                  <ElIcon v-else class="file-icon"><component :is="artifactIcon(artifact)" /></ElIcon>
                  <span class="file-text">
                    <strong>{{ artifact.name }}</strong>
                    <small>{{ artifactLabel(artifact) }}</small>
                  </span>
                  <ElIcon class="file-download"><Download /></ElIcon>
                </button>
              </div>
            </template>
          </div>
        </div>
      </template>
      <template v-if="outgoing">
        <div class="bubble-row is-user">
          <div class="bubble">{{ outgoing }}</div>
        </div>
        <div class="bubble-row is-assistant">
          <p class="answer-waiting">
            <span class="dots" aria-hidden="true"><i /><i /><i /></span>
            正在理解你的需求…
          </p>
        </div>
      </template>
    </div>

    <footer class="panel-foot">
      <p v-if="error" class="foot-error" role="alert">{{ error }}</p>
      <div class="composer" :class="{ 'is-listening': voiceActive }">
        <div v-if="voiceActive" class="listening" aria-live="polite">
          <span class="bars" aria-hidden="true"><i /><i /><i /><i /><i /></span>
          {{ voiceState === 'transcribing' ? '正在识别…' : `正在听，说完点麦克风结束 · ${voiceSeconds} 秒` }}
        </div>
        <textarea
          ref="input"
          v-model="text"
          rows="1"
          maxlength="4000"
          placeholder="说说你想做什么，比如：把今天的发布情况做成 Word"
          aria-label="给运营助手的消息"
          @keydown="onKeydown"
          @compositionstart="composing = true"
          @compositionend="composing = false"
        />
        <div class="composer-actions">
          <button
            v-if="voiceSupported"
            type="button"
            class="round-button mic"
            :class="{ 'is-active': voiceActive }"
            :aria-pressed="voiceActive"
            :aria-label="voiceActive ? '停止录音' : '语音输入'"
            :title="voiceActive ? '停止录音' : '语音输入'"
            :disabled="voiceState === 'transcribing'"
            @click="toggleVoice"
          >
            <ElIcon><Microphone /></ElIcon>
          </button>
          <button
            type="button"
            class="round-button send"
            aria-label="发送"
            title="发送（Enter）"
            :disabled="!text.trim() || busy"
            @click="send()"
          >
            <ElIcon :class="{ 'is-loading': sending }">
              <Loading v-if="sending" />
              <ArrowUp v-else />
            </ElIcon>
          </button>
        </div>
      </div>
      <p class="foot-note">对话会保存在「AI 对话」里；发布前都会请你确认。</p>
    </footer>
  </section>
</template>

<style scoped>
.assistant-panel {
  --ap-accent: var(--xi-primitive-color-brand-primary-7, #197465);
  --ap-accent-deep: var(--xi-primitive-color-brand-primary-9, #0e483e);
  --ap-accent-soft: var(--xi-primitive-color-brand-primary-2, #e7f5f2);
  display: flex;
  width: 100%;
  height: 100%;
  flex-direction: column;
  overflow: hidden;
  color: var(--xi-semantic-color-text-primary, #1f2329);
  font-size: 14px;
  background: rgb(255 255 255 / 92%);
  backdrop-filter: blur(24px) saturate(1.4);
  border: 1px solid rgb(255 255 255 / 70%);
  border-radius: 20px;
  box-shadow:
    0 24px 60px rgb(15 40 35 / 22%),
    0 4px 14px rgb(15 40 35 / 10%),
    inset 0 1px 0 rgb(255 255 255 / 80%);
}

/* header */
.panel-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 12px 12px 16px;
  color: #fff;
  background:
    radial-gradient(120% 140% at 0% 0%, rgb(255 255 255 / 18%), transparent 55%),
    linear-gradient(135deg, var(--ap-accent) 0%, var(--ap-accent-deep) 100%);
}
.panel-avatar {
  position: relative;
  display: grid;
  width: 34px;
  height: 34px;
  flex: none;
  place-items: center;
  background: rgb(255 255 255 / 14%);
  border-radius: 11px;
}
.panel-avatar i,
.panel-avatar i::before {
  position: absolute;
  border-radius: 50%;
}
.panel-avatar i:nth-child(1) {
  inset: 6px;
  border: 1.5px solid rgb(255 255 255 / 35%);
}
.panel-avatar i:nth-child(2) {
  inset: 10px;
  border: 1.5px solid rgb(255 255 255 / 60%);
}
.panel-avatar i:nth-child(3) {
  inset: 14px;
  background: #fff;
}
.panel-avatar.is-busy i:nth-child(1) {
  border-top-color: #fff;
  animation: ap-spin 1s linear infinite;
}
.panel-title {
  display: grid;
  min-width: 0;
  flex: 1;
  line-height: 1.3;
}
.panel-title strong {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.panel-title span {
  overflow: hidden;
  color: rgb(255 255 255 / 78%);
  font-size: 12px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.head-button {
  display: grid;
  width: 30px;
  height: 30px;
  flex: none;
  place-items: center;
  color: rgb(255 255 255 / 85%);
  font-size: 16px;
  background: transparent;
  border: 0;
  border-radius: 9px;
  cursor: pointer;
  transition: background 0.15s;
}
.head-button:hover:not(:disabled) {
  color: #fff;
  background: rgb(255 255 255 / 16%);
}
.head-button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.head-button:focus-visible {
  outline: 2px solid #fff;
  outline-offset: 1px;
}

/* conversation */
.panel-body {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  padding: 16px 14px;
  overscroll-behavior: contain;
  scroll-behavior: smooth;
}
.panel-empty {
  display: grid;
  flex: 1;
  place-items: center;
  color: var(--xi-semantic-color-text-tertiary, #8f959e);
  font-size: 20px;
}
.panel-welcome {
  margin: auto 0;
  padding: 4px 2px;
}
.welcome-title {
  margin: 0 0 6px;
  font-size: 17px;
  font-weight: 600;
}
.welcome-text {
  margin: 0 0 14px;
  color: var(--xi-semantic-color-text-secondary, #646a73);
  font-size: 13px;
  line-height: 1.7;
}
.suggestions {
  display: grid;
  gap: 8px;
}
.suggestions button {
  padding: 9px 12px;
  color: var(--ap-accent-deep);
  font: inherit;
  font-size: 13px;
  text-align: left;
  background: var(--ap-accent-soft);
  border: 1px solid transparent;
  border-radius: 12px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    transform 0.15s;
}
.suggestions button:hover {
  border-color: var(--xi-primitive-color-brand-primary-4, #b2ddd4);
  transform: translateX(2px);
}
.bubble-row {
  display: flex;
}
.bubble-row.is-user {
  justify-content: flex-end;
}
.bubble {
  max-width: 82%;
  padding: 9px 13px;
  color: #fff;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  background: linear-gradient(135deg, var(--ap-accent) 0%, var(--ap-accent-deep) 100%);
  border-radius: 16px 16px 4px 16px;
  box-shadow: 0 2px 8px rgb(14 72 62 / 18%);
}
.answer {
  min-width: 0;
  max-width: 100%;
  flex: 1;
}
.answer-tag {
  display: inline-block;
  margin-bottom: 6px;
  padding: 1px 8px;
  color: var(--ap-accent);
  font-size: 11px;
  background: var(--ap-accent-soft);
  border-radius: 999px;
}
.answer :deep(.agent-trace) {
  margin-top: 0;
  border-radius: 12px;
}
.markdown {
  padding: 10px 13px;
  line-height: 1.7;
  word-break: break-word;
  background: #fff;
  border: 1px solid var(--xi-semantic-color-border-subtle, #ebedf0);
  border-radius: 4px 16px 16px 16px;
}
.markdown :deep(p) {
  margin: 0 0 6px;
}
.markdown :deep(p:last-child) {
  margin-bottom: 0;
}
.markdown :deep(ul),
.markdown :deep(ol) {
  margin: 4px 0;
  padding-left: 20px;
}
.markdown :deep(table) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  font-size: 12px;
}
.markdown :deep(th),
.markdown :deep(td) {
  padding: 4px 8px;
  border: 1px solid var(--xi-semantic-color-border-subtle, #ebedf0);
}
.markdown :deep(a) {
  color: var(--ap-accent);
}
.answer-waiting {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  padding: 10px 13px;
  color: var(--xi-semantic-color-text-secondary, #646a73);
  font-size: 13px;
  background: #fff;
  border: 1px solid var(--xi-semantic-color-border-subtle, #ebedf0);
  border-radius: 4px 16px 16px 16px;
}
.dots {
  display: inline-flex;
  gap: 3px;
}
.dots i {
  width: 5px;
  height: 5px;
  background: var(--ap-accent);
  border-radius: 50%;
  animation: ap-bounce 1.2s infinite ease-in-out;
}
.dots i:nth-child(2) {
  animation-delay: 0.15s;
}
.dots i:nth-child(3) {
  animation-delay: 0.3s;
}
.answer-error {
  margin: 0 0 8px;
  padding: 9px 12px;
  color: var(--xi-semantic-color-feedback-danger-text, #c0392b);
  font-size: 13px;
  background: var(--xi-semantic-color-feedback-danger-background-subtle, #fdf0ef);
  border-radius: 12px;
}
.files {
  display: grid;
  gap: 6px;
  margin-top: 8px;
}
.file {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  color: inherit;
  font: inherit;
  text-align: left;
  background: #fff;
  border: 1px solid var(--xi-semantic-color-border-subtle, #ebedf0);
  border-radius: 12px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    box-shadow 0.15s;
}
.file:hover {
  border-color: var(--xi-primitive-color-brand-primary-4, #b2ddd4);
  box-shadow: 0 2px 10px rgb(14 72 62 / 8%);
}
.file img {
  width: 40px;
  height: 40px;
  flex: none;
  object-fit: cover;
  border-radius: 8px;
}
.file-icon {
  display: grid;
  width: 40px;
  height: 40px;
  flex: none;
  place-items: center;
  color: var(--ap-accent);
  font-size: 20px;
  background: var(--ap-accent-soft);
  border-radius: 10px;
}
.file-text {
  display: grid;
  min-width: 0;
  flex: 1;
}
.file-text strong {
  overflow: hidden;
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.file-text small {
  color: var(--xi-semantic-color-text-tertiary, #8f959e);
  font-size: 12px;
}
.file-download {
  color: var(--xi-semantic-color-text-tertiary, #8f959e);
}

/* composer */
.panel-foot {
  padding: 10px 12px 10px;
  background: linear-gradient(180deg, rgb(255 255 255 / 0%), rgb(255 255 255 / 90%) 30%);
  border-top: 1px solid var(--xi-semantic-color-border-subtle, #ebedf0);
}
.foot-error {
  margin: 0 0 8px;
  color: var(--xi-semantic-color-feedback-danger-text, #c0392b);
  font-size: 12px;
}
.composer {
  display: grid;
  gap: 6px;
  padding: 8px 8px 8px 12px;
  background: #fff;
  border: 1px solid var(--xi-semantic-color-border-default, #dee0e3);
  border-radius: 16px;
  transition:
    border-color 0.15s,
    box-shadow 0.15s;
}
.composer:focus-within {
  border-color: var(--ap-accent);
  box-shadow: 0 0 0 3px rgb(25 116 101 / 12%);
}
.composer.is-listening {
  border-color: var(--xi-semantic-color-feedback-danger-default, #e5484d);
  box-shadow: 0 0 0 3px rgb(229 72 77 / 12%);
}
.listening {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--xi-semantic-color-feedback-danger-text, #c0392b);
  font-size: 12px;
}
.bars {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 14px;
}
.bars i {
  width: 3px;
  height: 100%;
  background: currentcolor;
  border-radius: 2px;
  animation: ap-wave 0.9s infinite ease-in-out;
}
.bars i:nth-child(2) {
  animation-delay: 0.1s;
}
.bars i:nth-child(3) {
  animation-delay: 0.2s;
}
.bars i:nth-child(4) {
  animation-delay: 0.3s;
}
.bars i:nth-child(5) {
  animation-delay: 0.4s;
}
.composer textarea {
  width: 100%;
  min-height: 22px;
  max-height: 140px;
  padding: 2px 0;
  color: inherit;
  font: inherit;
  line-height: 1.6;
  background: transparent;
  border: 0;
  outline: none;
  resize: none;
}
.composer textarea::placeholder {
  color: var(--xi-semantic-color-text-tertiary, #8f959e);
}
.composer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.round-button {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  font-size: 16px;
  border: 0;
  border-radius: 50%;
  cursor: pointer;
  transition:
    background 0.15s,
    transform 0.1s,
    opacity 0.15s;
}
.round-button:active:not(:disabled) {
  transform: scale(0.94);
}
.round-button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.round-button:focus-visible {
  outline: 2px solid var(--ap-accent);
  outline-offset: 2px;
}
.mic {
  color: var(--xi-semantic-color-text-secondary, #646a73);
  background: var(--xi-semantic-color-background-page, #f5f6f7);
}
.mic:hover:not(:disabled) {
  color: var(--ap-accent);
  background: var(--ap-accent-soft);
}
.mic.is-active {
  color: #fff;
  background: var(--xi-semantic-color-feedback-danger-default, #e5484d);
  animation: ap-pulse 1.4s infinite;
}
.send {
  color: #fff;
  background: linear-gradient(135deg, var(--ap-accent) 0%, var(--ap-accent-deep) 100%);
}
.foot-note {
  margin: 6px 4px 0;
  color: var(--xi-semantic-color-text-tertiary, #8f959e);
  font-size: 11px;
}

@keyframes ap-spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes ap-bounce {
  0%,
  80%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }
  40% {
    opacity: 1;
    transform: translateY(-3px);
  }
}
@keyframes ap-wave {
  0%,
  100% {
    transform: scaleY(0.3);
  }
  50% {
    transform: scaleY(1);
  }
}
@keyframes ap-pulse {
  0% {
    box-shadow: 0 0 0 0 rgb(229 72 77 / 45%);
  }
  100% {
    box-shadow: 0 0 0 10px rgb(229 72 77 / 0%);
  }
}
@media (prefers-reduced-motion: reduce) {
  .panel-avatar.is-busy i:nth-child(1),
  .dots i,
  .bars i,
  .mic.is-active {
    animation: none;
  }
  .panel-body {
    scroll-behavior: auto;
  }
}
</style>
