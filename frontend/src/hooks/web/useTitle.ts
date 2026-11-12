import { watch, ref } from '@/vue-export'
import { isString } from '@/utils/is'
import { useAppStoreWithOut } from '@/store/modules/app'

const appStore = useAppStoreWithOut()

export const useTitle = (newTitle?: string) => {
  const { t } = useI18n()
  const pageTitle = newTitle ? t(newTitle) : ''
  const title = ref(
    pageTitle && pageTitle !== appStore.getTitle
      ? `${appStore.getTitle} - ${pageTitle}`
      : appStore.getTitle
  )

  watch(
    title,
    (n, o) => {
      if (isString(n) && n !== o && document) {
        document.title = n
      }
    },
    { immediate: true }
  )

  return title
}
