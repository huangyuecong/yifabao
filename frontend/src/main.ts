// 引入unocss css
import '@/plugins/unocss'
// 引入element-plus css
import 'element-plus/dist/index.css'
// 导入全局的svg图标
import '@/plugins/svgIcon'

// 初始化多语言
import { setupI18n } from '@/plugins/vueI18n'

// 引入状态管理
import { setupStore } from '@/store'

// 全局组件
import { setupGlobCom } from '@/components'

// 引入 element-plus
import { setupElementPlus } from '@/plugins/elementPlus'

// 引入全局样式
import '@/styles/index.scss'



// 引入动画
import '@/plugins/animate.css'

// 路由
import router, { setupRouter } from '@/router'

// 权限
// 指令
import { setupAuth, setupMountedFocus } from '@/directives'

import { createApp } from '@/vue-export'

import App from './App.vue'

import './permission'

// import '@/plugins/tongji' // 百度统计
import Logger from '@/utils/Logger'

import microApp from '@micro-zoe/micro-app' // 解决v-html 的安全隐患

import VueDOMPurifyHTML from 'vue-dompurify-html'
// 子系统使用组件
import { DictTag } from './components/DictTag'
// Apply the local theme after Element Plus and the global styles.
import '@/styles/brand-theme.scss'
import { setupMenuIcons } from '@/icons'

setupMenuIcons()

microApp.start()

// 创建实例
const setupAll = async () => {
  const app = createApp(App)

  // 全局属性
  app.config.globalProperties.window = window

  await setupI18n(app)

  setupStore(app)

  setupGlobCom(app)

  setupElementPlus(app)

  setupRouter(app)

  // directives 指令
  setupAuth(app)
  setupMountedFocus(app) // v2.3.0 +

  await router.isReady()

  app.use(VueDOMPurifyHTML)

  // 子系统使用组件
  app.component('DictTag', DictTag)

  app.mount('#app')
}

setupAll().catch((err) => {
  // 启动阶段（如 router.isReady 前的守卫）抛异常时，app.mount 不会执行，页面白屏。
  // 这里兜底打日志，避免只剩一条 Uncaught (in promise) 无从排查。
  console.error('[应用启动失败]', err)
})

Logger.prettyPrimary(`欢迎使用`, import.meta.env.VITE_APP_TITLE)
