<script setup lang="ts">
import type { PublishPlatform, PublishRecord, RecordStatus, Workflow } from '@/api/agent/catalog'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onActivated, onDeactivated, ref } from 'vue'
import { catalogApi, platformLabels, recordStatusLabels } from '@/api/agent/catalog'
import { platformInitial, platformName, recordStatusClass, shortTime, yuan } from './catalogPresentation'
import { friendlyContentError } from './contentWorkflow'
import WbPagination from './WbPagination.vue'

defineOptions({ name: 'CatalogRecordsView' })

const workflow = ref<Workflow | ''>('')
const status = ref<RecordStatus | 'active' | ''>('')
const platform = ref<PublishPlatform | ''>('')
const records = ref<PublishRecord[]>([])
const counts = ref<Partial<Record<RecordStatus, number>>>({})
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const resolving = ref<PublishRecord | null>(null)
const resolveUrl = ref('')
const busy = ref('')

const statusTabs: Array<{ key: RecordStatus | 'active' | '', label: string }> = [
  { key: '', label: '全部' },
  { key: 'succeeded', label: '成功' },
  { key: 'failed', label: '失败' },
  { key: 'unknown', label: '待核查' },
  { key: 'active', label: '进行中' },
]
const working = computed(() => (counts.value.queued ?? 0) + (counts.value.running ?? 0) + (counts.value.submitting ?? 0))
const tabCount = (key: string) => key === ''
  ? Object.values(counts.value).reduce((sum, value) => sum + (value ?? 0), 0)
  : key === 'active' ? working.value : counts.value[key as RecordStatus] ?? 0

async function load(quiet = false) {
  if (!quiet) {
    loading.value = true
  }
  try {
    const result = await catalogApi.records({
      workflow: workflow.value || undefined,
      status: status.value === 'active' ? 'queued,running,submitting' : status.value || undefined,
      platform: platform.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    records.value = result.items
    counts.value = result.counts
    total.value = result.total
  }
  catch (error) {
    if (!quiet) {
      ElMessage.error(friendlyContentError(error, '读取失败'))
    }
  }
  finally {
    loading.value = false
  }
}

function reload() {
  page.value = 1
  void load()
}

async function retry(record: PublishRecord) {
  busy.value = record.id
  try {
    await catalogApi.retryRecord(record.id)
    await load(true)
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '重试失败'))
  }
  finally {
    busy.value = ''
  }
}

function openResolve(record: PublishRecord) {
  resolving.value = record
  resolveUrl.value = ''
}

async function resolve(result: 'succeeded' | 'failed') {
  if (!resolving.value) {
    return
  }
  busy.value = resolving.value.id
  try {
    await catalogApi.resolveRecord(resolving.value.id, result, resolveUrl.value.trim())
    resolving.value = null
    await load(true)
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '保存失败'))
  }
  finally {
    busy.value = ''
  }
}

let timer: ReturnType<typeof setInterval> | undefined
onActivated(() => {
  void load()
  clearInterval(timer)
  timer = setInterval(() => working.value && void load(true), 5000)
})
onDeactivated(() => clearInterval(timer))
</script>

