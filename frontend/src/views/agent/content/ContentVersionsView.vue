<script setup lang="ts">
import type { Artifact, ContentTask } from '@/api/agent/content'
import { ArrowRight, Document, MagicStick, Picture, Search, VideoCamera } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { contentApi } from '@/api/agent/content'
import { saveBlob } from '@/api/agent/http'
import {
  historyArtifactKindLabel,
  historyArtifactMediaKind,
  historyArtifactSections,
  historyArtifactSummary,
} from './contentHistoryPresentation'
import {
  artifactStatusLabels,
  compareContentDateTimeDescending,
  contentFixedPopperOptions,
  formatDate,
  friendlyContentError,
  modeLabels,
  normalizeStageKey,
  parseContentDateTime,
  stageDefinitions,
  startOfBeijingCalendarDay,
  taskStatusLabels,
} from './contentWorkflow'

defineOptions({ name: 'ContentVersionsView' })

const tasks = ref<ContentTask[]>([])
const artifactsByTask = reactive<Record<string, Artifact[]>>({})
const loading = ref(false)
const loadingTaskId = ref('')
const query = ref('')
const statusFilter = ref('')
const ownerFilter = ref('')
const dateRange = ref<[Date, Date] | []>([])
const currentPage = ref(1)
const pageSize = ref(10)
const activeTaskId = ref('')
const historyOpen = ref(false)
const detailArtifact = ref<Artifact>()
const detailOpen = ref(false)
const detailMediaUrl = ref('')
const detailMediaLoading = ref(false)
const detailMediaError = ref('')
let detailMediaRequest = 0
const router = useRouter()
const route = useRoute()

function taskIcon(mode: string) {
  if (mode === 'image') {
    return Picture
  }
  if (mode === 'video') {
    return VideoCamera
  }
  if (mode === 'copy') {
    return Document
  }
  return MagicStick
}

function resetFilters() {
  query.value = ''
  statusFilter.value = ''
  ownerFilter.value = ''
  dateRange.value = []
}

const hasFilters = computed(() => Boolean(query.value || statusFilter.value || ownerFilter.value || dateRange.value.length))
const activeTask = computed(() => tasks.value.find(item => item.id === activeTaskId.value))
const activeTaskReadOnly = computed(() => activeTask.value?.can_edit === false)
const ownerOptions = computed(() => {
  const owners = new Map<string, string>()
  for (const task of tasks.value) {
    owners.set(task.owner_id, taskOwnerLabel(task))
  }
  return [...owners].map(([value, label]) => ({ value, label }))
})
const activeArtifacts = computed(() => artifactsByTask[activeTaskId.value] ?? [])
const detailMediaKind = computed(() =>
  detailArtifact.value ? historyArtifactMediaKind(detailArtifact.value) : null,
)
const detailSections = computed(() =>
  detailArtifact.value ? historyArtifactSections(detailArtifact.value) : [],
)
function taskUpdatedAt(task: ContentTask): Date | null {
  return parseContentDateTime(task.updated_at || task.created_at)
}

function taskOwnerLabel(task: ContentTask): string {
  return task.owner_id === 'dev-user' ? '历史开发账号（dev-user）' : task.owner_id
}

