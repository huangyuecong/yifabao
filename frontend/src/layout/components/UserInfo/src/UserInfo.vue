<script lang="ts" setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import avatarImg from '@/assets/imgs/avatar.gif'

import { entryIdentity, isEntrySso, logoutFromEntry } from '@/auth/entrySession'
import { useDesign } from '@/hooks/web/useDesign'
import { useLockStore } from '@/store/modules/lock'
import { useTagsViewStore } from '@/store/modules/tagsView'
import { useUserStore } from '@/store/modules/user'
// import LockDialog from './components/LockDialog.vue'
// import LockPage from './components/LockPage.vue'
import { computed, defineOptions } from '@/vue-export'

defineOptions({ name: 'UserInfo' })

const { t } = useI18n()

const { push, replace } = useRouter()

const userStore = useUserStore()

const tagsViewStore = useTagsViewStore()

const { getPrefixCls } = useDesign()

const prefixCls = getPrefixCls('user-info')

const avatar = computed(() => isEntrySso ? avatarImg : userStore.user.avatar || avatarImg)
const userName = computed(() => isEntrySso ? entryIdentity.value?.display_name || '统一登录用户' : userStore.user.nickname ?? 'Admin')

// 锁定屏幕
const lockStore = useLockStore()
const getIsLock = computed(() => lockStore.getLockInfo?.isLock ?? false)
// const dialogVisible = ref<boolean>(false)
// const lockScreen = () => {
//   dialogVisible.value = true
// }

const loginOut = async () => {
  try {
    await ElMessageBox.confirm(t('common.loginOutMessage'), t('common.reminder'), {
      confirmButtonText: t('common.ok'),
      cancelButtonText: t('common.cancel'),
      type: 'warning',
    })
    if (isEntrySso) {
      await logoutFromEntry()
      return
    }
    await userStore.loginOut()
    tagsViewStore.delAllViews()
    replace('/login?redirect=/index')
  }
  catch (reason) {
    if (isEntrySso && reason !== 'cancel' && reason !== 'close') {
      ElMessage.error('退出登录失败，请稍后重试')
    }
  }
}
const toProfile = async () => {
  push('/user/profile')
}
</script>

<template>
  <ElDropdown class="custom-hover" :class="prefixCls" trigger="click">
    <div class="ml-12px flex items-center pr-10px">
      <ElAvatar
        :src="avatar"
        alt=""
        class="rounded-[50%]"
        :style="{ '--el-avatar-size': '32px' }"
      />
      <span class="pl-[8px] text-12px text-[#222] <lg:hidden">
        {{ userName }}
        <Icon icon="svg-icon:caret-bottom" :size="12" class="ml-[4px] align--2px" />
      </span>
    </div>
    <template #dropdown>
      <ElDropdownMenu>
        <ElDropdownItem v-if="!isEntrySso">
          <Icon icon="ep:tools" />
          <div @click="toProfile">{{ t('common.profile') }}</div>
        </ElDropdownItem>
        <!-- <ElDropdownItem>
          <Icon icon="ep:menu" />
          <div @click="toDocument">{{ t('common.document') }}</div>
        </ElDropdownItem> -->
        <!-- <ElDropdownItem divided>
          <Icon icon="ep:lock" />
          <div @click="lockScreen">{{ t('lock.lockScreen') }}</div>
        </ElDropdownItem> -->
        <ElDropdownItem divided @click="loginOut">
          <Icon icon="ep:switch-button" />
          <div>{{ t('common.loginOut') }}</div>
        </ElDropdownItem>
      </ElDropdownMenu>
    </template>
  </ElDropdown>

  <!-- <LockDialog v-if="dialogVisible" v-model="dialogVisible" /> -->

  <!-- <teleport to="body">
    <transition name="fade-bottom" mode="out-in">
      <LockPage v-if="getIsLock" />
    </transition>
  </teleport> -->
</template>

<style scoped lang="scss">
.fade-bottom-enter-active,
.fade-bottom-leave-active {
  transition:
    opacity 0.25s,
    transform 0.3s;
}

.fade-bottom-enter-from {
  opacity: 0;
  transform: translateY(-10%);
}

.fade-bottom-leave-to {
  opacity: 0;
  transform: translateY(10%);
}
</style>
