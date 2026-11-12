<script setup lang="ts">
import type { Component } from 'vue'
import type { CatalogOverview, PublishRecord } from '@/api/agent/catalog'
import type { ContentTask, TaskMode } from '@/api/agent/content'
import { ArrowRight, ChatDotRound, Edit, Film, List, Picture } from '@element-plus/icons-vue'
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { catalogApi, recordStatusLabels } from '@/api/agent/catalog'
import { contentApi } from '@/api/agent/content'
import { platformInitial, platformName, recordStatusClass, shortTime } from './catalogPresentation'
import { formatDate, modeLabels, modeStageKeys, normalizeStageKey, recentContentTasks, stageDefinitions } from './contentWorkflow'

defineOptions({ name: 'ContentHomeView' })

interface Tool { key: string, name: string, description: string, icon: Component, mode?: TaskMode, path?: string }

const router = useRouter()
const loading = ref(true)
const tasks = ref<ContentTask[]>([])
const overview = ref<CatalogOverview>()
const records = ref<PublishRecord[]>([])

const tools: Tool[] = [
  { key: 'full', mode: 'full', icon: List, name: '完整流程', description: '从选题、文案、配图到成片的分步创作' },
  { key: 'copy', mode: 'copy', icon: Edit, name: '文案生成', description: '标题、正文，按要求改写' },
  { key: 'image', mode: 'image', icon: Picture, name: '图片生成', description: '按描述或参考图出图' },
  { key: 'video', mode: 'video', icon: Film, name: '视频制作', description: '用已有素材剪成短视频' },
  { key: 'chat', path: '/agent/content/chat', icon: ChatDotRound, name: 'AI 对话', description: '读资料、理思路、问问题' },
]

const recentTasks = computed(() => recentContentTasks(tasks.value).slice(0, 6))
const succeeded = (workflow: 'listing' | 'promotion') => overview.value?.records[workflow]?.succeeded ?? 0
const pipeline = computed(() => {
  const items = overview.value?.items ?? {}
  const listing = overview.value?.records.listing ?? {}
  const promotion = overview.value?.records.promotion ?? {}
  const working = (items.queued ?? 0) + (items.running ?? 0)
  return [
    { label: '商品', value: overview.value?.products ?? 0, note: '商品库', path: '/agent/content/products' },
    { label: '待审核', value: items.review ?? 0, note: working ? `${working} 个生成中` : (items.failed ? `${items.failed} 个失败` : '生成与审核'), path: '/agent/content/review', highlight: (items.review ?? 0) > 0 },
    { label: '已入库内容', value: overview.value?.contents ?? 0, note: '内容库', path: '/agent/content/contents' },
    { label: '已上架', value: succeeded('listing'), note: listing.unknown ? `${listing.unknown} 条待核查` : (listing.failed ? `${listing.failed} 条失败` : '闲鱼'), path: '/agent/content/listing' },
    { label: '已宣传', value: succeeded('promotion'), note: promotion.unknown ? `${promotion.unknown} 条待核查` : (promotion.failed ? `${promotion.failed} 条失败` : '抖音 · 小红书 · 快手'), path: '/agent/content/promotion' },
  ]
})

function taskStage(task: ContentTask) {
  if (task.archived) {
    return '已归档'
  }
  const steps = modeStageKeys[task.mode]
  const index = Math.max(1, steps.indexOf(normalizeStageKey(task.current_stage)) + 1)
  const label = stageDefinitions.find(stage => stage.key === normalizeStageKey(task.current_stage))?.shortLabel || '配置'
  return `${label} ${index}/${steps.length}`
}

function openTool(tool: Tool) {
  if (tool.mode) {
    router.push({ path: `/agent/content/create/${tool.mode}`, query: { new: String(Date.now()) } })
  }
  else {
    router.push(tool.path!)
  }
}

function continueTask(task: ContentTask) {
  if (task.can_edit === false) {
    router.push({ path: '/agent/content/versions', query: { taskId: task.id } })
    return
  }
  const stage = task.archived || task.status === 'archived' ? 'export' : normalizeStageKey(task.current_stage)
  router.push(`/agent/content/tasks/${task.id}/stages/${stage}`)
}