const filteredTasks = computed(() => {
  const normalizedQuery = query.value.trim().toLocaleLowerCase('zh-CN')
  const [start, end] = dateRange.value
  const startOfDay = start ? startOfBeijingCalendarDay(start) : null
  const endOfDay = end ? startOfBeijingCalendarDay(end, 1) : null
  return [...tasks.value]
    .filter((task) => {
      const matchesQuery = !normalizedQuery || [
        task.name,
        task.topic,
        task.product,
        task.content_type,
      ].some(value => value.toLocaleLowerCase('zh-CN').includes(normalizedQuery))
      const archived = Boolean(task.archived || task.status === 'archived')
      const matchesStatus = !statusFilter.value
        || (statusFilter.value === 'archived' ? archived : statusFilter.value === 'draft' && !archived)
      const updatedAt = taskUpdatedAt(task)
      const matchesDate = (!startOfDay && !endOfDay)
        || Boolean(
          updatedAt
          && (!startOfDay || updatedAt >= startOfDay)
          && (!endOfDay || updatedAt < endOfDay),
        )
      const matchesOwner = !ownerFilter.value || task.owner_id === ownerFilter.value
      return matchesQuery && matchesStatus && matchesDate && matchesOwner
    })
    .sort((left, right) =>
      compareContentDateTimeDescending(
        left.updated_at || left.created_at,
        right.updated_at || right.created_at,
      ),
    )
})
const paginatedTasks = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredTasks.value.slice(start, start + pageSize.value)
})
const statusOptions = computed(() => {
  const values = new Set<string>()
  if (tasks.value.some(item => !item.archived && item.status !== 'archived')) {
    values.add('draft')
  }
  if (tasks.value.some(item => item.archived || item.status === 'archived')) {
    values.add('archived')
  }
  return [...values]
})
const currentArtifacts = computed(() => {
  const currentStatuses = new Set(['selected', 'confirmed'])
  const byKind = new Map<string, Artifact>()
  for (const artifact of activeArtifacts.value) {
    if (!currentStatuses.has(artifact.status)) {
      continue
    }
    const current = byKind.get(artifact.kind)
    if (!current || artifact.version > current.version) {
      byKind.set(artifact.kind, artifact)
    }
  }
  return [...byKind.values()].sort((left, right) =>
    (left.stage_key || '').localeCompare(right.stage_key || ''),
  )
})
const versionGroups = computed(() => {
  const groups = new Map<string, Artifact[]>()
  for (const artifact of activeArtifacts.value) {
    const label = historyArtifactKindLabel(artifact)
    groups.set(label, [...(groups.get(label) ?? []), artifact])
  }
  return [...groups.entries()].map(([label, artifacts]) => ({
    label,
    artifacts: artifacts.sort((left, right) => right.version - left.version),
  }))
})

function artifactStatusLabel(status?: string | null) {
  return status ? artifactStatusLabels[status] ?? '状态待确认' : '未记录状态'
}

function stageLabel(stage?: string | null) {
  return stageDefinitions.find(item => item.key === stage)?.label ?? '未关联阶段'
}

function workflowStatus(task: ContentTask): string {
  return task.workflow_status || (task.status === 'archived' ? 'draft' : task.status)
}

function artifactStatusType(status?: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'confirmed') {
    return 'success'
  }
  if (['selected', 'stale'].includes(status ?? '')) {
    return 'warning'
  }
  if (['discarded', 'failed', 'legacy_mock'].includes(status ?? '')) {
    return 'danger'
  }
  return 'info'
}

function artifactSource(artifact: Artifact) {
  if (artifact.linked || artifact.source_task_id) {
    return '其他任务引用'
  }
  if (artifact.parent_id) {
    return '人工或 AI 修改'
  }
  if (artifact.generation_run_id || artifact.content.generation) {
    return 'AI 生成'
  }
  return '人工导入或系统生成'
}

let skipNextActivate = false

async function loadTasks(options: { resetPage?: boolean, silent?: boolean } = {}) {
  if (!options.silent) {
    loading.value = true
  }
  try {
    tasks.value = await contentApi.listTasks({ includeArchived: true })
    if (options.resetPage !== false) {
      currentPage.value = 1
    }
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '历史内容暂时不可用'))
  }
  finally {
    loading.value = false
  }
}

async function openTaskHistory(task: ContentTask) {
  activeTaskId.value = task.id
  historyOpen.value = true
  if (artifactsByTask[task.id]) {
    return
  }
  loadingTaskId.value = task.id
  try {
    artifactsByTask[task.id] = await contentApi.listArtifacts(task.id)
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '无法读取该任务的历史版本'))
  }
  finally {
    loadingTaskId.value = ''
  }
}

