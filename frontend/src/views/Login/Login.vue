<script lang="ts" setup>
import type { FormInstance, FormRules } from 'element-plus'
import type { TokenType } from '@/api/login/types'
import { nextTick, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as LoginApi from '@/api/login'
import { isEntrySso } from '@/auth/entrySession'
import { Verify } from '@/components/Verifition'
import { deleteUserCache } from '@/hooks/web/useCache'
import { useAppStore } from '@/store/modules/app'
import { useUserStore } from '@/store/modules/user'
import * as authUtil from '@/utils/auth'
import { encrypt, isSupportedAlgorithm } from '@/utils/PasswordEncryptionUtil'
import ContentEntryAuth from '@/views/agent/ContentEntryAuth.vue'
import { loginRedirectPath } from './loginRedirect'

defineProps<{ sessionTimeout?: boolean }>()
const appStore = useAppStore()
const userStore = useUserStore()
const router = useRouter()
const route = useRoute()
const tenantEnabled = import.meta.env.VITE_APP_TENANT_ENABLE === 'true'
const captchaEnabled = import.meta.env.VITE_APP_CAPTCHA_ENABLE === 'true'
const enterpriseAgentId = import.meta.env.VITE_WECHAT_WORK_AGENT_ID || import.meta.env.VITE_AGENT_ID
const enterpriseLoginEnabled = Boolean(enterpriseAgentId)
const formRef = ref<FormInstance>()
const verifyRef = ref<{ show: () => void }>()
const verifierMounted = ref(false)
const verifierVersion = ref(0)
const loading = ref(false)
const enterpriseLoading = ref(false)
const error = ref('')
const form = reactive({
  tenantName: import.meta.env.VITE_APP_DEFAULT_LOGIN_TENANT || '',
  username: '',
  password: '',
})
const rules: FormRules = {
  tenantName: [{ required: tenantEnabled, message: '请输入租户名称', trigger: 'blur' }],
  username: [{ required: true, whitespace: true, message: '请输入账号', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

function errorMessage(reason: unknown) {
  return reason instanceof Error ? reason.message : '登录未成功，请检查账号或稍后重试'
}
async function prepareTenant() {
  if (!tenantEnabled) return
  const tenantId = await LoginApi.getTenantIdByName(form.tenantName.trim())
  if (tenantId == null || tenantId === '') {
    throw new Error('未找到该租户，请检查租户名称')
  }
  authUtil.setTenantId(String(tenantId))
}
async function finishLogin(captchaVerification = '') {
  if (loading.value || enterpriseLoading.value || isEntrySso) return
  if (captchaEnabled && !captchaVerification) {
    error.value = '请先完成安全验证'
    return
  }
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (loading.value || enterpriseLoading.value) return
  loading.value = true
  error.value = ''
  try {
    await prepareTenant()
    const encryption = await LoginApi.getPublicKey()
    const algorithm =
      typeof encryption?.algorithm === 'string' ? encryption.algorithm.toUpperCase() : ''
    if (
      !isSupportedAlgorithm(algorithm) ||
      (algorithm !== 'NONE' && (typeof encryption.key !== 'string' || !encryption.key))
    ) {
      throw new Error('登录服务未返回有效的密码加密配置，请稍后重试')
    }
    const credentials = {
      username: form.username.trim(),
      password: encrypt(form.password, algorithm, encryption.key || ''),
      passwordTransmissionMode: algorithm,
      captchaVerification,
    }
    const token: TokenType = await LoginApi.login(credentials)
    if (
      typeof token?.accessToken !== 'string' ||
      !token.accessToken ||
      typeof token.refreshToken !== 'string' ||
      !token.refreshToken
    ) {
      throw new Error('登录服务未返回有效会话，请重新登录')
    }
    deleteUserCache()
    userStore.resetState()
    authUtil.setToken(token)
    form.password = ''
    await router.replace(loginRedirectPath(route.query.redirect))
  }
  catch (reason) {
    error.value = errorMessage(reason)
  }
  finally {
    loading.value = false
  }
}
async function beginLogin() {
  if (loading.value || enterpriseLoading.value || isEntrySso) return
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (loading.value || enterpriseLoading.value) return
  if (!captchaEnabled) {
    await finishLogin()
    return
  }
  loading.value = true
  error.value = ''
  try {
    await prepareTenant()
    verifierVersion.value++
    verifierMounted.value = true
    await nextTick()
    verifyRef.value?.show()
  }
  catch (reason) {
    error.value = errorMessage(reason)
  }
  finally {
    loading.value = false
  }
}
async function onVerified(result: { captchaVerification?: string }) {
  await finishLogin(result.captchaVerification || '')
}
async function enterpriseLogin() {
  if (loading.value || enterpriseLoading.value || isEntrySso) return
  if (tenantEnabled && !form.tenantName.trim()) {
    error.value = '请先输入租户名称'
    return
  }
  enterpriseLoading.value = true
  error.value = ''
  try {
    await prepareTenant()
    const callback = new URL(
      router.resolve({
        path: '/login',
        query: {
          type: '30',
          agentid: enterpriseAgentId,
          system: import.meta.env.VITE_APP_SYS_CODE || undefined,
        },
      }).href,
      window.location.origin,
    )
    const url = await LoginApi.getAuthorizeUrl(callback.href)
    if (typeof url !== 'string') throw new Error('登录服务未返回企业微信授权地址')
    const target = new URL(url)
    if (!['https:', 'http:'].includes(target.protocol) || target.username || target.password) {
      throw new Error('企业微信授权地址无效')
    }
    window.location.assign(target.href)
  }
  catch (reason) {
    error.value = errorMessage(reason)
  }
  finally {
    enterpriseLoading.value = false
  }
}
</script>

<template>
  <ContentEntryAuth v-if="isEntrySso" />
  <main v-else class="login-page">
    <section class="login-card" aria-labelledby="login-title">
      <p class="login-eyebrow">内容创作与发布工作台</p>
      <h1 id="login-title">{{ appStore.getTitle || '易发宝工作台' }}</h1>
      <p class="login-description">登录后继续使用你的工作台</p>
      <ElAlert
        v-if="sessionTimeout"
        title="登录已过期，请重新登录"
        type="info"
        :closable="false"
        show-icon
      />
      <ElAlert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <ElForm
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        :disabled="loading || enterpriseLoading"
        @submit.prevent="beginLogin"
      >
        <ElFormItem v-if="tenantEnabled" label="租户名称" prop="tenantName">
          <ElInput
            v-model="form.tenantName"
            autocomplete="organization"
            placeholder="请输入租户名称"
          />
        </ElFormItem>
        <ElFormItem label="账号" prop="username">
          <ElInput v-model="form.username" autocomplete="username" placeholder="请输入账号" />
        </ElFormItem>
        <ElFormItem label="密码" prop="password">
          <ElInput
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="请输入密码"
          />
        </ElFormItem>
        <ElButton class="login-submit" type="primary" native-type="submit" :loading="loading">
          登录
        </ElButton>
        <ElButton
          v-if="enterpriseLoginEnabled"
          class="enterprise-login"
          :loading="enterpriseLoading"
          @click="enterpriseLogin"
        >
          企业微信登录
        </ElButton>
      </ElForm>
      <p class="login-help">企业微信或 OA 单点登录可从对应工作台进入。</p>
    </section>
    <Verify
      v-if="captchaEnabled && verifierMounted"
      :key="verifierVersion"
      ref="verifyRef"
      mode="pop"
      captcha-type="blockPuzzle"
      @success="onVerified"
    />
  </main>
</template>

<style scoped>
.login-page {
  display: grid;
  min-height: 100vh;
  padding: 32px 20px;
  place-items: center;
  box-sizing: border-box;
  overflow: auto;
  background: var(--el-bg-color-page, #f6f7f9);
}
.login-card {
  width: min(100%, 420px);
  padding: 36px;
  box-sizing: border-box;
  background: var(--el-bg-color, #fff);
  border: 1px solid var(--el-border-color-light, #e5e7eb);
  border-radius: 16px;
}
.login-eyebrow {
  margin: 0 0 12px;
  color: var(--el-color-primary);
  font-size: 13px;
}
.login-card h1 {
  margin: 0;
  font-size: 25px;
  line-height: 1.4;
}
.login-description,
.login-help {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
}
.login-description {
  margin: 12px 0 24px;
}
.login-card .el-alert {
  margin-bottom: 20px;
}
.login-submit,
.enterprise-login {
  width: 100%;
}
.enterprise-login {
  margin: 12px 0 0;
}
.login-help {
  margin: 24px 0 0;
}
</style>
