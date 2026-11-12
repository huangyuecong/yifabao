import type { App } from '@/vue-export'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
// 需要全局引入一些组件，如ElScrollbar，不然一些下拉项样式有问题
// import { ElButton, ElLoading, ElScrollbar } from 'element-plus'
import ElementPlus from 'element-plus'

// const plugins = [ElLoading]

// const components = [ElScrollbar, ElButton]

export const setupElementPlus = (app: App<Element>) => {
  // plugins.forEach((plugin) => {
  //   app.use(plugin)
  // })

  // components.forEach((component) => {
  //   app.component(component.name, component)
  // })
  // 全局注册 ElementPlus Icon
  for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component)
  }
  app.use(ElementPlus)
}