async function continueTask() {
  if (!activeTask.value || activeTaskReadOnly.value) {
    return
  }
  const task = activeTask.value
  if (task.archived || task.status === 'archived') {
    try {
      await ElMessageBox.confirm('原归档内容保持不变。修改内容只有在最终确认后才另存为新任务；未确认就退出，不会增加任务。是否开始修改？', '修改归档内容', {
        type: 'info',
        confirmButtonText: '开始修改',
        cancelButtonText: '取消',
      })
      const restored = await contentApi.restoreTask(task.id)
      // Keep the archived task in history. The returned draft is intentionally
      // absent from task lists until its final confirmation.
      historyOpen.value = false
      const editStage = restored.mode === 'video' ? 'video_preview' : 'configuration'
      await router.push(`/agent/content/tasks/${restored.id}/stages/${editStage}`)
    }
    catch (error) {
      if (error === 'cancel' || error === 'close') {
        return
      }
      ElMessage.error(friendlyContentError(error, '暂时无法开始修改，原归档内容未改变，请重试'))
    }
    return
  }
  historyOpen.value = false
  await router.push(
    `/agent/content/tasks/${task.id}/stages/${normalizeStageKey(task.current_stage)}`,
  )
}

async function deleteActiveTask() {
  const task = activeTask.value
  if (!task || activeTaskReadOnly.value) {
    return
  }
  try {
    await ElMessageBox.confirm(
      `删除后“${task.name}”及其所有版本、生成记录和视频工程数据都会从数据库永久移除，无法恢复。确定删除吗？`,
      '删除内容任务',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    await contentApi.deleteTask(task.id)
    ElMessage.success('任务已删除')
    historyOpen.value = false
    detailOpen.value = false
    clearDetailMedia()
    detailArtifact.value = undefined
    delete artifactsByTask[task.id]
    activeTaskId.value = ''
    await loadTasks()
  }
  catch (error) {
    if (error === 'cancel' || error === 'close') {
      return
    }
    ElMessage.error(friendlyContentError(error, '任务删除失败，原任务未改变，请重试'))
  }
}

function viewArtifact(artifact: Artifact) {
  clearDetailMedia()
  detailArtifact.value = artifact
  detailOpen.value = true
  if (historyArtifactMediaKind(artifact)) {
    void loadDetailMedia(artifact)
  }
}

function clearDetailMedia() {
  detailMediaRequest += 1
  if (detailMediaUrl.value) {
    URL.revokeObjectURL(detailMediaUrl.value)
  }
  detailMediaUrl.value = ''
  detailMediaLoading.value = false
  detailMediaError.value = ''
}

async function loadDetailMedia(artifact: Artifact) {
  const mediaKind = historyArtifactMediaKind(artifact)
  if (!mediaKind) {
    return
  }
  const requestId = ++detailMediaRequest
  detailMediaLoading.value = true
  try {
    const { blob } = await contentApi.downloadArtifact(artifact.id)
    if (requestId !== detailMediaRequest || detailArtifact.value?.id !== artifact.id) {
      return
    }
    detailMediaUrl.value = URL.createObjectURL(blob)
  }
  catch (error) {
    if (requestId === detailMediaRequest && detailArtifact.value?.id === artifact.id) {
      detailMediaError.value = friendlyContentError(error, '该版本暂时无法预览，请点击导出下载')
    }
  }
  finally {
    if (requestId === detailMediaRequest) {
      detailMediaLoading.value = false
    }
  }
}

function handleDetailClosed() {
  clearDetailMedia()
  detailArtifact.value = undefined
}

async function exportArtifact(artifact: Artifact) {
  try {
    const { blob, fileName } = await contentApi.downloadArtifact(artifact.id)
    saveBlob(blob, fileName || `${historyArtifactKindLabel(artifact)}-v${artifact.version}`)
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '该版本暂时没有可下载文件'))
  }
}

async function exportTask() {
  if (!activeTask.value) {
    return
  }
  try {
    const { blob, fileName } = await contentApi.exportTask(activeTask.value.id)
    saveBlob(blob, fileName || `${activeTask.value.name}-素材包.zip`)
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error))
  }
}

watch([query, statusFilter, ownerFilter, dateRange, pageSize], () => {
  currentPage.value = 1
}, { deep: true })

async function openRequestedTaskHistory() {
  const taskId = typeof route.query.taskId === 'string' ? route.query.taskId : ''
  const task = taskId ? tasks.value.find(item => item.id === taskId) : undefined
  if (task) {
    await openTaskHistory(task)
  }
}

