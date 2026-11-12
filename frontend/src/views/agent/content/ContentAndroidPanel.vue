<script setup lang="ts">
import type { AndroidScreen, AndroidScreenAction, AutomationDevice, AutomationPlatform, AutomationRun } from '@/api/agent/automation'
import { computed, onBeforeUnmount, onDeactivated, ref } from 'vue'
import { automationApi } from '@/api/agent/automation'

const props = defineProps<{ device?: AutomationDevice, enabled: boolean, run?: AutomationRun, platforms?: AutomationPlatform[] }>()
const emit = defineEmits<{ updated: [], published: [run: AutomationRun] }>()
const names: Record<AutomationPlatform, string> = { xianyu: '闲鱼', douyin: '抖音', xiaohongshu: '小红书', kuaishou: '快手' }
const labels: Record<string, string> = { not_installed: '未安装', unverified: '待登录检测', needs_consent: '待确认协议', login_required: '待登录', logged_in: '已登录' }
const opened = ref(false)
const busy = ref(false)
const refreshing = ref(false)
const verifying = ref(false)
const opening = ref(false)
const closing = ref(false)
const error = ref('')
const screen = ref<AndroidScreen>()
const sessionDevice = ref('')
const input = ref('')
const mediaConfirmed = ref(false)
const cleanupMessage = ref('')
let revision = 0
let pending: Promise<unknown> | undefined
const autoRefresh = ref(true)
const pointerInside = ref(false)
const inputFocused = ref(false)
const interactionPaused = computed(() => pointerInside.value || inputFocused.value)
const openPlatform = ref<AutomationPlatform>()
const visiblePlatforms = computed(() => props.run?.config.platforms || props.platforms || props.device?.platforms || [])
const screenTitle = computed(() => openPlatform.value ? `${names[openPlatform.value]} · 服务器安卓` : '安卓屏幕')
let timer: ReturnType<typeof setTimeout> | undefined
let settledFrameTimer: ReturnType<typeof setTimeout> | undefined
let needsSettledFrame = false
let gesture: { x: number, y: number, frame: string } | undefined

