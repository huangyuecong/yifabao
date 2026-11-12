import type { App } from '@/vue-export'
import { hasPermi } from './permission/hasPermi'
import { hasRole } from './permission/hasRole'

/**
 * 导出指令：v-xxx
 * @methods hasRole 用户权限，用法: v-hasRole
 * @methods hasPermi 按钮权限，用法: v-hasPermi
 */
export const setupAuth = (app: App<Element>) => {
  // 给外部APP注册权限验证指令
  console.log('给APP注册权限指令', app)
  hasRole(app)
  hasPermi(app)
}

/**
 * 导出指令：v-mountedFocus
 */
export const setupMountedFocus = (app: App<Element>) => {
  app.directive('mountedFocus', {
    mounted(el) {
      el.focus()
    },
  })
}
