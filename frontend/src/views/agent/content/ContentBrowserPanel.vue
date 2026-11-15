<script setup lang="ts">
import type { AutomationPurpose, BrowserLoginStatus, BrowserPlatformSession } from '@/api/agent/automation'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onBeforeUnmount, onDeactivated, ref } from 'vue'
import { automationApi } from '@/api/agent/automation'
import { remoteLoginApi, trialMe } from '@/api/agent/trial'
import RemoteLoginDialog from './RemoteLoginDialog.vue'

const props = withDefaults(defineProps<{ enabled: boolean, purpose?: AutomationPurpose }>(), { purpose: 'promotion' })
const emit = defineEmits<{ updated: [session: BrowserPlatformSession] }>()
const platforms = computed(() => props.purpose === 'listing'
  ? [{ id: 'xianyu', label: '闲鱼' }] as const
  : [
      { id: 'douyin', label: '抖音' },
      { id: 'xiaohongshu', label: '小红书' },
      { id: 'kuaishou', label: '快手' },
    ] as const)
const labels = { not_opened: '待打开', login_required: '待登录', logged_in: '已登录', unknown: '待检测登录', unavailable: '暂未连接' }
const sessions = ref<Partial<Record<BrowserPlatformSession['platform'], BrowserPlatformSession>>>({})
const pending = ref('')
const notices = ref<Partial<Record<BrowserPlatformSession['platform'], string>>>({})
// Online trial: the browser is on the server, so logging in shows its screen here instead.
const remote = computed(() => !!trialMe.value.remote_browser)
const remoteFor = ref<BrowserPlatformSession['platform'] | ''>('')
const remoteLabel = computed(() => platforms.value.find(item => item.id === remoteFor.value)?.label ?? '')
let revision = 0
let active = true
const requestVersions: Record<BrowserPlatformSession['platform'], number> = { xianyu: 0, douyin: 0, xiaohongshu: 0, kuaishou: 0 }
function update(session: BrowserPlatformSession) {
  sessions.value = { ...sessions.value, [session.platform]: session }
  emit('updated', session)
}
function invalidate(platform: BrowserPlatformSession['platform'], state: 'unknown' | 'unavailable' = 'unknown', message = '') {
  update({ platform, connected: false, state, publish_ready: false, message })
  notices.value[platform] = message
}
function isCurrent(platform: BrowserPlatformSession['platform'], current: number, request: number) {
  return active && current === revision && requestVersions[platform] === request
}
async function load() {
  const current = revision
  const requests = platforms.value.map(({ id }) => {
    const request = ++requestVersions[id]
    invalidate(id)
    return { id, request }
  })
  try {
    const result = await automationApi.browserSessions(props.purpose)
    for (const { id, request } of requests) {
      if (isCurrent(id, current, request)) {
        const session = result.items.find(item => item.platform === id)
        if (session) {
          update(session)
          notices.value[id] = session.state === 'unavailable' ? session.message || '' : ''
        }
        else {
          invalidate(id, 'unavailable', '暂未取得此平台状态，请检测登录。')
        }
      }
    }
  }
  catch (value) {
    for (const { id, request } of requests) {
      if (isCurrent(id, current, request)) {
        invalidate(id, 'unavailable', value instanceof Error ? value.message : '网页登录状态暂时无法读取')
      }
    }
  }
}
async function forgetLogins() {
  try {
    await ElMessageBox.confirm('关闭你在服务器上的无痕窗口，删除你在闲鱼、抖音、小红书、快手的全部登录信息？之后需要重新扫码。', '清除平台登录', {
      type: 'warning',
      confirmButtonText: '清除',
      cancelButtonText: '取消',
    })
  }
  catch {
    return
  }
  try {
    await remoteLoginApi.forget()
    ElMessage.success('已清除服务器上的平台登录')
    void load()
  }
  catch (value) {
    ElMessage.error(value instanceof Error ? value.message : '清除失败')
  }
}
async function operate(platform: BrowserPlatformSession['platform'], operation: 'login' | 'publish' | 'inspect') {
  if (!active || !props.enabled || pending.value) {
    return
  }
  if (remote.value && operation === 'login') {
    remoteFor.value = platform
    return
  }
  const current = revision
  const request = ++requestVersions[platform]
  pending.value = `${platform}:${operation}`
  invalidate(platform)
  try {
    const session = await (operation === 'inspect' ? automationApi.inspectBrowser(platform) : automationApi.openBrowser(platform, operation))
    if (isCurrent(platform, current, request)) {
      update(session)
      notices.value[platform] = session.state === 'unavailable'
        ? session.message || '浏览器暂未连接，请重试。'
        : operation === 'inspect'
          ? session.state === 'logged_in' ? '登录已确认，可使用此网页账号。' : session.message || '尚未确认登录，请在打开的网页完成登录后再次检测。'
          : operation === 'login' ? '已打开网页登录窗口。完成登录后，点击“检测登录”。' : '已打开网页发布页，可核对平台要求。'
    }
  }
  catch (value) {
    if (isCurrent(platform, current, request)) {
      invalidate(platform, 'unavailable', value instanceof Error ? value.message : '网页操作未完成，请重试')
    }
  }
  finally {
    if (current === revision) {
      pending.value = ''
    }
  }
}
// Login monitor: the server re-checks logged-in platforms in the background, so an
// expired login shows here before a publish fails.
const monitor = ref<Partial<Record<BrowserPlatformSession['platform'], BrowserLoginStatus>>>({})
const lost = computed(() => platforms.value.filter(({ id }) => monitor.value[id]?.lost_at && monitor.value[id]?.state !== 'logged_in'))
let monitorTimer: ReturnType<typeof setInterval> | undefined
async function loadMonitor() {
  try {
    const result = await automationApi.browserMonitor()
    monitor.value = Object.fromEntries(result.items.map(item => [item.platform, item]))
  }
  catch {}
}
function checkedAgo(platform: BrowserPlatformSession['platform']) {
  const age = monitor.value[platform]?.age_seconds
  if (age === undefined) {
    return ''
  }
  // eslint-disable-next-line financial/no-division -- Seconds to minutes for display, not financial arithmetic.
  return age < 60 ? '刚刚检测' : `${Math.round(age / 60)} 分钟前检测`
}
async function clearLogin(platform: BrowserPlatformSession['platform'], label: string) {
  if (!active || pending.value) {
    return
  }
  try {
    await ElMessageBox.confirm(`清除发布浏览器里${label}的登录信息（Cookie 和网站缓存）？其他平台不受影响，之后可以重新登录，也可以换一个${label}账号。`, '清除登录缓存', {
      type: 'warning',
      confirmButtonText: '清除',
      cancelButtonText: '取消',
    })
  }
  catch {
    return
  }
  pending.value = `${platform}:clear`
  try {
    const session = await automationApi.clearBrowserLogin(platform)
    update(session)
    notices.value[platform] = session.message || '已清除登录缓存'
    void loadMonitor()
  }
  catch (value) {
    ElMessage.error(value instanceof Error ? value.message : '清除失败')
  }
  finally {
    pending.value = ''
  }
}
onActivated(() => {
  active = true
  void loadMonitor()
  void load().then(loadMonitor)
  clearInterval(monitorTimer)
  monitorTimer = setInterval(() => void loadMonitor(), 60_000)
})
onDeactivated(() => {
  clearInterval(monitorTimer)
  active = false
  revision++
  pending.value = ''
  platforms.value.forEach(({ id }) => invalidate(id))
})
onBeforeUnmount(() => {
  clearInterval(monitorTimer)
  active = false
  revision++
})
</script>