watch(() => route.query.taskId, () => {
  void openRequestedTaskHistory()
})

onMounted(async () => {
  skipNextActivate = true
  await loadTasks()
  await openRequestedTaskHistory()
})

onActivated(async () => {
  if (skipNextActivate) {
    skipNextActivate = false
    return
  }
  await loadTasks({ resetPage: false, silent: true })
  await openRequestedTaskHistory()
})

onBeforeUnmount(handleDetailClosed)
</script>

<template>
  <div class="history-view" :aria-busy="loading">
    <section class="view-heading">
      <div>
        <h2>历史任务</h2>
        <p>按时间、关键词和状态查找内容任务，进入任务后查看当前内容及全部历史版本。</p>
      </div>
      <ElButton :loading="loading" @click="loadTasks()">刷新</ElButton>
    </section>

    <section class="history-panel">
      <div class="history-toolbar">
        <ElInput
          v-model="query"
          clearable
          :prefix-icon="Search"
          placeholder="搜索任务、主题、产品"
          aria-label="搜索历史内容"
        />
        <ElDatePicker
          v-model="dateRange"
          type="daterange"
          unlink-panels
          teleported
          popper-class="content-agent-picker"
          :popper-options="contentFixedPopperOptions"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          range-separator="至"
        />
        <ElSelect
          v-model="statusFilter"
          clearable
          placeholder="全部状态"
          aria-label="按状态筛选"
        >
          <ElOption
            v-for="status in statusOptions"
            :key="status"
            :label="taskStatusLabels[status] ?? status"
            :value="status"
          />
        </ElSelect>
        <ElSelect
          v-model="ownerFilter"
          clearable
          filterable
          placeholder="全部创作者"
          aria-label="按创作者筛选"
        >
          <ElOption
            v-for="owner in ownerOptions"
            :key="owner.value"
            :label="owner.label"
            :value="owner.value"
          />
        </ElSelect>
        <ElButton :disabled="!hasFilters" @click="resetFilters">重置</ElButton>
      </div>
      <div class="history-result-count"><span>全部记录 <strong>{{ filteredTasks.length }}</strong></span><span>按最近更新排序</span></div>

      <ElEmpty v-if="!loading && !filteredTasks.length" description="没有符合条件的历史内容" />
      <div v-else class="history-list" aria-live="polite">
        <button
          v-for="task in paginatedTasks"
          :key="task.id"
          type="button"
          class="history-item"
          @click="openTaskHistory(task)"
        >
          <span class="history-type-icon" :class="`history-type-${task.mode}`" aria-hidden="true"><component :is="taskIcon(task.mode)" /></span>
          <div class="history-main">
            <div class="history-title-row">
              <strong>{{ task.name }}</strong>
              <ElTag v-if="task.archived || task.status === 'archived'" type="info" effect="light">已归档</ElTag>
              <ElTag v-else type="warning" effect="light">
                配置中
              </ElTag>
            </div>
            <p>{{ task.topic || task.requirements || '未填写主题或要求' }}</p>
            <div class="history-meta">
              <span>{{ modeLabels[task.mode] }}</span>
              <span>创作者：{{ taskOwnerLabel(task) }}</span>
              <span v-if="task.can_edit === false">只读</span>
              <ElTag
                v-if="task.product"
                class="product-tag"
                size="small"
                type="info"
              >
                {{ task.product }}
              </ElTag>
              <ElTag size="small" type="info" effect="light">{{ stageLabel(task.current_stage) }}</ElTag>
            </div>
          </div>
          <div class="history-time">
            <strong>{{ formatDate(task.updated_at) }}</strong>
            <small>查看内容与版本 <ArrowRight aria-hidden="true" /></small>
          </div>
        </button>
      </div>
      <div v-if="filteredTasks.length" class="history-pagination" aria-label="历史内容分页">
        <ElPagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          background
          :page-sizes="[10, 20, 50]"
          :pager-count="7"
          layout="total, sizes, prev, pager, next, jumper"
          :total="filteredTasks.length"
        />
      </div>
    </section>

    <ElDrawer
      v-model="historyOpen"
      size="min(960px, 94vw)"
      destroy-on-close
      :show-close="true"
    >
      <template #header>
        <div v-if="activeTask" class="drawer-heading">
          <div><span>历史内容</span><h3>{{ activeTask.name }}</h3></div>
          <ElTag v-if="activeTask.archived || activeTask.status === 'archived'" type="info">已归档</ElTag>
          <ElTag v-else type="warning">
            配置中
          </ElTag>
        </div>
      </template>

      <div v-if="activeTask" v-loading="loadingTaskId === activeTask.id" class="history-detail">
        <ElAlert
          v-if="activeTaskReadOnly"
          title="当前任务仅可查看"
          description="可查看内容、历史版本并下载文件；修改和删除仍需原有任务编辑权限。"
          type="info"
          :closable="false"
          show-icon
        />
        <dl class="task-overview">
          <div><dt>内容模式</dt><dd>{{ modeLabels[activeTask.mode] }}</dd></div>
          <div><dt>当前阶段</dt><dd>{{ stageLabel(activeTask.current_stage) }}</dd></div>
          <div><dt>最近更新</dt><dd>{{ formatDate(activeTask.updated_at) }}</dd></div>
          <div><dt>创作者</dt><dd>{{ taskOwnerLabel(activeTask) }}</dd></div>
          <div><dt>产品</dt><dd>{{ activeTask.product || '—' }}</dd></div>
          <div><dt>主题</dt><dd>{{ activeTask.topic || '—' }}</dd></div>
        </dl>

        <section class="current-section">
          <div class="section-heading">
            <div><h4>当前内容</h4><p>各内容类型当前正在使用或已经确认的版本。</p></div>
          </div>
          <ElEmpty v-if="!currentArtifacts.length" description="当前还没有已选择或已确认的内容" />
          <div v-else class="current-grid">
            <button
              v-for="artifact in currentArtifacts"
              :key="artifact.id"
              type="button"
              class="current-card"
              @click="viewArtifact(artifact)"
            >
              <span>{{ historyArtifactKindLabel(artifact) }}</span>
              <strong>版本 {{ artifact.version }}</strong>
              <ElTag :type="artifactStatusType(artifact.status)" size="small">
                {{ artifactStatusLabel(artifact.status) }}
              </ElTag>
            </button>
          </div>
        </section>

        <section class="versions-section">
          <div class="section-heading"><div><h4>全部版本记录</h4><p>点击任一记录查看具体内容和版本关系。</p></div></div>
          <ElCollapse accordion>
            <ElCollapseItem
              v-for="group in versionGroups"
              :key="group.label"
              :name="group.label"
              :title="`${group.label}（${group.artifacts.length} 个版本）`"
            >
              <ElTable :data="group.artifacts" @row-click="viewArtifact">
                <ElTableColumn prop="version" label="版本" width="90" />
                <ElTableColumn label="状态" width="110">
                  <template #default="scope">
                    <ElTag :type="artifactStatusType(scope.row.status)" size="small">
                      {{ artifactStatusLabel(scope.row.status) }}
                    </ElTag>
                  </template>
                </ElTableColumn>
                <ElTableColumn label="所属阶段" min-width="130">
                  <template #default="scope">{{ stageLabel(scope.row.stage_key) }}</template>
                </ElTableColumn>
                <ElTableColumn label="来源" min-width="150">
                  <template #default="scope">{{ artifactSource(scope.row) }}</template>
                </ElTableColumn>
                <ElTableColumn label="内容说明" min-width="280">
                  <template #default="scope">
                    <span class="version-summary">{{ historyArtifactSummary(scope.row) }}</span>
                  </template>
                </ElTableColumn>
                <ElTableColumn label="创建时间" min-width="180">
                  <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
                </ElTableColumn>
                <ElTableColumn label="操作" width="180" fixed="right">
                  <template #default="scope">
                    <ElButton link type="primary" @click.stop="viewArtifact(scope.row)">查看记录</ElButton>
                    <ElButton link @click.stop="exportArtifact(scope.row)">导出</ElButton>
                  </template>
                </ElTableColumn>
              </ElTable>
            </ElCollapseItem>
          </ElCollapse>
        </section>
      </div>
      <template #footer>
        <div v-if="activeTask" class="history-actions">
          <ElButton
            v-if="!activeTaskReadOnly"
            class="delete-task-button"
            type="danger"
            text
            @click="deleteActiveTask"
          >
            删除任务
          </ElButton>
          <ElButton :disabled="workflowStatus(activeTask) !== 'confirmed' || !currentArtifacts.length" @click="exportTask">导出已确认素材包</ElButton>
          <ElButton v-if="!activeTaskReadOnly" type="primary" @click="continueTask">{{ activeTask.archived || activeTask.status === 'archived' ? '修改归档内容' : '继续配置' }}</ElButton>
        </div>
      </template>
    </ElDrawer>

    <ElDialog
      v-model="detailOpen"
      title="版本记录详情"
      width="min(920px, 92vw)"
      @closed="handleDetailClosed"
    >
      <div v-if="detailArtifact" class="record-detail">
        <dl class="task-overview record-overview">
          <div><dt>内容类型</dt><dd>{{ historyArtifactKindLabel(detailArtifact) }}</dd></div>
          <div><dt>版本</dt><dd>v{{ detailArtifact.version }}</dd></div>
          <div><dt>状态</dt><dd>{{ artifactStatusLabel(detailArtifact.status) }}</dd></div>
          <div><dt>来源</dt><dd>{{ artifactSource(detailArtifact) }}</dd></div>
          <div><dt>创建时间</dt><dd>{{ formatDate(detailArtifact.created_at) }}</dd></div>
        </dl>
        <div
          v-if="detailMediaKind"
          class="record-media"
          :aria-busy="detailMediaLoading"
        >
          <ElImage
            v-if="detailMediaKind === 'image' && detailMediaUrl"
            :src="detailMediaUrl"
            :preview-src-list="[detailMediaUrl]"
            alt="版本图片预览"
            fit="contain"
            preview-teleported
          />
          <audio
            v-else-if="detailMediaKind === 'audio' && detailMediaUrl"
            :src="detailMediaUrl"
            aria-label="版本音频预览"
            controls
            preload="metadata"
          />
          <video
            v-else-if="detailMediaKind === 'video' && detailMediaUrl"
            :src="detailMediaUrl"
            aria-label="版本视频预览"
            controls
            preload="metadata"
          />
          <ElEmpty
            v-else-if="!detailMediaLoading"
            description="文件预览暂不可用，可点击“导出此版本”下载"
          />
        </div>
        <ElAlert
          v-if="detailMediaError"
          type="warning"
          :closable="false"
          :title="detailMediaError"
        />
        <section
          v-for="section in detailSections"
          :key="section.title"
          class="record-section"
        >
          <h4>{{ section.title }}</h4>
          <dl class="record-fields">
            <div
              v-for="(field, index) in section.fields"
              :key="`${section.title}-${index}-${field.label}`"
              class="record-field"
            >
              <dt>{{ field.label }}</dt>
              <dd>{{ field.value }}</dd>
            </div>
          </dl>
        </section>
      </div>
      <template #footer>
        <ElButton v-if="detailArtifact" @click="exportArtifact(detailArtifact)">导出此版本</ElButton>
        <ElButton @click="detailOpen = false">关闭</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.history-view,
