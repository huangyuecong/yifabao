<script setup lang="ts">
import type { BrowserPlatformSession } from '@/api/agent/automation'
import { ElMessage } from 'element-plus'
import { onBeforeUnmount, ref, watch } from 'vue'
import { automationApi } from '@/api/agent/automation'
import { remoteLoginApi } from '@/api/agent/trial'

/* Online trial: the platform's login page runs in a browser on the server. This shows that
 * page live; the tester scans its QR code with their phone, and can click on the picture or
 * type (e.g. a verification code) to reach the page. */
const props = defineProps<{ platform: BrowserPlatformSession['platform'] | '', label: string }>()
const emit = defineEmits<{ close: [], updated: [session: BrowserPlatformSession] }>()

const image = ref('')
const starting = ref(false)
const problem = ref('')
const typed = ref('')
const checking = ref(false)
const sending = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let alive = false
let polls = 0

function release() {
  if (image.value) {
    URL.revokeObjectURL(image.value)
  }
}

async function refresh() {
  if (!alive || !props.platform) {
    return
  }
  try {
    const blob = await remoteLoginApi.screen(props.platform)
    if (!alive) {
      return
    }
    release()
    image.value = URL.createObjectURL(blob)
    problem.value = ''
  }
  catch (error) {
    problem.value = error instanceof Error ? error.message : '暂时取不到画面'
  }
  polls += 1
  // Every few pictures, check whether the scan has signed the account in.
  if (polls % 4 === 0) {
    void inspect(true)
  }
  if (alive) {
    timer = setTimeout(refresh, 1500)
  }
}

async function open() {
  if (!props.platform) {
    return
  }
  alive = true
  polls = 0
  starting.value = true
  problem.value = ''
  try {
    const session = await remoteLoginApi.start(props.platform) as BrowserPlatformSession
    if (session?.state === 'unavailable') {
      problem.value = session.message || '服务器浏览器暂时打不开'
    }
  }
  catch (error) {
    problem.value = error instanceof Error ? error.message : '服务器浏览器暂时打不开'
  }
  finally {
    starting.value = false
  }
  void refresh()
}

function stop() {
  alive = false
  clearTimeout(timer)
  release()
  image.value = ''
}

async function inspect(quiet = false) {
  if (!props.platform || checking.value) {
    return
  }
  checking.value = true
  try {
    const session = await automationApi.inspectBrowser(props.platform)
    emit('updated', session)
    if (session.state === 'logged_in') {
      ElMessage.success(`${props.label}已登录`)
      emit('close')
    }
    else if (!quiet) {
      ElMessage.info(session.message || '还没有登录，请用手机扫码')
    }
  }
  catch (error) {
    if (!quiet) {
      ElMessage.error(error instanceof Error ? error.message : '检测失败')
    }
  }
  finally {
    checking.value = false
  }
}

async function clickAt(event: MouseEvent) {
  if (!props.platform) {
    return
  }
  const box = (event.currentTarget as HTMLElement).getBoundingClientRect()
  // Screen coordinates as a fraction of the picture, not money.
  // eslint-disable-next-line financial/no-division
  const x = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width))
  // eslint-disable-next-line financial/no-division
  const y = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height))
  try {
    await remoteLoginApi.click(props.platform, x, y)
    clearTimeout(timer)
    timer = setTimeout(refresh, 500)
  }
  catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '点击没有送达')
  }
}

async function send(key?: 'Enter' | 'Backspace') {
  if (!props.platform || sending.value || (!typed.value && !key)) {
    return
  }
  sending.value = true
  try {
    await remoteLoginApi.type(props.platform, typed.value, key)
    typed.value = ''
    clearTimeout(timer)
    timer = setTimeout(refresh, 500)
  }
  catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '输入没有送达')
  }
  finally {
    sending.value = false
  }
}

watch(() => props.platform, (value) => {
  stop()
  if (value) {
    void open()
  }
}, { immediate: true })
onBeforeUnmount(stop)
</script>

<template>
  <ElDialog
    :model-value="!!platform"
    :title="`${label}扫码登录`"
    width="min(920px, 96vw)"
    append-to-body
    destroy-on-close
    @close="emit('close')"
  >
    <p class="hint">
      下面是服务器上的{{ label }}登录页。用手机{{ label }} App 扫描画面里的二维码；需要切换到“扫码登录”或点按钮时，直接点画面。
      登录信息只保存在服务器上你的试用账号名下，可随时在“平台账号”里清除。
    </p>
    <div v-loading="starting && !image" class="screen" element-loading-text="正在打开服务器浏览器…">
      <img
        v-if="image"
        :src="image"
        alt="服务器浏览器画面"
        draggable="false"
        @click="clickAt"
      >
      <p v-else-if="problem" class="problem">{{ problem }}</p>
    </div>
    <p v-if="problem && image" class="problem small">{{ problem }}</p>
    <div class="input-row">
      <ElInput
        v-model="typed"
        maxlength="64"
        placeholder="需要输入验证码或手机号时：先点画面里的输入框，再在这里输入"
        @keydown.enter.prevent="send('Enter')"
      />
      <ElButton :loading="sending" :disabled="!typed" @click="send()">输入</ElButton>
      <ElButton :disabled="sending" @click="send('Backspace')">退格</ElButton>
    </div>
    <template #footer>
      <ElButton @click="emit('close')">关闭</ElButton>
      <ElButton type="primary" :loading="checking" @click="inspect()">我已扫码，检测登录</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}
.screen {
  display: grid;
  min-height: 360px;
  place-items: center;
  overflow: hidden;
  background: #f2f3f5;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
}
.screen img {
  display: block;
  width: 100%;
  height: auto;
  cursor: pointer;
  user-select: none;
}
.problem {
  margin: 0;
  padding: 24px;
  color: var(--el-color-danger);
  font-size: 13px;
  text-align: center;
}
.problem.small {
  padding: 6px 0 0;
  text-align: left;
}
.input-row {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.input-row .el-button {
  margin: 0;
}
</style>
