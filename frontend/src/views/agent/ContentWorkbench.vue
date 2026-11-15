<script setup lang="ts">
import type { Component } from 'vue'
import type { ContentServiceStatus, StageKey, TaskMode } from '@/api/agent/content'
import {
  ChatDotRound,
  Collection,
  Document,
  Edit,
  Film,
  FolderOpened,
  FullScreen,
  Goods,
  House,
  List,
  Monitor,
  Picture,
  Promotion,
  Sell,
  Stamp,
  Tickets,
  User,
} from '@element-plus/icons-vue'
import { useFullscreen } from '@vueuse/core'
import { ElNotification } from 'element-plus'
import { computed, defineAsyncComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { catalogApi } from '@/api/agent/catalog'
import { contentApi } from '@/api/agent/content'
import { loadTrialMe, trialMe } from '@/api/agent/trial'
import { isStandaloneMode } from '@/auth/workspaceMode'
import { contentCreateViewKey, contentTaskViewKey } from '@/router/videoEditorRouteSession'
import AssistantBall from './content/AssistantBall.vue'
import ContentHomeView from './content/ContentHomeView.vue'
import { normalizeStageKey } from './content/contentWorkflow'
import { useLocalAccess } from './content/localAccess'
import { pendingReviewCount } from './content/reviewBadge'
import { serviceStatusPresentation } from './content/serviceStatusPresentation'
import './content/content-theme.css'
import './content/catalog.css'

defineOptions({ name: 'ContentWorkbench' })
// Pages load on first visit (code splitting); the home page stays in the shell chunk.
const loadCatalogContentsView = () => import('./content/CatalogContentsView.vue')
const loadCatalogListingWizard = () => import('./content/CatalogListingWizard.vue')
const loadCatalogProductsView = () => import('./content/CatalogProductsView.vue')
const loadCatalogPublishView = () => import('./content/CatalogPublishView.vue')
const loadCatalogRecordsView = () => import('./content/CatalogRecordsView.vue')
const loadCatalogReviewView = () => import('./content/CatalogReviewView.vue')
const loadContentAccessView = () => import('./content/ContentAccessView.vue')
const loadContentAuditView = () => import('./content/ContentAuditView.vue')
const loadContentAutomationView = () => import('./content/ContentAutomationView.vue')
const loadContentChatView = () => import('./content/ContentChatView.vue')
const loadContentLibraryView = () => import('./content/ContentLibraryView.vue')
const loadContentServicesView = () => import('./content/ContentServicesView.vue')
const loadContentTaskView = () => import('./content/ContentTaskView.vue')
const loadContentVersionsView = () => import('./content/ContentVersionsView.vue')
const loadTrialAccountsView = () => import('./content/TrialAccountsView.vue')
const CatalogContentsView = defineAsyncComponent(loadCatalogContentsView)
const CatalogListingWizard = defineAsyncComponent(loadCatalogListingWizard)
const CatalogProductsView = defineAsyncComponent(loadCatalogProductsView)
const CatalogPublishView = defineAsyncComponent(loadCatalogPublishView)
const CatalogRecordsView = defineAsyncComponent(loadCatalogRecordsView)
const CatalogReviewView = defineAsyncComponent(loadCatalogReviewView)
const ContentAccessView = defineAsyncComponent(loadContentAccessView)
const ContentAuditView = defineAsyncComponent(loadContentAuditView)
const ContentAutomationView = defineAsyncComponent(loadContentAutomationView)
const ContentChatView = defineAsyncComponent(loadContentChatView)
const ContentLibraryView = defineAsyncComponent(loadContentLibraryView)
const ContentServicesView = defineAsyncComponent(loadContentServicesView)
const ContentTaskView = defineAsyncComponent(loadContentTaskView)
const ContentVersionsView = defineAsyncComponent(loadContentVersionsView)
const TrialAccountsView = defineAsyncComponent(loadTrialAccountsView)

type ContentView = 'home' | 'task' | 'chat' | 'library' | 'versions' | 'services' | 'access' | 'audit'
  | 'automation' | 'products' | 'review' | 'contents' | 'publish' | 'records' | 'trial'

interface NavItem { key: string, label: string, path: string, icon: Component, badge?: number }

const route = useRoute()
const router = useRouter()
const { toggle: toggleFullscreen } = useFullscreen()
const brandIcon = `${import.meta.env?.BASE_URL ?? '/'}automation-workbench-icon.svg?v=5`
const { canManageUsers, hasPermission } = useLocalAccess()
const statusLoading = ref(false)
const overallStatus = ref<ContentServiceStatus['overall']>()
const navOpen = ref(false)

const currentView = computed<ContentView>(() => (route.meta.contentView as ContentView | undefined) ?? 'home')
const automationPurpose = computed(() => route.meta.automationPurpose === 'promotion' ? 'promotion' : 'listing')
// 上架记录 / 发布历史 are tabs of their module; the old /records page still lists everything.
const recordsWorkflow = computed(() => route.meta.automationPurpose === 'promotion' || route.meta.automationPurpose === 'listing'
  ? route.meta.automationPurpose
  : undefined)
const currentMode = computed<TaskMode>(() => (route.meta.contentMode as TaskMode | undefined) ?? 'full')
const taskId = computed(() => (typeof route.params.taskId === 'string' ? route.params.taskId : ''))
const stageKey = computed<StageKey>(() =>
  typeof route.params.stageKey === 'string' ? normalizeStageKey(route.params.stageKey) : 'configuration',
)
const currentNavKey = computed(() => {
  if (currentView.value === 'publish' || currentView.value === 'automation' || (currentView.value === 'records' && recordsWorkflow.value)) {
    return automationPurpose.value
  }
  if (currentView.value === 'task') {
    return `create-${currentMode.value}`
  }
  if (currentView.value === 'trial') {
    return route.meta.trialTab === 'accounts' ? 'trial-accounts' : 'trial-operations'
  }
  return currentView.value
})
const statusPresentation = computed(() => serviceStatusPresentation(overallStatus.value))

const navGroups = computed<Array<{ title: string, items: NavItem[] }>>(() => [
  {
    title: '',
    items: [{ key: 'home', label: '工作台', path: '/agent/content', icon: House }],
  },
  {
    title: '商品与发布',
    items: [
      { key: 'products', label: '商品库', path: '/agent/content/products', icon: Goods },
      { key: 'review', label: '生成与审核', path: '/agent/content/review', icon: Stamp, badge: pendingReviewCount.value },
      { key: 'contents', label: '内容库', path: '/agent/content/contents', icon: FolderOpened },
      { key: 'listing', label: '商品上架', path: '/agent/content/listing', icon: Sell },
      { key: 'promotion', label: '内容宣传', path: '/agent/content/promotion', icon: Promotion },
    ],
  },
  {
    title: '创作工具',
    items: [
      { key: 'create-full', label: '完整流程', path: '/agent/content/create/full', icon: List },
      { key: 'create-copy', label: '文案生成', path: '/agent/content/create/copy', icon: Edit },
      { key: 'create-image', label: '图片生成', path: '/agent/content/create/image', icon: Picture },
      { key: 'create-video', label: '视频制作', path: '/agent/content/create/video', icon: Film },
      { key: 'chat', label: 'AI 对话', path: '/agent/content/chat', icon: ChatDotRound },
    ],
  },
  {
    title: '资料与设置',
    items: [
      { key: 'library', label: '资料与素材', path: '/agent/content/library', icon: Collection },
      { key: 'versions', label: '历史任务', path: '/agent/content/versions', icon: Document },
      { key: 'services', label: '模型与服务', path: '/agent/content/services', icon: Monitor },
      // On the online trial, accounts and their operation log replace the enterprise pages.
      ...(trialMe.value.enabled
        ? [
            ...(trialMe.value.is_owner ? [{ key: 'trial-accounts', label: '账号管理', path: '/agent/content/accounts', icon: User }] : []),
            { key: 'trial-operations', label: '操作记录', path: '/agent/content/operations', icon: Tickets },
          ]
        : [
            ...(!isStandaloneMode && canManageUsers.value ? [{ key: 'access', label: '人员与权限', path: '/agent/content/access', icon: User }] : []),
            ...(hasPermission('audit.read') ? [{ key: 'audit', label: '操作记录', path: '/agent/content/audit', icon: Tickets }] : []),
          ]),
    ],
  },
])

function go(item: NavItem) {
  navOpen.value = false
  if (item.key.startsWith('create-')) {
    router.push({ path: item.path, query: { new: String(Date.now()) } })
    return
  }
  router.push(item.path)
}

// Batch generation runs in the background: tell the user when it finishes, wherever they are.
let generating: { active: number, review: number, failed: number } | undefined
function noticeFinishedGeneration(items: Record<string, number | undefined>) {
  const now = {
    active: (items.queued ?? 0) + (items.running ?? 0),
    review: items.review ?? 0,
    failed: items.failed ?? 0,
  }
  const before = generating
  generating = now
  if (!before || before.active === 0 || now.active >= before.active) {
    return
  }
  const done = Math.max(0, now.review - before.review)
  const failed = Math.max(0, now.failed - before.failed)
  if (!done && !failed) {
    return
  }
  const finished = now.active === 0
  ElNotification({
    type: failed && !done ? 'error' : failed ? 'warning' : 'success',
    title: finished ? '内容生成完成' : '有内容生成好了',
    duration: 8000,
    message: h('span', [
      [done ? `${done} 条已生成，等待审核` : '', failed ? `${failed} 条生成失败` : ''].filter(Boolean).join('，'),
      finished ? '' : `，还有 ${now.active} 条在生成`,
      h('a', {
        href: '#',
        style: 'margin-left:6px',
        onClick: (event: Event) => {
          event.preventDefault()
          router.push('/agent/content/review')
        },
      }, '去审核'),
    ]),
  })
}

async function loadStatus() {
  if (statusLoading.value) {
    return
  }
  statusLoading.value = true
  const [status, overview] = await Promise.allSettled([contentApi.serviceStatus(), catalogApi.overview()])
  overallStatus.value = status.status === 'fulfilled' ? status.value.overall : undefined
  if (overview.status === 'fulfilled') {
    pendingReviewCount.value = overview.value.items.review ?? 0
    noticeFinishedGeneration(overview.value.items)
  }
  statusLoading.value = false
}

let statusTimer: ReturnType<typeof setInterval> | undefined
// Warm the cache with the everyday pages once the browser is idle, so the first
// switch to them is instant without putting them in the initial download.
function prefetchPages() {
  const pages = [loadCatalogProductsView, loadCatalogReviewView, loadCatalogContentsView, loadCatalogPublishView, loadCatalogListingWizard]
  const run = () => pages.forEach(load => void load().catch(() => {}))
  if ('requestIdleCallback' in window) {
    window.requestIdleCallback(run, { timeout: 4000 })
  }
  else {
    setTimeout(run, 1500)
  }
}

onMounted(() => {
  void loadTrialMe()
  prefetchPages()
  void loadStatus()
  statusTimer = setInterval(() => void loadStatus(), 15000)
})
onBeforeUnmount(() => clearInterval(statusTimer))
</script>

<template>
  <div class="content-agent-shell" :class="{ 'is-chat': currentView === 'chat', 'nav-open': navOpen }">
    <aside class="wb-sidebar" aria-label="主导航">
      <div class="wb-sidebar-head">
        <button type="button" class="wb-brand" @click="router.push('/agent/content')">
          <img
            :src="brandIcon"
            alt=""
            width="26"
            height="26"
          >
          <span>易发宝工作台</span>
        </button>
        <button
          type="button"
          class="wb-nav-toggle"
          aria-label="展开导航"
          @click="navOpen = !navOpen"
        >
          <span /><span /><span />
        </button>
      </div>
      <nav class="wb-nav">
        <div v-for="group in navGroups" :key="group.title || 'root'" class="wb-nav-group">
          <div v-if="group.title" class="wb-nav-title">{{ group.title }}</div>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            :class="{ active: currentNavKey === item.key }"
            :aria-current="currentNavKey === item.key ? 'page' : undefined"
            @click="go(item)"
          >
            <ElIcon><component :is="item.icon" /></ElIcon>
            <span>{{ item.label }}</span>
            <em v-if="item.badge">{{ item.badge > 99 ? '99+' : item.badge }}</em>
          </button>
        </div>
      </nav>
      <div v-if="trialMe.enabled" class="wb-account" :title="`当前登录：${trialMe.display_name}（${trialMe.username}）`">
        <ElIcon><User /></ElIcon>
        <span class="wb-ellipsis">{{ trialMe.display_name }}</span>
        <small>{{ trialMe.is_owner ? '管理员' : '试用账号' }}</small>
      </div>
      <div class="wb-sidebar-foot">
        <span class="wb-status" :class="`is-${statusPresentation.type || 'info'}`" :title="statusPresentation.label">
          <i />{{ statusPresentation.label }}
        </span>
        <button
          type="button"
          class="wb-icon-button"
          aria-label="切换全屏"
          title="切换全屏"
          @click="toggleFullscreen"
        >
          <ElIcon><FullScreen /></ElIcon>
        </button>
      </div>
    </aside>

    <main class="wb-main">
      <keep-alive :max="8">
        <ContentHomeView v-if="currentView === 'home'" />
        <CatalogProductsView v-else-if="currentView === 'products'" />
        <CatalogReviewView v-else-if="currentView === 'review'" />
        <CatalogContentsView v-else-if="currentView === 'contents'" />
        <CatalogListingWizard v-else-if="currentView === 'publish' && automationPurpose === 'listing'" />
        <CatalogPublishView v-else-if="currentView === 'publish'" />
        <CatalogRecordsView v-else-if="currentView === 'records'" :key="recordsWorkflow || 'all'" :fixed="recordsWorkflow" />
        <ContentChatView v-else-if="currentView === 'chat'" />
        <ContentTaskView
          v-else-if="currentView === 'task'"
          :key="contentTaskViewKey(
            route.fullPath,
            taskId ? `${taskId}:${currentMode}` : contentCreateViewKey(currentMode, route.query.new),
          )"
          :mode="currentMode"
          :task-id="taskId"
          :stage-key="stageKey"
        />
        <ContentLibraryView v-else-if="currentView === 'library'" />
        <ContentAutomationView v-else-if="currentView === 'automation'" :key="`rules-${automationPurpose}`" :purpose="automationPurpose" />
        <ContentVersionsView v-else-if="currentView === 'versions'" />
        <ContentAccessView v-else-if="currentView === 'access'" />
        <ContentAuditView v-else-if="currentView === 'audit'" />
        <TrialAccountsView v-else-if="currentView === 'trial'" />
        <ContentServicesView v-else />
      </keep-alive>
    </main>
    <!-- The operations assistant floats over every page; AI 对话 is the same thing full-size. -->
    <AssistantBall :hidden="currentView === 'chat'" />
  </div>
