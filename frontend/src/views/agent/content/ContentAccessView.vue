<script setup lang="ts">
import type { ContentAccessUser } from '@/api/agent/access'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { contentAccessApi } from '@/api/agent/access'
import { accessErrorStatus, useLocalAccess } from './localAccess'

defineOptions({ name: 'ContentAccessView' })
const { access, canManageUsers, message, refresh } = useLocalAccess()
const users = ref<ContentAccessUser[]>([])
const keyword = ref('')
const page = ref(1)
const pageSize = 20
const total = ref(0)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const formError = ref('')
const editing = ref<ContentAccessUser>()
const drawerOpen = ref(false)
const isAdmin = ref(false)
const permissions = ref<string[]>([])
const conflict = ref(false)
let requestGeneration = 0
let loadSequence = 0
let active = true

const permissionOptions = [
  { value: 'models.manage', label: '模型配置', description: '修改所有用户使用的模型接口、密钥与模型选择，恢复上一版配置。' },
  { value: 'library.manage', label: '资料管理', description: '管理其他人员的企业资料与素材；普通用户仍可维护自己的资料。' },
  { value: 'rules.manage', label: '检测规则管理', description: '新增和删除关键词、敏感词与禁用词检测规则。' },
  { value: 'audit.read', label: '查看操作记录', description: '查看本项目的操作审计记录。' },
]
function permissionLabel(value: string) {
  return permissionOptions.find(item => item.value === value)?.label || value
}
function clearProtectedData() {
  requestGeneration++
  users.value = []
  total.value = 0
  drawerOpen.value = false
  editing.value = undefined
  permissions.value = []
}
watch(() => [access.value?.subject, canManageUsers.value], ([subject, allowed], previous) => {
  if (!allowed || (previous?.[0] && subject !== previous[0])) {
    clearProtectedData()
  }
})
function reasonMessage(reason: unknown) {
  return reason instanceof Error ? reason.message : '操作失败，请稍后重试'
}
async function load() {
  const sequence = ++loadSequence
  let generation = requestGeneration
  loading.value = true
  error.value = ''
  try {
    await refresh()
    if (sequence !== loadSequence) {
      return
    }
    if (!canManageUsers.value) {
      clearProtectedData()
      return
    }
    // Refresh may invalidate an earlier account's data; fetch for the current identity.
    generation = requestGeneration
    const subject = access.value?.subject
    const result = await contentAccessApi.users({ keyword: keyword.value.trim(), page: page.value, page_size: pageSize })
    if (sequence !== loadSequence || generation !== requestGeneration || !canManageUsers.value || subject !== access.value?.subject) {
      return
    }
    users.value = result.items
    total.value = result.total
    page.value = result.page
  }
  catch (reason) {
    if (sequence !== loadSequence || generation !== requestGeneration) {
      return
    }
    error.value = reasonMessage(reason)
    users.value = []
    total.value = 0
    if ([401, 403].includes(accessErrorStatus(reason) || 0)) {
      clearProtectedData()
      await refresh()
    }
  }
  finally {
    if (sequence === loadSequence) {
      loading.value = false
    }
  }
}
function search() {
  page.value = 1
  void load()
}
function edit(user: ContentAccessUser) {
  if (!canManageUsers.value) {
    return
  }
  editing.value = { ...user, permissions: [...user.permissions] }
  isAdmin.value = user.is_project_admin
  permissions.value = [...user.permissions]
  conflict.value = false
  formError.value = ''
  drawerOpen.value = true
}
async function reloadConflict() {
  const userId = editing.value?.id
  await load()
  const current = users.value.find(user => user.id === userId)
  if (current) {
    edit(current)
  }
  else if (canManageUsers.value) {
    formError.value = '未找到该人员，请关闭后重新搜索'
  }
}
async function save() {
  if (!editing.value || saving.value || !canManageUsers.value || conflict.value) {
    return
  }
  saving.value = true
  formError.value = ''
  const user = editing.value
  const payload = { expected_revision: user.revision, is_project_admin: isAdmin.value, permissions: [...permissions.value] }
  try {
    if (payload.is_project_admin !== user.is_project_admin) {
      const action = payload.is_project_admin
        ? '设为项目管理员？该人员将可以分配人员权限，并管理全部四项业务功能。'
        : '取消项目管理员？该人员将只保留下方勾选的业务权限，无法再分配人员权限。'
      await ElMessageBox.confirm(`将 ${user.display_name || user.subject}（${user.subject}）${action}`, '确认管理员变更', { type: 'warning', confirmButtonText: '确认变更', cancelButtonText: '取消' })
    }
    await contentAccessApi.update(user.id, payload)
    drawerOpen.value = false
    editing.value = undefined
    ElMessage.success('权限已保存，后续请求按新权限执行')
    await load()
  }
  catch (reason) {
    if (reason === 'cancel' || reason === 'close') {
      return
    }
    formError.value = reasonMessage(reason)
    if (accessErrorStatus(reason) === 409) {
      conflict.value = true
    }
    if ([401, 403].includes(accessErrorStatus(reason) || 0)) {
      error.value = formError.value
      clearProtectedData()
      await refresh()
    }
  }
  finally {
    saving.value = false
  }
}
const focus = () => {
  if (active && !saving.value) {
    void load()
  }
}
onMounted(() => {
  void load()
  if (typeof window !== 'undefined') {
    window.addEventListener('focus', focus)
  }
})
onActivated(() => {
  active = true
  if (!loading.value) {
    void load()
  }
})
onDeactivated(() => {
  active = false
})
onBeforeUnmount(() => {
  requestGeneration++
  loadSequence++
  if (typeof window !== 'undefined') {
    window.removeEventListener('focus', focus)
  }
})
</script>

