<script setup lang="ts">
import type { KnowledgeSyncStatus } from '@/api/agent/content'
import { computed, onUnmounted, ref, watch } from 'vue'
import { contentApi } from '@/api/agent/content'
import { friendlyContentError } from './contentWorkflow'

const props = defineProps<{
  itemId: string
  initial?: KnowledgeSyncStatus | null
  canEdit?: boolean
}>()
const state = ref<KnowledgeSyncStatus | null>(props.initial ?? null)
const busy = ref(false)
const error = ref('')
let timer: ReturnType<typeof setTimeout> | undefined
let revision = 0
const labels: Record<string, string> = {
  not_synced: '等待同步',
  queued: '等待同步',
  uploading: '正在同步',
  processing: '正在建立知识索引',
  ready: '知识索引可用',
  failed: '知识索引失败',
  ambiguous: '同步结果待核实',
  deleting: '正在移除旧索引',
  deleted: '知识索引已移除',
  excluded: '不参与知识同步',
  local: '使用本地资料',
}
const label = computed(() => labels[state.value?.status ?? ''] ?? '同步状态待确认')
const tone = computed(() => state.value?.status === 'ready' ? 'success' : ['failed', 'ambiguous'].includes(state.value?.status ?? '') ? 'warning' : 'info')
const originalLabels: Record<string, string> = {
  no_file: '纯文本资料，无附件',
  disabled: '原件托管未启用',
  not_archived: '原件等待托管',
  queued: '原件正在托管',
  processing: '原件正在校验',
  ready: '原件已托管',
  failed: '原件托管失败，本地文件保留',
  ambiguous: '原件托管结果待核实',
  unsupported: '暂不支持托管，原件保留本机',
  excluded: '原件不在托管范围',
}
const originalLabel = computed(() => originalLabels[state.value?.original?.status ?? ''] ?? '原件状态待确认')

function schedule() {
  clearTimeout(timer)
  const indexPending = state.value?.sync_enabled !== false && ['not_synced', 'queued', 'uploading', 'processing', 'deleting'].includes(state.value?.status ?? '')
  const originalPending = state.value?.original?.sync_enabled && ['not_archived', 'queued', 'processing'].includes(state.value.original.status)
  if (indexPending || originalPending) {
    timer = setTimeout(refresh, 5000)
  }
}

async function refresh(retry: boolean | 'original' = false) {
  const current = ++revision
  busy.value = true
  error.value = ''
  try {
    const value = await (retry === 'original'
      ? contentApi.retryLibraryOriginalArchive(props.itemId)
      : retry ? contentApi.retryLibraryKnowledgeSync(props.itemId) : contentApi.getLibraryKnowledgeSync(props.itemId))
    if (current === revision) {
      state.value = value
      schedule()
    }
  }
  catch (cause) {
    if (current === revision) {
      error.value = friendlyContentError(cause)
    }
  }
  finally {
    if (current === revision) {
      busy.value = false
    }
  }
}

watch(() => [props.itemId, props.initial] as const, () => {
  revision++
  clearTimeout(timer)
  state.value = props.initial ?? null
  if (state.value) {
    refresh()
  }
}, { immediate: true })
onUnmounted(() => {
  revision++
  clearTimeout(timer)
})
</script>

<template>
  <div v-if="state && (state.status !== 'local' || state.original)" class="knowledge-sync">
    <div class="knowledge-sync-actions">
      <ElTag :type="tone" effect="plain">{{ label }}</ElTag>
      <ElButton link :loading="busy" @click="refresh()">刷新状态</ElButton>
      <ElButton
        v-if="canEdit && state.can_retry"
        link
        type="primary"
        :disabled="busy"
        @click="refresh(true)"
      >
        重试同步
      </ElButton>
    </div>
    <p v-if="state.status !== 'local' && state.sync_enabled === false">知识索引自动同步尚未启用，请联系项目管理员。</p>
    <p v-if="state.error">{{ state.error }}</p>
    <div v-if="state.original" class="knowledge-sync-actions knowledge-original">
      <ElTag :type="state.original.status === 'ready' ? 'success' : 'info'" effect="plain">{{ originalLabel }}</ElTag>
      <ElButton
        v-if="canEdit && state.original.can_retry"
        link
        type="primary"
        :disabled="busy"
        @click="refresh('original')"
      >
        重试原件托管
      </ElButton>
    </div>
    <p v-if="state.original?.error">{{ state.original.error }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.knowledge-sync {
  padding: 12px 0;
}
.knowledge-sync-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
.knowledge-sync p {
  margin: 8px 0 0;
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}
.knowledge-original {
  margin-top: 8px;
}
</style>