<template>
  <div class="wb-page">
    <header class="wb-page-head">
      <div>
        <h1>发布记录</h1>
        <p>每次上架或宣传的执行结果。「待核查」表示已点击发布但没拿到平台回执，请到平台确认后标记。</p>
      </div>
      <div class="wb-page-actions">
        <ElSelect
          v-model="workflow"
          placeholder="全部流程"
          clearable
          class="filter"
          @change="reload"
        >
          <ElOption value="listing" label="商品上架" />
          <ElOption value="promotion" label="内容宣传" />
        </ElSelect>
        <ElSelect
          v-model="platform"
          placeholder="全部平台"
          clearable
          class="filter"
          @change="reload"
        >
          <ElOption
            v-for="(label, key) in platformLabels"
            :key="key"
            :value="key"
            :label="label"
          />
        </ElSelect>
        <ElButton :icon="Refresh" @click="load()">刷新</ElButton>
      </div>
    </header>

    <section class="wb-panel">
      <div class="wb-toolbar">
        <div class="wb-tabs">
          <button
            v-for="tab in statusTabs"
            :key="tab.key"
            type="button"
            :class="{ active: status === tab.key }"
            @click="status = tab.key; reload()"
          >
            {{ tab.label }}<small>{{ tabCount(tab.key) }}</small>
          </button>
        </div>
      </div>
      <ElTable
        v-loading="loading"
        :data="records"
        row-key="id"
        empty-text="暂无记录"
        class="record-table"
      >
        <ElTableColumn label="平台" width="100">
          <template #default="{ row }">
            <span class="wb-platform" :class="`p-${row.platform}`"><i>{{ platformInitial[row.platform as PublishPlatform] }}</i>{{ platformName(row.platform) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="商品 / 标题" min-width="240">
          <template #default="{ row }">
            <div class="title-cell">
              <strong class="wb-ellipsis">{{ row.product_name }}</strong>
              <small class="wb-muted wb-ellipsis">{{ row.title }}</small>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="96">
          <template #default="{ row }">
            <span class="wb-pill" :class="recordStatusClass(row.status)">{{ recordStatusLabels[row.status as RecordStatus] }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="售价" width="96" align="right">
          <template #default="{ row }">
            <span class="wb-num" :class="{ 'wb-muted': row.workflow !== 'listing' }">{{ row.workflow === 'listing' ? yuan(row.price) : '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="结果" min-width="220">
          <template #default="{ row }">
            <a
              v-if="row.external_url"
              :href="row.external_url"
              target="_blank"
              rel="noopener"
              class="result-link wb-ellipsis"
            >{{ row.external_url }}</a>
            <span v-else-if="row.error" class="error-text" :title="row.error">{{ row.error }}</span>
            <span v-else class="wb-muted">—</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="账号" width="120">
          <template #default="{ row }">
            <span class="wb-muted wb-ellipsis">{{ row.account_label || '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="时间" width="140">
          <template #default="{ row }">
            <span class="wb-muted wb-num" :title="row.attempt > 1 ? `第 ${row.attempt} 次执行` : ''">{{ shortTime(row.finished_at || row.created_at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn width="90" align="right">
          <template #default="{ row }">
            <ElButton
              v-if="row.status === 'failed'"
              link
              type="primary"
              :loading="busy === row.id"
              @click="retry(row)"
            >
              重试
            </ElButton>
            <ElButton
              v-else-if="row.status === 'unknown'"
              link
              type="warning"
              @click="openResolve(row)"
            >
              核查
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
      <WbPagination
        v-model:page="page"
        v-model:page-size="pageSize"
        :total="total"
        @change="load()"
      />
    </section>

    <ElDialog
      :model-value="!!resolving"
      title="核查发布结果"
      width="460px"
      append-to-body
      @close="resolving = null"
    >
      <p class="resolve-text">
        请到{{ resolving ? platformName(resolving.platform) : '' }}确认「{{ resolving?.title }}」是否已发布。系统不会自动重试，避免重复发布。
      </p>
      <ElInput v-model="resolveUrl" placeholder="已发布的作品或商品链接（选填，https://）" />
      <template #footer>
        <ElButton :loading="!!busy" @click="resolve('failed')">未发布</ElButton>
        <ElButton type="primary" :loading="!!busy" @click="resolve('succeeded')">已发布</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.filter {
  width: 130px;
}
.title-cell {
  display: grid;
  min-width: 0;
  gap: 2px;
}
.title-cell strong {
  font-weight: 500;
}
.title-cell small {
  font-size: 12px;
}
.result-link {
  display: block;
  color: var(--xi-semantic-color-link-default);
  font-size: 13px;
}
.error-text {
  display: -webkit-box;
  overflow: hidden;
  color: var(--xi-semantic-color-feedback-danger-text);
  font-size: 13px;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.record-table :deep(th.el-table__cell) {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  font-weight: 500;
  background: #fafbfc;
}
.resolve-text {
  margin: 0 0 12px;
  color: var(--xi-semantic-color-text-secondary);
  font-size: 13px;
  line-height: 1.6;
}
</style>
