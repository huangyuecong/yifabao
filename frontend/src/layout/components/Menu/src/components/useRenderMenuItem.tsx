import { isUrl } from '@/utils/is'
import { pathResolve } from '@/utils/routerHelper'
import { ElMenuItem, ElSubMenu } from 'element-plus'
import qs from 'qs'
import { hasOneShowingChild } from '../helper'
import { useRenderMenuTitle } from './useRenderMenuTitle'

const { renderMenuTitle } = useRenderMenuTitle()

export const useRenderMenuItem = () =>
// allRouters: AppRouteRecordRaw[] = [],
{
  const renderMenuItem = (routers: AppRouteRecordRaw[], parentPath = '/') => {
    return routers
      .filter(v => !v.meta?.hidden)
      .map((v) => {
        const meta = v.meta ?? {}
        const { oneShowingChild, onlyOneChild } = hasOneShowingChild(v.children, v)
        // getAllParentPath<AppRouteRecordRaw>(allRouters, v.path).join('/')
        let fullPath
        switch (v.pathType) {
          case 'iframe':
            fullPath = `/iframe${qs.stringify({
              src: isUrl(v.path) ? v.path : window.location.origin + v.path,
            }, {
              addQueryPrefix: true,
            })}`
            break
          case 'micro':

            break
          case 'external':
            fullPath = isUrl(v.path) ? v.path : window.location.origin + v.path
            break
          case 'module_federation':
            fullPath = pathResolve(parentPath, v.path)
            break
          default:
            fullPath = pathResolve(parentPath, v.path)
        }

        if (
          oneShowingChild
          && (!onlyOneChild?.children || onlyOneChild?.noShowingChildren)
          && !meta?.alwaysShow
        ) {
          return (
            <ElMenuItem
              index={onlyOneChild ? pathResolve(fullPath, onlyOneChild.path) : fullPath}
            >
              {{
                default: () => renderMenuTitle(onlyOneChild || v),
              }}
            </ElMenuItem>
          )
        }
        else {
          return (
            <ElSubMenu index={fullPath}>
              {{
                title: () => renderMenuTitle(v),
                default: () => renderMenuItem(v.children!, fullPath),
              }}
            </ElSubMenu>
          )
        }
      })
  }

  return {
    renderMenuItem,
  }
}
