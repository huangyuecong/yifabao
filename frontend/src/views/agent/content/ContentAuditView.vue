<script setup lang="ts">
import type { ContentAuditLog } from '@/api/agent/content'
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import { contentApi } from '@/api/agent/content'
import { accessErrorStatus, useLocalAccess } from './localAccess'

defineOptions({ name: 'ContentAuditView' })
const { access, hasPermission, message, refresh } = useLocalAccess()
const canRead = computed(() => hasPermission('audit.read'))
const records = ref<ContentAuditLog[]>([])
const loading = ref(false)
const error = ref('')
const action = ref('')
const resourceType = ref('')
const appliedFilters = ref<{ action?: string, resource_type?: string }>({})
const page = ref(1)
const pageSize = 20
watch(() => [access.value?.subject, canRead.value], ([subject, allowed], previous) => {
  if (!allowed || (previous?.[0] && subject !== previous[0])) {
    records.value = []
    page.value = 1
  }
})

async function load() {
  if (loading.value) {
    return
  }
  loading.value = true
  error.value = ''
  try {
    await refresh()
    if (!canRead.value) {
      records.value = []
      return
    }
    const subject = access.value?.subject
    const result = await contentApi.auditLogs({ ...appliedFilters.value, page: page.value, page_size: pageSize })
    if (canRead.value && subject === access.value?.subject) {
      records.value = result
    }
  }
  catch (reason) {
    records.value = []
    error.value = reason instanceof Error ? reason.message : '暂时无法读取操作记录'
    if ([401, 403].includes(accessErrorStatus(reason) || 0)) {
      await refresh()
    }
  }
  finally {
    loading.value = false
  }
}
function search() {
  appliedFilters.value = { action: action.value.trim() || undefined, resource_type: resourceType.value.trim() || undefined }
  page.value = 1
  void load()
}
function changePage(value: number) {
  page.value = value
  void load()
}
function detailsText(details: Record<string, unknown>) {
  return JSON.stringify(details, null, 2)
}
onMounted(load)
onActivated(load)
</script>

<template>
  <div class="audit-view" :aria-busy="loading">
    <section class="audit-heading"><div><h2>操作记录</h2><p>查看本项目最近的业务操作，人员授权变更也会留痕。</p></div><ElButton :loading="loading" @click="load">刷新记录</ElButton></section>
    <ElAlert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
    />
    <ElAlert
      v-if="!canRead"
      :title="message || '当前账号没有查看操作记录的权限'"
      type="info"
      :closable="false"
      show-icon
    />
    <template v-else>
      <form class="audit-search" @submit.prevent="search">
        <ElInput
          v-model="action"
          clearable
          placeholder="操作标识（精确匹配，可留空）"
          aria-label="操作标识"
          maxlength="100"
        />
        <ElInput
          v-model="resourceType"
          clearable
          placeholder="对象类型（精确匹配，可留空）"
          aria-label="对象类型"
          maxlength="64"
        />
        <ElButton native-type="submit" type="primary" :loading="loading">筛选</ElButton>
      </form>
      <div class="audit-table">
        <ElTable :data="records" row-key="id" empty-text="本页暂无匹配记录">
          <ElTableColumn type="expand"><template #default="{ row }"><pre class="audit-details">{{ detailsText(row.details) }}</pre></template></ElTableColumn>
          <ElTableColumn label="时间" min-width="170"><template #default="{ row }">{{ new Date(row.created_at).toLocaleString('zh-CN') }}</template></ElTableColumn>
          <ElTableColumn
            prop="owner_id"
            label="操作人账号"
            min-width="200"
            show-overflow-tooltip
          />
          <ElTableColumn prop="action" label="操作" min-width="190" />
          <ElTableColumn prop="resource_type" label="对象类型" min-width="140" />
          <ElTableColumn
            prop="resource_id"
            label="对象标识"
            min-width="220"
            show-overflow-tooltip
          />
        </ElTable>
      </div>
      <div class="audit-pagination" aria-label="操作记录分页">
        <ElButton :disabled="loading || page <= 1" @click="changePage(page - 1)">上一页</ElButton>
        <span>第 {{ page }} 页</span>
        <ElButton :disabled="loading || records.length < pageSize" @click="changePage(page + 1)">下一页</ElButton>
      </div>
    </template>
  </div>
</template>

<style scoped>
.audit-view {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-md);
}
.audit-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-sm);
}
h2,
p {
  margin: 0;
}
h2 {
  font-size: var(--xi-semantic-typography-heading1-font-size);
  font-weight: 600;
  line-height: 28px;
}
.audit-heading p {
  margin-top: 2px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 13px;
}
.audit-search {
  display: flex;
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-sm);
}
.audit-search :deep(.el-input) {
  width: min(280px, 100%);
}
.audit-table {
  min-width: 0;
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}
.audit-pagination {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-sm);
}
.audit-details {
  padding: var(--xi-component-content-gap-md);
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--xi-semantic-color-text-secondary);
}
</style>