.history-panel,
.history-list,
.history-main,
.history-detail,
.current-section,
.versions-section,
.record-detail {
  display: grid;
}
.history-view {
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--xi-component-content-gap-md);
}
.view-heading {
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-sm);
}
.view-heading,
.history-title-row,
.history-meta,
.drawer-heading,
.section-heading,
.history-actions,
.current-grid,
.current-card {
  display: flex;
  align-items: center;
}
.view-heading,
.drawer-heading,
.section-heading,
.history-title-row {
  justify-content: space-between;
}
.view-heading h2,
.view-heading p,
.drawer-heading h3,
.section-heading h4,
.section-heading p,
.history-item p {
  margin: 0;
}
.history-actions {
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--xi-component-content-gap-xs);
}
.view-heading h2 {
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading1-font-size);
  font-weight: 600;
  line-height: 28px;
}
.view-heading p {
  margin-top: 2px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 13px;
}
.section-heading p,
.history-item p {
  margin-top: var(--xi-component-content-gap-xxs);
  color: var(--xi-semantic-color-text-secondary);
}
.history-panel {
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--xi-component-content-gap-md);
  padding: var(--xi-component-content-gap-lg);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}
.history-toolbar {
  display: grid;
  grid-template-columns: minmax(180px, 1.5fr) minmax(240px, 1.35fr) minmax(110px, 0.65fr) minmax(130px, 0.75fr) auto;
  gap: 12px;
  align-items: center;
}
.history-toolbar > * {
  width: 100%;
  min-width: 0;
}
.history-toolbar :deep(.el-date-editor) {
  width: 100%;
  min-width: 0;
}
.history-result-count {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--xi-semantic-color-text-secondary);
}
.history-result-count strong {
  margin-left: 5px;
  color: var(--xi-semantic-color-text-primary);
  font-variant-numeric: tabular-nums;
}
.history-list,
.history-detail,
.current-section,
.versions-section,
.record-detail {
  min-width: 0;
  gap: var(--xi-component-content-gap-md);
}
.history-item {
  display: grid;
  width: 100%;
  grid-template-columns: 44px minmax(0, 1fr) auto;
  gap: var(--xi-component-content-gap-lg);
  padding: var(--xi-component-content-gap-md);
  text-align: left;
  cursor: pointer;
  align-items: center;
  background: var(--xi-semantic-color-background-container);
  border: 1px solid transparent;
  border-bottom-color: var(--xi-semantic-color-border-subtle);
  border-radius: 8px;
  transition:
    background-color 180ms ease,
    border-color 180ms ease;
}
.history-item:hover,
.history-item:focus-visible {
  border-color: var(--xi-semantic-color-border-brand-default);
  background: var(--xi-semantic-color-background-brand-subtle);
}
.history-main {
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--xi-component-content-gap-xs);
}
.history-title-row {
  gap: var(--xi-component-content-gap-sm);
}
.history-title-row strong {
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading3-font-size);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.history-meta {
  min-width: 0;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}