function reason(value: unknown) {
  return value instanceof Error ? value.message : '安卓操作未完成，请刷新后重试'
}
function isStaleFrame(value: unknown) {
  return !!value && typeof value === 'object' && 'code' in value && value.code === 'android_stale_frame'
}
async function recoverStaleFrame(current: number, instruction: string) {
  if (current !== revision || !screen.value || !opened.value) {
    return
  }
  refreshing.value = true
  mediaConfirmed.value = false
  try {
    // Only read the new screen. Coordinates, text and publication must be
    // confirmed again by the user against the refreshed frame.
    pending = automationApi.androidScreen(sessionDevice.value, screen.value.session_id)
    const result = await pending as AndroidScreen
    if (current === revision) {
      screen.value = result
      error.value = `画面已刷新，${instruction}`
    }
  }
  catch (value) {
    if (current === revision) {
      error.value = `画面已过期且刷新未完成，请点击“刷新画面”后重试。${reason(value)}`
      autoRefresh.value = false
    }
  }
  finally {
    if (current === revision) {
      refreshing.value = false
    }
  }
}
function space(bytes: number) {
  // eslint-disable-next-line financial/no-division -- Storage capacity, not a monetary value.
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}
async function cleanupMedia() {
  if (!props.device || busy.value || closing.value) {
    return
  }
  busy.value = true
  error.value = ''
  try {
    const result = await automationApi.cleanupAndroidMedia(props.device.device_id)
    cleanupMessage.value = `已清理 ${result.removed_files} 个过期副本`
    emit('updated')
  }
  catch (value) {
    error.value = reason(value)
  }
  finally {
    busy.value = false
  }
}
function schedule() {
  clearTimeout(timer)
  if (opened.value && screen.value && autoRefresh.value && !interactionPaused.value && !closing.value) {
    timer = setTimeout(() => void refresh(true), 3000)
  }
}
function cancelSettledFrame() {
  clearTimeout(settledFrameTimer)
  needsSettledFrame = false
}
function scheduleSettledFrame() {
  clearTimeout(settledFrameTimer)
  if (!needsSettledFrame || !opened.value || closing.value || inputFocused.value) {
    return
  }
  clearTimeout(timer)
  settledFrameTimer = setTimeout(() => {
    if (inputFocused.value || !opened.value || closing.value) {
      return
    }
    if (busy.value || gesture) {
      scheduleSettledFrame()
      return
    }
    needsSettledFrame = false
    void refresh()
  }, 700)
}
function pointerPresence(inside: boolean, event: PointerEvent) {
  if (event.pointerType === 'mouse' || event.pointerType === 'pen') {
    pointerInside.value = inside
    schedule()
  }
}
function inputFocus(focused: boolean) {
  inputFocused.value = focused
  schedule()
  scheduleSettledFrame()
}
async function open(platform: AutomationPlatform) {
  if (!props.device || busy.value || closing.value) {
    return
  }
  const current = ++revision
  cancelSettledFrame()
  busy.value = true
  opening.value = true
  opened.value = true
  autoRefresh.value = true
  pointerInside.value = false
  inputFocused.value = false
  screen.value = undefined
  error.value = ''
  mediaConfirmed.value = false
  sessionDevice.value = props.device.device_id
  openPlatform.value = platform
  try {
    const device = sessionDevice.value
    const response = props.run
      ? automationApi.openAndroidDraft(device, props.run.id, platform)
      : automationApi.openAndroid(device, platform)
    pending = response
    const result = await response
    if (current !== revision) {
      return
    }
    screen.value = result
  }
  catch (value) {
    if (current === revision) {
      error.value = reason(value)
    }
  }
  finally {
    if (current === revision) {
      busy.value = false
      opening.value = false
      schedule()
    }
  }
}
async function refresh(background = false) {
  if (!screen.value || !opened.value) {
    return
  }
  if (background && interactionPaused.value) {
    return
  }
  if (busy.value || gesture) {
    schedule()
    return
  }
  busy.value = true
  refreshing.value = true
  const current = revision
  try {
    pending = automationApi.androidScreen(sessionDevice.value, screen.value.session_id)
    const result = await pending as AndroidScreen
    if (current === revision) {
      screen.value = result
      error.value = ''
    }
  }
  catch (value) {
    if (current === revision) {
      error.value = reason(value)
      autoRefresh.value = false
    }
  }
  finally {
    if (current === revision) {
      busy.value = false
      refreshing.value = false
      schedule()
    }
  }
}
async function action(value: Omit<AndroidScreenAction, 'frame_id'>, frame?: string) {
  if (!screen.value || busy.value) {
    return
  }
  clearTimeout(timer)
  cancelSettledFrame()
  busy.value = true
  error.value = ''
  const current = revision
  try {
    pending = automationApi.androidAction(sessionDevice.value, screen.value.session_id, { ...value, frame_id: frame || screen.value.frame_id })
    const result = await pending as AndroidScreen
    if (current === revision) {
      screen.value = result
      mediaConfirmed.value = false
      needsSettledFrame = true
    }
    if (current === revision && value.kind === 'text') {
      input.value = ''
    }
  }
  catch (failure) {
    if (isStaleFrame(failure)) {
      await recoverStaleFrame(current, value.kind === 'text' ? '输入内容已保留，请确认输入框后再次发送。' : '请核对当前页面后再次点击或滑动。')
    }
    else if (current === revision) {
      error.value = reason(failure)
    }
  }
  finally {
    if (current === revision) {
      busy.value = false
      schedule()
      scheduleSettledFrame()
    }
  }
}
function point(event: PointerEvent) {
  if (!screen.value) {
    return { x: 0, y: 0 }
  }
  const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect()
  return {
    // eslint-disable-next-line financial/no-division -- Screen coordinates, not monetary values.
    x: Math.max(0, Math.min(screen.value.width - 1, Math.round((event.clientX - bounds.left) * screen.value.width / bounds.width))),
    // eslint-disable-next-line financial/no-division -- Screen coordinates, not monetary values.
    y: Math.max(0, Math.min(screen.value.height - 1, Math.round((event.clientY - bounds.top) * screen.value.height / bounds.height))),
  }
}
function pointerDown(event: PointerEvent) {
  if (busy.value || !screen.value) {
    return
  }
  clearTimeout(timer)
  gesture = { ...point(event), frame: screen.value.frame_id }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function pointerUp(event: PointerEvent) {
  if (!gesture) {
    return
  }
  const start = gesture
  const end = point(event)
  gesture = undefined
  if (Math.abs(start.x - end.x) + Math.abs(start.y - end.y) > 25) {
    void action({ kind: 'swipe', x: start.x, y: start.y, end_x: end.x, end_y: end.y }, start.frame)
  }
  else {
    void action({ kind: 'tap', x: start.x, y: start.y }, start.frame)
  }
}
function pointerCancel() {
  gesture = undefined
  schedule()
}
async function verify() {
  if (!screen.value || busy.value) {
    return
  }
  busy.value = true
  verifying.value = true
  error.value = ''
  clearTimeout(timer)
  try {
    const current = revision
    pending = automationApi.verifyAndroid(sessionDevice.value, screen.value.session_id)
    const result = await pending as AndroidScreen
    if (current === revision) {
      screen.value = result
    }
    emit('updated')
  }
  catch (value) {
    error.value = reason(value)
  }
  finally {
    busy.value = false
    verifying.value = false
    schedule()
  }
}
async function close() {
  if (closing.value || (!opened.value && !screen.value && !pending)) {
    return
  }
  closing.value = true
  revision++
  clearTimeout(timer)
  cancelSettledFrame()
  autoRefresh.value = true
  pointerInside.value = false
  inputFocused.value = false
  opened.value = false
  input.value = ''
  gesture = undefined
  const current = screen.value
  const request = pending
  const device = sessionDevice.value
  screen.value = undefined
  try {
    // An App can finish opening after the drawer was dismissed. Release that
    // session too, otherwise the other business page cannot use the device.
    const result = await request?.catch(() => undefined)
    const token = current?.session_id
      || (result && typeof result === 'object' && 'session_id' in result ? String(result.session_id) : '')
    if (device && token) {
      await automationApi.closeAndroid(device, token)
    }
  }
  catch (value) {
    error.value = reason(value)
  }
  finally {
    busy.value = false
    refreshing.value = false
    verifying.value = false
    opening.value = false
    pending = undefined
    closing.value = false
  }
  emit('updated')
}
async function fillDraft() {
  if (!screen.value || busy.value) {
    return
  }
  busy.value = true
  clearTimeout(timer)
  const current = revision
  try {
    pending = automationApi.fillAndroidDraft(sessionDevice.value, screen.value.session_id)
    const result = await pending as AndroidScreen
    if (current === revision) {
      screen.value = result
      error.value = ''
      mediaConfirmed.value = false
    }
  }
  catch (value) {
    error.value = reason(value)
  }
  finally {
    busy.value = false
    schedule()
  }
}
async function publishDraft() {
  if (!screen.value?.draft || !mediaConfirmed.value || busy.value) {
    return
  }
  clearTimeout(timer)
  busy.value = true
  const current = revision
  try {
    pending = automationApi.publishAndroidDraft(sessionDevice.value, screen.value.session_id, screen.value.draft.run_id, screen.value.frame_id)
    const result = await pending as AutomationRun
    if (current === revision) {
      screen.value = undefined
      opened.value = false
      input.value = ''
      emit('published', result)
      emit('updated')
    }
  }
  catch (value) {
    if (isStaleFrame(value)) {
      await recoverStaleFrame(current, '请重新核对账号、素材和发布信息，再点击发布。')
    }
    else if (current === revision) {
      error.value = reason(value)
    }
    if (current === revision) {
      autoRefresh.value = false
    }
  }
  finally {
    if (current === revision) {
      busy.value = false
      schedule()
    }
  }
}
onBeforeUnmount(() => void close())
onDeactivated(() => void close())
</script>

<template>
  <div class="android-platforms">
    <details v-if="device?.storage && !run" class="storage-summary">
      <summary>素材空间 <span>剩余 {{ space(device.storage.free_bytes) }} · 已用 {{ space(device.storage.cache_bytes) }}</span></summary>
      <div class="storage-details">
        <p>临时素材上限 {{ space(device.storage.cache_limit_bytes) }}。相同文件复用，发布成功后清理副本；未使用副本保留 {{ device.storage.retention_hours }} 小时。编辑中或结果待核实的素材保留。</p>
        <ElButton size="small" :disabled="!enabled || busy || closing || device.busy || !device.connected" @click="cleanupMedia">清理过期素材</ElButton>
        <span v-if="cleanupMessage" role="status">{{ cleanupMessage }}</span>
      </div>
    </details>
    <article v-for="platform in visiblePlatforms" :key="platform" class="android-platform">
      <div class="app-heading">
        <span class="app-mark" aria-hidden="true">{{ names[platform].slice(0, 1) }}</span>
        <strong>{{ names[platform] }}</strong>
        <ElTag size="small" :type="device?.apps?.[platform]?.state === 'logged_in' ? 'success' : 'info'">
          {{ device?.apps?.[platform]?.runtime_state === 'exited' ? '启动异常' : labels[device?.apps?.[platform]?.state || 'unverified'] }}
        </ElTag>
      </div>
      <p class="app-account">{{ device?.apps?.[platform]?.error || device?.apps?.[platform]?.account_label || '登录后绑定发布账号' }}</p>
      <ElButton :loading="(opening || closing) && openPlatform === platform" :disabled="!enabled || busy || closing || !device?.configured || run?.platform_results[platform]?.status === 'succeeded'" @click="open(platform)">{{ run ? '导入草稿到' : '打开' }}{{ names[platform] }}</ElButton>
    </article>
    <p v-if="error && !opened" class="android-error" role="alert">{{ error }}</p>
    <ElDrawer
      :model-value="opened"
      :title="screenTitle"
      size="min(940px, 100vw)"
      :close-on-click-modal="false"
      @close="close"
    >
      <div
        v-if="!screen"
        class="android-connection-state"
        role="status"
        :aria-busy="opening"
      >
        <template v-if="opening">
          <span class="connection-spinner" aria-hidden="true" />
          <strong>{{ run ? '正在导入草稿并打开 App' : `正在连接设备并打开${openPlatform ? names[openPlatform] : ' App'}` }}</strong>
          <p>首次连接需要启动远程画面，请稍候。连接后可直接在这里操作。</p>
        </template>
        <template v-else>
          <strong>连接未完成</strong>
          <p class="android-error" role="alert">{{ error || '未收到安卓画面，请重新连接。' }}</p>
          <ElButton
            v-if="openPlatform"
            type="primary"
            :disabled="closing"
            @click="open(openPlatform)"
          >
            重新连接
          </ElButton>
        </template>
      </div>
      <div
        v-else
        class="android-console"
        @pointerenter="pointerPresence(true, $event)"
        @pointerleave="pointerPresence(false, $event)"
      >
        <div class="android-screen">
          <img
            v-if="screen"
            :src="screen.image"
            :alt="`${screenTitle}实时画面，可点击和滑动`"
            draggable="false"
            @pointerdown="pointerDown"
            @pointerup="pointerUp"
            @pointercancel="pointerCancel"
          >
          <span v-if="busy" class="screen-busy">{{ refreshing ? '正在刷新画面…' : '正在操作…' }}</span>
        </div>
        <div class="android-controls">
          <p>{{ run ? '核对本次素材和发布信息，可在左侧屏幕补充 App 要求的字段。' : '直接点击或滑动左侧屏幕完成登录。' }}</p>
          <div class="screen-buttons">
            <ElButton :disabled="busy" @click="action({ kind: 'back' })">返回</ElButton>
            <ElButton :disabled="busy" @click="action({ kind: 'home' })">桌面</ElButton>
            <ElButton :disabled="busy" :loading="refreshing" @click="refresh()">刷新画面</ElButton>
            <ElButton :disabled="busy" @click="action({ kind: 'reopen' })">重新打开 App</ElButton>
          </div>
          <ElSwitch v-model="autoRefresh" active-text="自动刷新" @change="schedule" />
          <span v-if="autoRefresh && interactionPaused" class="screen-note">操作期间暂停自动刷新，离开操作区后恢复。</span>
          <ElInput
            v-model="input"
            type="textarea"
            :rows="3"
            maxlength="2000"
            placeholder="先点击安卓输入框，再在这里输入文字或验证码"
            autocomplete="off"
            @focus="inputFocus(true)"
            @blur="inputFocus(false)"
          />
          <ElButton :disabled="busy || !input" @click="action({ kind: 'text', text: input })">填写到当前输入框</ElButton>
          <template v-if="screen?.draft">
            <p>{{ screen.draft.note }}</p>
            <details><summary>本次素材（{{ screen.draft.media_count }} 个）</summary><p v-for="file in screen.draft.files" :key="file">{{ file }}</p></details>
            <details><summary>草稿内容</summary><p v-for="(value, key) in screen.draft.fields" :key="key">{{ value }}</p></details>
            <ElButton :disabled="busy" @click="fillDraft">填写草稿文案</ElButton>
            <ElCheckbox v-model="mediaConfirmed" :disabled="busy">已核对账号、本次图片或视频及发布信息</ElCheckbox>
            <ElButton type="primary" :disabled="busy || !mediaConfirmed" @click="publishDraft">发布到{{ names[screen.platform] }}</ElButton>
          </template>
          <template v-else>
            <p>登录后进入自己的个人主页，显示账号 ID 和编辑资料入口，再检测绑定。</p>
            <ElButton
              type="primary"
              :disabled="busy"
              :loading="verifying"
              @click="verify"
            >
              检测并绑定登录账号
            </ElButton>
          </template>
          <p v-if="screen?.app.account_label" class="account-bound">{{ screen.app.account_label }}</p>
          <p v-if="screen?.app.error" class="android-error">{{ screen.app.error }}</p>
          <p v-if="error" class="android-error" role="alert">{{ error }}</p>
          <p class="screen-note">完成后关闭此屏幕，即可让工作流使用这台设备。</p>
        </div>
      </div>
    </ElDrawer>
  </div>
</template>

<style scoped>
.android-platforms {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
}
.storage-summary {
  order: 1;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  padding-top: 6px;
}
.storage-summary summary {
  padding: 8px 0;
  cursor: pointer;
  line-height: 1.6;
}
.storage-summary summary span {
  margin-left: 12px;
}
.storage-details {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding: 0 0 8px;
}
.storage-details p {
  flex-basis: 100%;
  margin: 4px 0;
  line-height: 1.7;
}
.android-platform {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  padding: 12px;
  border: 0;
  border-radius: 8px;
  background: var(--el-fill-color-light);
}
.app-heading {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 13px;
}
.app-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-weight: 600;
}
.app-account {
  margin: 0;
}
.android-platform p,
.android-controls p {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.android-console {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(240px, 1fr);
  gap: 24px;
}
.android-connection-state {
  display: flex;
  min-height: 320px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 24px;
  text-align: center;
}
.android-connection-state p {
  max-width: 480px;
  margin: 0;
  color: var(--el-text-color-secondary);
  line-height: 1.7;
}
.connection-spinner {
  width: 28px;
  height: 28px;
  border: 3px solid var(--el-border-color-light);
  border-top-color: var(--el-color-primary);
  border-radius: 50%;
  animation: connection-spin 0.8s linear infinite;
}
@keyframes connection-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .connection-spinner {
    animation: none;
  }
}
.android-screen {
  position: relative;
  text-align: center;
  background: #111827;
  border-radius: 12px;
  overflow: hidden;
  align-self: start;
}
.android-screen img {
  display: block;
  max-height: 74vh;
  max-width: 100%;
  margin: auto;
  touch-action: none;
  cursor: pointer;
  user-select: none;
}
.screen-busy {
  position: absolute;
  bottom: 8px;
  left: 50%;
  transform: translateX(-50%);
  color: white;
  background: #334155;
  border-radius: 5px;
  padding: 4px 12px;
  pointer-events: none;
}
.android-controls {
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: stretch;
}
.screen-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.android-controls .el-button {
  margin-left: 0;
}
.android-error {
  color: var(--el-color-danger) !important;
  grid-column: 1 / -1;
}
.account-bound {
  color: var(--el-color-success) !important;
}
.screen-note {
  font-size: 12px !important;
}
@media (max-width: 900px) {
  .android-platform {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .app-account {
    grid-column: 1 / -1;
    grid-row: 2;
  }
}
@media (max-width: 640px) {
  .android-console {
    grid-template-columns: 1fr;
  }
  .android-screen img {
    max-height: 54vh;
  }
}
</style>
