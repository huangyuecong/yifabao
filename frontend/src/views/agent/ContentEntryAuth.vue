<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { ensureEntrySession, entryAuthError, entryReturnTo } from '@/auth/entrySession'

const route = useRoute()
const loading = ref(false)
async function retry() {
  loading.value = true
  const target = entryReturnTo(typeof route.query.return_to === 'string' ? route.query.return_to : undefined)
  try {
    if (await ensureEntrySession(target)) {
      window.location.replace(target)
    }
  }
  finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="entry-auth-page">
    <ElResult icon="info" title="统一登录" :sub-title="entryAuthError || '请先完成统一身份验证后使用内容智能体'">
      <template #extra>
        <ElButton type="primary" :loading="loading" @click="retry">重新验证登录</ElButton>
      </template>
    </ElResult>
  </main>
</template>

<style scoped>
.entry-auth-page {
  display: grid;
  min-height: 100vh;
  padding: var(--xi-component-content-gap-lg);
  place-items: center;
  background: var(--xi-semantic-color-background-container);
}
</style>
