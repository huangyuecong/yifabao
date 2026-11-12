import type { ContentAccess, ContentPermission } from '@/api/agent/access'
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, readonly, ref } from 'vue'
import { contentAccessApi } from '@/api/agent/access'

const access = ref<ContentAccess>()
const loading = ref(false)
const error = ref('')
let pending: Promise<void> | undefined

export function accessErrorStatus(reason: unknown): number | undefined {
  return reason && typeof reason === 'object' && 'status' in reason ? Number(reason.status) : undefined
}

export function refreshLocalAccess() {
  if (pending) {
    return pending
  }
  loading.value = true
  pending = (async () => {
    try {
      access.value = await contentAccessApi.me()
      error.value = ''
    }
    catch (reason) {
      access.value = undefined
      error.value = reason instanceof Error ? reason.message : '暂时无法读取权限，请重试'
    }
    finally {
      loading.value = false
      pending = undefined
    }
  })()
  return pending
}

export function useLocalAccess() {
  let active = true
  const refreshOnFocus = () => {
    if (active) {
      void refreshLocalAccess()
    }
  }
  onMounted(() => {
    void refreshLocalAccess()
    if (typeof window !== 'undefined') {
      window.addEventListener('focus', refreshOnFocus)
    }
  })
  onActivated(() => {
    active = true
    void refreshLocalAccess()
  })
  onDeactivated(() => {
    active = false
  })
  onBeforeUnmount(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('focus', refreshOnFocus)
    }
  })
  return {
    access: readonly(access),
    loading: readonly(loading),
    message: computed(() => error.value || access.value?.message || ''),
    canManageUsers: computed(() => access.value?.authentication_ready === true && access.value.can_manage_users),
    hasPermission: (permission: ContentPermission) => access.value?.permissions.includes(permission) === true,
    refresh: refreshLocalAccess,
  }
}