</template>

<style scoped>
.content-agent-shell {
  display: grid;
  grid-template-columns: var(--wb-sidebar-width) minmax(0, 1fr);
  align-items: start;
  min-height: 100dvh;
  color: var(--xi-semantic-color-text-primary);
  background: var(--xi-component-page-background);
}
.wb-sidebar {
  position: sticky;
  top: 0;
  display: flex;
  height: 100dvh;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid var(--xi-semantic-color-border-subtle);
}
.wb-sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 14px 12px;
}
.wb-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0;
  color: var(--xi-semantic-color-text-primary);
  font: inherit;
  font-size: 15px;
  font-weight: 600;
  background: none;
  border: 0;
  cursor: pointer;
}
.wb-brand img {
  display: block;
}
.wb-nav-toggle {
  display: none;
}
.wb-nav {
  flex: 1;
  overflow-y: auto;
  padding: 4px 10px 12px;
}
.wb-nav-group + .wb-nav-group {
  margin-top: 14px;
}
.wb-nav-title {
  padding: 0 10px 6px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
}
.wb-nav button {
  display: flex;
  align-items: center;
  width: 100%;
  gap: 10px;
  height: 34px;
  padding: 0 10px;
  color: var(--xi-semantic-color-text-secondary);
  font: inherit;
  font-size: 14px;
  text-align: left;
  background: none;
  border: 0;
  border-radius: 4px;
  cursor: pointer;
}
.wb-nav button .el-icon {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 16px;
}
.wb-nav button:hover {
  background: var(--wb-hover);
}
.wb-nav button.active {
  color: var(--xi-semantic-color-text-primary);
  font-weight: 600;
  background: var(--wb-selected);
}
.wb-nav button.active .el-icon {
  color: var(--xi-semantic-color-action-primary-default);
}
.wb-nav button:focus-visible,
.wb-brand:focus-visible,
.wb-icon-button:focus-visible {
  outline: 2px solid var(--xi-semantic-color-action-primary-default);
  outline-offset: 1px;
}
.wb-nav em {
  margin-left: auto;
  min-width: 20px;
  padding: 0 6px;
  color: #fff;
  font-size: 12px;
  font-style: normal;
  font-weight: 600;
  line-height: 18px;
  text-align: center;
  background: var(--xi-semantic-color-action-primary-default);
  border-radius: 9px;
}
.wb-account {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 10px 8px;
  padding: 8px 10px;
  color: var(--xi-semantic-color-text-primary);
  font-size: 13px;
  background: var(--wb-hover);
  border-radius: 4px;
}
.wb-account .el-icon {
  color: var(--xi-semantic-color-text-tertiary);
}
.wb-account small {
  margin-left: auto;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  white-space: nowrap;
}
.wb-sidebar-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 14px;
  border-top: 1px solid var(--xi-semantic-color-border-subtle);
}
.wb-status {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 6px;
  overflow: hidden;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.wb-status i {
  width: 6px;
  height: 6px;
  flex: none;
  background: var(--xi-semantic-color-feedback-info-default);
  border-radius: 50%;
}
.wb-status.is-success i {
  background: var(--xi-semantic-color-feedback-success-default);
}
.wb-status.is-warning i {
  background: var(--xi-semantic-color-feedback-warning-default);
}
.wb-status.is-danger i {
  background: var(--xi-semantic-color-feedback-danger-default);
}
.wb-icon-button {
  display: grid;
  width: 28px;
  height: 28px;
  flex: none;
  place-items: center;
  color: var(--xi-semantic-color-text-tertiary);
  background: none;
  border: 0;
  border-radius: 4px;
  cursor: pointer;
}
.wb-icon-button:hover {
  color: var(--xi-semantic-color-text-primary);
  background: var(--wb-hover);
}
.wb-main {
  min-width: 0;
  padding: 24px 32px 40px;
}
/* Every page shares the workbench column (.wb-page in catalog.css), so switching pages
 * never moves the content sideways. The video editor and chat are workspaces and stay full width. */
.wb-main > :not(.wb-page, .has-video-editor, .content-chat-view) {
  max-width: 1440px;
  margin-inline: auto;
}
.content-agent-shell.is-chat {
  height: 100dvh;
}
.is-chat .wb-main {
  display: flex;
  height: 100dvh;
  box-sizing: border-box;
  flex-direction: column;
  padding: 12px 16px;
}

@media (width < 900px) {
  .content-agent-shell {
    display: block;
  }
  .wb-sidebar {
    z-index: 20;
    height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--xi-semantic-color-border-subtle);
  }
  .wb-sidebar-head {
    padding: 10px 16px;
  }
  .wb-nav-toggle {
    display: grid;
    width: 36px;
    height: 36px;
    place-content: center;
    gap: 4px;
    background: none;
    border: 0;
    cursor: pointer;
  }
  .wb-nav-toggle span {
    display: block;
    width: 18px;
    height: 2px;
    background: var(--xi-semantic-color-text-secondary);
  }
  .wb-nav,
  .wb-account,
  .wb-sidebar-foot {
    display: none;
  }
  .nav-open .wb-nav {
    display: block;
    max-height: 70dvh;
  }
  .wb-main {
    padding: 16px;
  }
  .is-chat .wb-main {
    height: calc(100dvh - 57px);
  }
}
</style>
