import { Layout } from '@/utils/routerHelper'
import { isStandaloneMode } from '@/auth/workspaceMode'
import contentAgentRoutes from './contentRoutes'
// import { createRemoteComponent } from '@module-federation/bridge-vue3'
// import { init, loadRemote } from '@module-federation/enhanced/runtime'

const { t } = useI18n()
const isAgentDevMode = import.meta.env.VITE_AGENT_DEV_MODE === 'true' || import.meta.env.VITE_CONTENT_AUTH_MODE === 'entry'

const remainingRouter: AppRouteRecordRaw[] = [
  {
    path: '/entry-auth',
    name: 'ContentEntryAuth',
    component: () => import('@/views/agent/ContentEntryAuth.vue'),
    meta: { hidden: true, title: '统一登录' },
  },
  {
    path: '/admin',
    redirect: '/agent/content',
    name: 'ContentAdminEntry',
    meta: { hidden: true },
  },
  {
    // Keep bookmarks from the previous /content/admin router base working.
    path: '/admin/agent/:path(.*)',
    redirect: to => ({ path: `/agent/${to.params.path}`, query: to.query, hash: to.hash }),
    name: 'ContentLegacyEntry',
    meta: { hidden: true },
  },
  {
    path: '/redirect',
    component: Layout,
    name: 'RedirectParent',
    children: [
      {
        path: '/redirect/:path(.*)',
        name: 'Redirect',
        component: () => import('@/views/Redirect/Redirect.vue'),
        meta: {},
      },
    ],
    meta: {
      hidden: true,
      noTagsView: true,
    },
  },
  {
    path: '/',
    component: Layout,
    redirect: isStandaloneMode || isAgentDevMode ? '/agent/content' : '/index',
    name: 'Home',
    meta: {},
    children: [
      {
        path: 'index',
        component: () => import('@/views/Home/Index.vue'),
        name: 'Index',
        meta: {
          title: t('router.home'),
          noCache: false,
          affix: true,
          icon: 'CommunicationIconAction',
        },
      },
    ],
  },
  {
    path: '/agent',
    component: () => import('@/layout/ContentLayout.vue'),
    redirect: '/agent/content',
    name: 'AgentWorkbench',
    meta: {
      title: '易发宝工作台',
      icon: 'ep:magic-stick',
    },
    children: [
      ...contentAgentRoutes,
    ],
  },
  {
    path: '/user',
    component: Layout,
    name: 'UserInfo',
    meta: {
      hidden: true,
    },
    children: [
      {
        path: 'profile',
        component: () => import('@/views/Profile/Index.vue'),
        name: 'Profile',
        meta: {
          canTo: true,
          hidden: true,
          noTagsView: false,
          icon: 'ep:user',
          title: t('common.profile'),
        },
      },

    ],
  },

  {
    path: '/login',
    component: () => import('@/views/Login/Login.vue'),
    name: 'Login',
    meta: {
      hidden: true,
      title: t('router.login'),
      noTagsView: true,
    },
  },
  {
    path: '/403',
    component: () => import('@/views/Error/403.vue'),
    name: 'NoAccess',
    meta: {
      hidden: true,
      title: '403',
      noTagsView: true,
    },
  },
  {
    path: '/404',
    component: () => import('@/views/Error/404.vue'),
    name: 'NoFound',
    meta: {
      hidden: true,
      title: '404',
      noTagsView: true,
    },
  },
  {
    path: '/500',
    component: () => import('@/views/Error/500.vue'),
    name: 'Error',
    meta: {
      hidden: true,
      title: '500',
      noTagsView: true,
    },
  },
  {
    path: '/:pathMatch(.*)*',
    component: () => import('@/views/Error/404.vue'),
    name: '',
    meta: {
      title: '404',
      hidden: true,
      breadcrumb: false,
    },
  },
]

export default remainingRouter
