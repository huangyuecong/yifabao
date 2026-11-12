<script setup lang="ts">
import type { AutomationPurpose, BrowserPlatformSession } from '@/api/agent/automation'
import { computed, onActivated, onBeforeUnmount, onDeactivated, ref } from 'vue'
import { automationApi } from '@/api/agent/automation'

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
async function operate(platform: BrowserPlatformSession['platform'], operation: 'login' | 'publish' | 'inspect') {
  if (!active || !props.enabled || pending.value) {
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
onActivated(() => {
  active = true
  void load()
})
onDeactivated(() => {
  active = false
  revision++
  pending.value = ''
  platforms.value.forEach(({ id }) => invalidate(id))
})
onBeforeUnmount(() => {
  active = false
  revision++
})
</script>

<template>
  <section class="browser-connections" aria-label="网页版平台登录">
    <div class="browser-heading"><strong>网页登录与发布</strong><span>登录状态保留在专用浏览器中</span></div>
    <article v-for="platform in platforms" :key="platform.id" class="browser-platform">
      <div class="platform-summary">
        <strong>{{ platform.label }}</strong>
        <ElTag size="small" :type="sessions[platform.id]?.state === 'logged_in' ? 'success' : 'info'">{{ labels[sessions[platform.id]?.state || 'not_opened'] }}</ElTag>
        <span v-if="sessions[platform.id]?.state === 'logged_in'">{{ sessions[platform.id]?.account_label || '已检测到登录账号' }}</span>
      </div>
      <div class="browser-actions">
        <ElButton :disabled="!enabled || !!pending" :loading="pending === `${platform.id}:login`" @click="operate(platform.id, 'login')">打开{{ platform.label }}登录</ElButton>
        <ElButton :disabled="!enabled || !!pending" :loading="pending === `${platform.id}:inspect`" @click="operate(platform.id, 'inspect')">检测登录</ElButton>
        <ElButton :disabled="!enabled || !!pending" :loading="pending === `${platform.id}:publish`" @click="operate(platform.id, 'publish')">打开发布页</ElButton>
      </div>
      <p v-if="notices[platform.id]" role="status">{{ notices[platform.id] }}</p>
      <p v-if="sessions[platform.id]?.state === 'logged_in' && !sessions[platform.id]?.publish_ready" class="publish-note">账号已连接，自动发布仍需完成平台适配。</p>
    </article>
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
</style>