<template>
  <div class="access-view" :aria-busy="loading">
    <section class="access-heading">
      <div><h2>人员与权限</h2><p>管理本智能体内的功能权限；访问资格由统一身份验证服务校验。</p></div>
      <ElButton :loading="loading" @click="load">刷新权限</ElButton>
    </section>
    <ElAlert
      v-if="error"
      type="error"
      :title="error"
      :closable="false"
      show-icon
    />
    <ElAlert
      v-if="!canManageUsers"
      type="info"
      :title="message || '仅项目管理员可以查看和分配人员权限'"
      :closable="false"
      show-icon
    />
    <template v-else>
      <p class="access-note">这里只显示已经登录过本智能体的人员。首次进入默认为普通用户；请让待授权人员先登录一次，再按姓名或账号标识搜索。</p>
      <form class="access-search" @submit.prevent="search">
        <ElInput
          v-model="keyword"
          clearable
          placeholder="搜索姓名、账号标识或部门"
          aria-label="搜索人员"
          maxlength="120"
        />
        <ElButton native-type="submit" type="primary" :loading="loading">搜索</ElButton>
      </form>
      <div class="access-table">
        <ElTable :data="users" row-key="id" empty-text="暂无匹配人员，请先让人员登录本智能体">
          <ElTableColumn label="人员" min-width="240">
            <template #default="{ row }"><strong>{{ row.display_name || row.subject }}</strong><span class="account-id">{{ row.subject }}</span><ElTag v-if="row.subject === access?.subject" size="small" type="info">当前账号</ElTag></template>
          </ElTableColumn>
          <ElTableColumn prop="department" label="部门" min-width="140" />
          <ElTableColumn label="权限" min-width="260">
            <template #default="{ row }"><div class="permission-tags"><ElTag v-if="row.is_project_admin">项目管理员</ElTag><template v-else><ElTag v-for="permission in row.permissions" :key="permission" type="info">{{ permissionLabel(permission) }}</ElTag><span v-if="!row.permissions.length">普通用户</span></template></div></template>
          </ElTableColumn>
          <ElTableColumn label="最近访问" min-width="170"><template #default="{ row }">{{ row.last_seen_at ? new Date(row.last_seen_at).toLocaleString('zh-CN') : '—' }}</template></ElTableColumn>
          <ElTableColumn label="操作" width="110" fixed="right"><template #default="{ row }"><ElButton link type="primary" @click="edit(row)">设置权限</ElButton></template></ElTableColumn>
        </ElTable>
      </div>
      <ElPagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </template>
    <ElDrawer
      v-model="drawerOpen"
      title="设置人员权限"
      size="min(560px, 94vw)"
      destroy-on-close
      :close-on-click-modal="!saving"
      :close-on-press-escape="!saving"
      :show-close="!saving"
    >
      <div v-if="editing" class="access-editor">
        <div><h3>{{ editing.display_name || editing.subject }}</h3><p class="account-id">账号标识：{{ editing.subject }}</p></div>
        <ElAlert
          v-if="formError"
          :title="formError"
          type="error"
          :closable="false"
          show-icon
        />
        <ElForm label-position="top" :disabled="saving" @submit.prevent>
          <ElFormItem label="项目管理员"><ElSwitch v-model="isAdmin" aria-label="项目管理员" /><p class="access-note">拥有人员授权和全部业务管理权限。系统至少保留一位项目管理员。</p></ElFormItem>
          <ElFormItem label="业务权限">
            <p v-if="isAdmin" class="access-note">项目管理员已拥有以下全部权限；取消管理员后使用下方保留的勾选项。</p>
            <ElCheckboxGroup v-model="permissions" :disabled="isAdmin" class="permission-options">
              <div v-for="option in permissionOptions" :key="option.value" class="permission-option"><ElCheckbox :value="option.value">{{ option.label }}</ElCheckbox><p>{{ option.description }}</p></div>
            </ElCheckboxGroup>
          </ElFormItem>
        </ElForm>
      </div>
      <template #footer>
        <div class="access-footer">
          <ElButton :disabled="saving" @click="drawerOpen = false">取消</ElButton><ElButton v-if="conflict" :loading="loading" @click="reloadConflict">重新加载该人员</ElButton><ElButton
            type="primary"
            :loading="saving"
            :disabled="conflict || !canManageUsers"
            @click="save"
          >
            保存权限
          </ElButton>
        </div>
      </template>
    </ElDrawer>
  </div>
</template>

<style scoped>
.access-view,
.access-editor {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-md);
}
.access-heading,
.access-search,
.access-footer,
.permission-tags {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-sm);
}
.access-heading {
  justify-content: space-between;
}
h2,
h3,
p {
  margin: 0;
}
h2 {
  font-size: var(--xi-semantic-typography-heading2-font-size);
}
.access-heading p,
.access-note,
.permission-option p,
.account-id {
  color: var(--xi-semantic-color-text-secondary);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.access-heading p {
  margin-top: var(--xi-component-content-gap-xs);
}
.access-search :deep(.el-input) {
  width: min(420px, 100%);
}
.access-table {
  min-width: 0;
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}
.account-id {
  display: block;
  font-size: var(--xi-semantic-typography-caption-font-size);
}
.permission-options {
  display: grid;
  width: 100%;
  gap: var(--xi-component-content-gap-sm);
}
.permission-option {
  padding: var(--xi-component-content-gap-sm);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
}
.permission-option p {
  font-size: var(--xi-semantic-typography-caption-font-size);
}
.access-footer {
  justify-content: flex-end;
}
.access-view :deep(.el-pagination) {
  max-width: 100%;
  flex-wrap: wrap;
  row-gap: var(--xi-component-content-gap-xs);
}
</style>
