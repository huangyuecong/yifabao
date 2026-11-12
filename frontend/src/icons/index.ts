import type { IconifyJSON } from '@iconify/iconify'
import Iconify from '@iconify/iconify'
import menuIcons from './menu-icons.gen.json'

// 注册菜单用到的离线图标小集合（由 scripts/gen-menu-icons.mjs 从 @iconify/json 裁剪生成）。
// 注册后侧边栏等处的 tabler:/ant-design: 图标可离线渲染，无需请求 iconify 在线 API。
export function setupMenuIcons() {
  ;(menuIcons as IconifyJSON[]).forEach(collection => Iconify.addCollection(collection))
}