.history-meta :deep(.el-tag) {
  max-width: 100%;
}
.history-meta :deep(.el-tag__content) {
  overflow: hidden;
  text-overflow: ellipsis;
}
.history-time {
  display: grid;
  justify-items: end;
  align-content: center;
  gap: var(--xi-component-content-gap-xxs);
  color: var(--xi-semantic-color-text-tertiary);
}
.history-time strong {
  color: var(--xi-semantic-color-text-secondary);
  font-weight: 500;
}
.history-time small {
  color: var(--xi-semantic-color-text-brand);
}
.history-pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: var(--xi-component-content-gap-sm);
  border-top: var(--xi-component-content-border);
}
.history-pagination :deep(.el-pagination) {
  flex-wrap: wrap;
  justify-content: flex-end;
  row-gap: var(--xi-component-content-gap-xs);
}
.drawer-heading {
  width: 100%;
  gap: var(--xi-component-content-gap-md);
}
.drawer-heading span {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}
.drawer-heading h3,
.section-heading h4 {
  color: var(--xi-semantic-color-text-primary);
}
.current-grid {
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-sm);
}
.current-card {
  min-width: 190px;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-sm);
  cursor: pointer;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand-subtle);
  border-radius: var(--xi-component-content-radius-control);
}
.current-card span {
  color: var(--xi-semantic-color-text-brand);
}
.versions-section :deep(.el-table__row) {
  cursor: pointer;
}
.version-summary {
  display: block;
  overflow: hidden;
  color: var(--xi-semantic-color-text-secondary);
  line-height: 1.55;
  text-overflow: ellipsis;
  white-space: normal;
}
.record-media {
  display: grid;
  min-width: 0;
  width: 100%;
  max-width: 100%;
  min-height: 96px;
  overflow: hidden;
  place-items: center;
  padding: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
}
.record-media :deep(.el-image) {
  display: block;
  width: 100%;
  max-width: 100%;
  max-height: 360px;
}
.record-media :deep(.el-image__inner),
.record-media :deep(.el-image img) {
  max-width: 100%;
  max-height: 360px;
  width: auto;
  height: auto;
  object-fit: contain;
}
.record-media audio,
.record-media video {
  display: block;
  width: 100%;
  max-width: 100%;
  max-height: 360px;
}
.record-detail :deep(.el-alert) {
  margin: 0;
}
.record-section {
  display: grid;
  gap: var(--xi-component-content-gap-xs);
}
.record-section h4 {
  margin: 0;
  color: var(--xi-semantic-color-text-primary);
}
.record-fields {
  display: grid;
  margin: 0;
  overflow: hidden;
  border-radius: 8px;
}
.record-field {
  display: grid;
  grid-template-columns: minmax(120px, 0.28fr) minmax(0, 1fr);
  border-bottom: var(--xi-semantic-color-border-subtle) 1px solid;
}
.record-field:last-child {
  border-bottom: 0;
}
.record-field dt,
.record-field dd {
  min-width: 0;
  padding: var(--xi-component-content-gap-sm);
  margin: 0;
}
.record-field dt {
  color: var(--xi-semantic-color-text-secondary);
  font-weight: 500;
}
.record-field dd {
  color: var(--xi-semantic-color-text-primary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.history-list {
  gap: 4px;
}
.history-title-row {
  justify-content: flex-start;
}
/* One neutral tile for every task type; the icon shape and the mode label tell them apart. */
.history-type-icon {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
  background: var(--xi-semantic-color-background-page);
  color: var(--xi-semantic-color-text-secondary);
}
.history-type-icon svg {
  width: 20px;
  height: 20px;
}
.history-main p {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
.history-time strong {
  font-size: 12px;
}
.history-time small {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 8px;
}
.history-time small svg {
  width: 13px;
  height: 13px;
}
.task-overview {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 20px 24px;
  margin: 0;
  padding: 22px;
  border-radius: 12px;
  background: var(--xi-semantic-color-background-page);
}
.task-overview > div {
  min-width: 0;
}
.task-overview dt {
  color: var(--xi-semantic-color-text-secondary);
  font-size: 12px;
}
.task-overview dd {
  margin: 6px 0 0;
  color: var(--xi-semantic-color-text-primary);
  font-size: 13px;
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.history-actions {
  width: 100%;
  gap: 10px;
}
.history-actions .el-button {
  margin-left: 0;
}
.history-actions .delete-task-button {
  margin-right: auto;
}
.drawer-heading {
  padding-right: 14px;
}
.drawer-heading h3 {
  margin-top: 6px;
  overflow-wrap: anywhere;
}
@media (max-width: 1100px) {
  .history-toolbar {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
  }
  .history-toolbar > :first-child {
    grid-column: span 2;
  }
  .history-toolbar :deep(.el-date-editor) {
    grid-column: 1 / -1;
    grid-row: 2;
  }
}
@media (max-width: 760px) {
  .history-item {
    grid-template-columns: 36px minmax(0, 1fr);
    padding: 12px 4px;
    gap: 12px;
  }
  .history-type-icon {
    width: 36px;
    height: 36px;
  }
  .history-time {
    grid-column: 2;
    grid-template-columns: 1fr auto;
    justify-items: start;
    width: 100%;
  }
  .history-time small {
    margin: 0;
  }
  .history-title-row {
    flex-wrap: wrap;
  }
  .task-overview {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    padding: 16px;
  }
  .history-panel {
    padding: 16px;
  }
  .history-toolbar {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
  .history-toolbar > :last-child {
    grid-column: 1 / -1;
  }
  .history-toolbar > * {
    min-width: 0;
    max-width: 100%;
    width: 100%;
  }
  .history-toolbar > :first-child {
    min-width: 0;
  }
  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--xi-component-content-gap-sm);
  }
  .history-pagination,
  .history-pagination :deep(.el-pagination) {
    justify-content: flex-start;
  }
  .record-field {
    grid-template-columns: 1fr;
  }
  .record-field dt {
    padding-bottom: var(--xi-component-content-gap-xxs);
  }
  .record-field dd {
    padding-top: var(--xi-component-content-gap-xxs);
  }
}
@media (prefers-reduced-motion: reduce) {
  .history-item {
    transition: none;
  }
}
</style>
