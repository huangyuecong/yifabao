import { h } from '@/vue-export'
import type { VNode } from '@/vue-export'
import { Icon } from '@/components/Icon'
import { IconTypes } from '@/types/icon'

export const useIcon = (props: IconTypes): VNode => {
  return h(Icon, props)
}