<template>
  <section class="browser-connections" aria-label="网页版平台登录">
    <div class="browser-heading">
      <strong>网页登录与发布</strong><span>{{ remote ? '在线试用：服务器浏览器给每个试用账号单独开一个无痕窗口，用手机扫码登录。闲置 45 分钟或清除后，登录信息全部删除' : '登录状态保留在专用浏览器中' }}</span>
    </div>
    <ElAlert
      v-if="lost.length"
      type="error"
      :closable="false"
      show-icon
      :title="`${lost.map(item => item.label).join('、')}的登录已失效，请重新登录后再发布`"
    />
    <article v-for="platform in platforms" :key="platform.id" class="browser-platform">
      <div class="platform-summary">
        <strong>{{ platform.label }}</strong>
        <ElTag size="small" :type="sessions[platform.id]?.state === 'logged_in' ? 'success' : 'info'">{{ labels[sessions[platform.id]?.state || 'not_opened'] }}</ElTag>
        <span v-if="sessions[platform.id]?.state === 'logged_in'">{{ sessions[platform.id]?.account_label || '已检测到登录账号' }}</span>
        <small v-if="checkedAgo(platform.id)" class="monitor" title="登录监控每隔几分钟自动检测一次已登录的平台">{{ checkedAgo(platform.id) }}</small>
      </div>
      <div class="browser-actions">
        <ElButton :disabled="!enabled || !!pending" :loading="pending === `${platform.id}:login`" @click="operate(platform.id, 'login')">{{ remote ? `扫码登录${platform.label}` : `打开${platform.label}登录` }}</ElButton>
        <ElButton :disabled="!enabled || !!pending" :loading="pending === `${platform.id}:inspect`" @click="operate(platform.id, 'inspect')">检测登录</ElButton>
        <ElButton
          v-if="!remote"
          :disabled="!enabled || !!pending"
          :loading="pending === `${platform.id}:publish`"
          @click="operate(platform.id, 'publish')"
        >
          打开发布页
        </ElButton>
        <ElButton
          :disabled="!enabled || !!pending"
          :loading="pending === `${platform.id}:clear`"
          @click="clearLogin(platform.id, platform.label)"
        >
          清除登录缓存
        </ElButton>
      </div>
      <p v-if="notices[platform.id]" role="status">{{ notices[platform.id] }}</p>
      <p v-if="sessions[platform.id]?.state === 'logged_in' && !sessions[platform.id]?.publish_ready" class="publish-note">账号已连接，自动发布仍需完成平台适配。</p>
    </article>
    <ElButton
      v-if="remote"
      link
      class="forget"
      @click="forgetLogins"
    >
      清除全部平台的登录（关闭我的无痕窗口）
    </ElButton>
    <RemoteLoginDialog
      :platform="remoteFor"
      :label="remoteLabel"
      @updated="update"
      @close="remoteFor = ''"
    />
  </section>
</template>

<style scoped>
.browser-connections {
  display: grid;
  gap: 12px;
  margin: 12px 0 20px;
}
.browser-heading,
.platform-summary,
.browser-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}
.browser-heading {
  justify-content: space-between;
}
.browser-heading span,
.platform-summary span,
.browser-platform p {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}
.browser-platform {
  padding: 16px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 10px;
  background: var(--el-fill-color-blank);
}
.browser-actions {
  margin-top: 12px;
}
.browser-actions .el-button {
  margin: 0;
}
.browser-platform p {
  margin: 10px 0 0;
}
.monitor {
  margin-left: auto;
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.forget {
  justify-self: start;
  font-size: 12px;
}
</style>
