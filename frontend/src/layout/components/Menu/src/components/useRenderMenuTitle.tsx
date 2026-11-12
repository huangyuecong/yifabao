// import AuthorizationIconAction from '@/assets/imgs/AuthorizationIconAction.png'
// import AuthorizationIconDisabled from '@/assets/imgs/AuthorizationIconDisabled.png'

// import AuthorizeIconAction from '@/assets/imgs/AuthorizeIconAction.png'
// import AuthorizeIconDisabled from '@/assets/imgs/AuthorizeIconDisabled.png'
// import CommunicationIconAction from '@/assets/imgs/CommunicationIconAction.png'
// import CommunicationIconDisabled from '@/assets/imgs/CommunicationIconDisabled.png'
// import RecordIconAction from '@/assets/imgs/RecordIconAction.png'
// import RecordIconDisabled from '@/assets/imgs/RecordIconDisabled.png'
// import SettingsIconAction from '@/assets/imgs/SettingsIconAction.png'
// import SettingsIconDisabled from '@/assets/imgs/SettingsIconDisabled.png'
// import type { RouteMeta } from 'vue-router'
import { Icon } from '@/components/Icon'
import { useI18n } from '@/hooks/web/useI18n'

// const iconsSet = {
//   AuthorizationIconAction,
//   AuthorizationIconDisabled,
//   CommunicationIconAction,
//   CommunicationIconDisabled,
//   RecordIconAction,
//   RecordIconDisabled,
//   SettingsIconAction,
//   SettingsIconDisabled,
//   AuthorizeIconAction,
//   AuthorizeIconDisabled,
// }

export const useRenderMenuTitle = () => {
  const renderMenuTitle = (v: AppRouteRecordRaw) => {
    // console.log(tt)
    const { t } = useI18n()
    // const route = useRoute()
    const { title = 'Please set title', icon } = v.meta

    // console.log(route.meta, v.meta)

    // const isActive = route.meta.icon === v.meta.icon && route.meta.title === v.meta.title

    return icon
      ? (
          <div class="flex items-center">
            <Icon icon={icon as string} class="mr-2" />
            <span class="v-menu__title overflow-ellipsis flex items-center overflow-hidden whitespace-nowrap">
              {t(title as string)}
              {v.pathType === 'external' && <Icon icon="mdi:arrow-top-right" />}
            </span>
          </div>
        )
      : (
          <span class="v-menu__title overflow-ellipsis flex items-center overflow-hidden whitespace-nowrap">
            {t(title as string)}
            {v.pathType === 'external' && <Icon icon="mdi:arrow-top-right" />}
          </span>
        )
  }

  return {
    renderMenuTitle,
  }
}