async function load(silent = false) {
  if (!silent) {
    loading.value = true
  }
  const [taskResult, overviewResult, recordResult] = await Promise.allSettled([
    contentApi.listTasks({ includeArchived: true }),
    catalogApi.overview(),
    catalogApi.records({ page_size: 6 }),
  ])
  if (taskResult.status === 'fulfilled') {
    tasks.value = taskResult.value
  }
  if (overviewResult.status === 'fulfilled') {
    overview.value = overviewResult.value
  }
  if (recordResult.status === 'fulfilled') {
    records.value = recordResult.value.items
  }
  loading.value = false
}

let skipNextActivate = false
onMounted(() => {
  skipNextActivate = true
  void load()
})
onActivated(() => {
  if (skipNextActivate) {
    skipNextActivate = false
    return
  }
  void load(true)
})
</script>

<template>
  <div class="wb-page home" :aria-busy="loading">
    <header class="wb-page-head">
      <div>
        <h1>工作台</h1>
        <p>商品 → 批量生成 → 人工审核 → 入库 → 上架 / 宣传</p>
      </div>
      <div class="wb-page-actions">
        <ElButton @click="router.push('/agent/content/review')">去审核</ElButton>
        <ElButton type="primary" @click="router.push('/agent/content/products')">选商品批量生成</ElButton>
      </div>
    </header>

    <section class="wb-panel pipeline" aria-label="流程概况">
      <button
        v-for="(step, index) in pipeline"
        :key="step.label"
        type="button"
        class="pipeline-step"
        :class="{ highlight: step.highlight }"
        @click="router.push(step.path)"
      >
        <span class="step-label">{{ step.label }}</span>
        <strong class="wb-num">{{ loading ? '–' : step.value }}</strong>
        <small>{{ step.note }}</small>
        <ElIcon v-if="index < pipeline.length - 1" class="step-arrow"><ArrowRight /></ElIcon>
      </button>
    </section>

    <section aria-labelledby="tools-title">
      <h2 id="tools-title" class="block-title">创作工具</h2>
      <div class="tool-grid">
        <button
          v-for="tool in tools"
          :key="tool.key"
          type="button"
          class="tool"
          @click="openTool(tool)"
        >
          <span class="tool-icon"><ElIcon><component :is="tool.icon" /></ElIcon></span>
          <span class="tool-text">
            <strong>{{ tool.name }}</strong>
            <small>{{ tool.description }}</small>
          </span>
        </button>
      </div>
    </section>

    <div class="home-columns">
      <section class="wb-panel">
        <div class="wb-panel-head">
          <h2>最近的创作任务</h2>
          <ElButton link type="primary" @click="router.push('/agent/content/versions')">全部</ElButton>
        </div>
        <div v-if="!loading && !recentTasks.length" class="wb-empty">还没有创作任务</div>
        <button
          v-for="task in recentTasks"
          :key="task.id"
          type="button"
          class="list-line"
          @click="continueTask(task)"
        >
          <span class="line-main">
            <strong class="wb-ellipsis">{{ task.name }}</strong>
            <small class="wb-muted wb-ellipsis">{{ modeLabels[task.mode] }}{{ task.product ? ` · ${task.product}` : '' }}</small>
          </span>
          <span class="wb-muted line-meta">{{ taskStage(task) }}</span>
          <span class="wb-muted wb-num line-time">{{ formatDate(task.updated_at || task.created_at).slice(5, 16) }}</span>
        </button>
      </section>

      <section class="wb-panel">
        <div class="wb-panel-head">
          <h2>最近的发布</h2>
          <ElButton link type="primary" @click="router.push('/agent/content/records')">全部</ElButton>
        </div>
        <div v-if="!loading && !records.length" class="wb-empty">还没有发布记录</div>
        <div v-for="record in records" :key="record.id" class="list-line">
          <span class="wb-platform" :class="`p-${record.platform}`"><i>{{ platformInitial[record.platform] }}</i></span>
          <span class="line-main">
            <strong class="wb-ellipsis">{{ record.product_name }}</strong>
            <small class="wb-muted wb-ellipsis">{{ platformName(record.platform) }} · {{ record.title }}</small>
          </span>
          <span class="wb-pill" :class="recordStatusClass(record.status)">{{ recordStatusLabels[record.status] }}</span>
          <span class="wb-muted wb-num line-time">{{ shortTime(record.created_at).slice(5) }}</span>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.pipeline {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
}
.pipeline-step {
  position: relative;
  display: grid;
  justify-items: start;
  gap: 2px;
  padding: 16px 20px;
  color: inherit;
  font: inherit;
  text-align: left;
  background: none;
  border: 0;
  cursor: pointer;
}
.pipeline-step + .pipeline-step {
  border-left: 1px solid var(--xi-semantic-color-border-subtle);
}
.pipeline-step:hover {
  background: var(--wb-hover);
}
.pipeline-step:focus-visible {
  outline: 2px solid var(--xi-semantic-color-action-primary-default);
  outline-offset: -2px;
}
.step-label {
  color: var(--xi-semantic-color-text-secondary);
  font-size: 13px;
}
.pipeline-step strong {
  font-size: 26px;
  font-weight: 600;
  line-height: 36px;
}
.pipeline-step.highlight strong {
  color: var(--xi-semantic-color-feedback-warning-text);
}
.pipeline-step small {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
}
.step-arrow {
  position: absolute;
  top: 50%;
  right: -8px;
  z-index: 1;
  width: 16px;
  height: 16px;
  color: var(--xi-semantic-color-text-placeholder);
  font-size: 12px;
  background: #fff;
  border-radius: 50%;
  transform: translateY(-50%);
}
.block-title {
  margin: 4px 0 10px;
  font-size: 14px;
  font-weight: 600;
}
.tool-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}
.tool {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
  padding: 14px;
  color: inherit;
  font: inherit;
  text-align: left;
  background: #fff;
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 6px;
  cursor: pointer;
}
.tool:hover {
  border-color: var(--xi-semantic-color-border-default);
  background: #fcfcfd;
}
.tool:hover .tool-icon {
  color: var(--xi-semantic-color-action-primary-default);
}
.tool:focus-visible {
  outline: 2px solid var(--xi-semantic-color-action-primary-default);
  outline-offset: 1px;
}
.tool-icon {
  display: grid;
  width: 32px;
  height: 32px;
  flex: none;
  place-items: center;
  color: var(--xi-semantic-color-text-secondary);
  font-size: 16px;
  background: var(--xi-semantic-color-background-page);
  border-radius: 4px;
}
.tool-text {
  display: grid;
  min-width: 0;
  gap: 3px;
}
.tool-text strong {
  font-size: 14px;
  font-weight: 600;
}
.tool-text small {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  line-height: 1.5;
}
.home-columns {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.list-line {
  display: flex;
  align-items: center;
  width: 100%;
  gap: 12px;
  padding: 10px 16px;
  color: inherit;
  font: inherit;
  text-align: left;
  background: none;
  border: 0;
  border-bottom: 1px solid var(--xi-semantic-color-divider-default);
}
button.list-line {
  cursor: pointer;
}
button.list-line:hover {
  background: var(--wb-hover);
}
.list-line:last-child {
  border-bottom: 0;
}
.line-main {
  display: grid;
  flex: 1;
  min-width: 0;
  gap: 2px;
}
.line-main strong {
  font-size: 13px;
  font-weight: 500;
}
.line-main small {
  font-size: 12px;
}
.line-meta,
.line-time {
  flex: none;
  font-size: 12px;
}
@media (width < 1200px) {
  .tool-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (width < 900px) {
  .pipeline {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .pipeline-step + .pipeline-step {
    border-left: 0;
  }
  .pipeline-step {
    border-bottom: 1px solid var(--xi-semantic-color-border-subtle);
  }
  .step-arrow {
    display: none;
  }
  .home-columns {
    grid-template-columns: 1fr;
  }
}
@media (width < 600px) {
  .tool-grid {
    grid-template-columns: 1fr 1fr;
  }
  .line-meta {
    display: none;
  }
}
</style>
