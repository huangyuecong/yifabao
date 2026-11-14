<script setup lang="ts">
import type { TrialAccount, TrialOperation } from '@/api/agent/trial'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { trialApi, trialMe } from '@/api/agent/trial'
import { shortTime } from './catalogPresentation'
import { friendlyContentError } from './contentWorkflow'
import WbPagination from './WbPagination.vue'

/* Online trial: the owner creates an account for each tester; everyone can see what
 * their account did, and the owner sees every account's operations. */
defineOptions({ name: 'TrialAccountsView' })
const route = useRoute()
const router = useRouter()
const tab = computed(() => route.meta.trialTab === 'accounts' && trialMe.value.is_owner ? 'accounts' : 'operations')

const accounts = ref<TrialAccount[]>([])
const accountsLoading = ref(false)
const operations = ref<TrialOperation[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const who = ref('')
const day = ref('')
const opsLoading = ref(false)

const dialog = reactive({ open: false, editing: '', username: '', display_name: '', password: '', is_owner: false })
const saving = ref(false)

async function loadAccounts() {
  if (!trialMe.value.is_owner) {
    return
  }
  accountsLoading.value = true
  try {
    accounts.value = (await trialApi.accounts()).items
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '读取账号失败'))
  }
  finally {
    accountsLoading.value = false
  }
}

async function loadOperations() {
  opsLoading.value = true
  try {
    const result = await trialApi.operations({
      username: who.value || undefined,
      day: day.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    operations.value = result.items
    total.value = result.total
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '读取操作记录失败'))
  }
  finally {
    opsLoading.value = false
  }
}

function openCreate() {
  Object.assign(dialog, { open: true, editing: '', username: '', display_name: '', password: '', is_owner: false })
}

function openEdit(account: TrialAccount) {
  Object.assign(dialog, { open: true, editing: account.username, username: account.username, display_name: account.display_name, password: '', is_owner: account.is_owner })
}

async function save() {
  saving.value = true
  try {
    if (dialog.editing) {
      await trialApi.update(dialog.editing, {
        display_name: dialog.display_name,
        is_owner: dialog.is_owner,
        ...(dialog.password ? { password: dialog.password } : {}),
      })
      ElMessage.success(dialog.password ? '已保存，新密码立即生效' : '已保存')
    }
    else {
      await trialApi.create({
        username: dialog.username.trim(),
        display_name: dialog.display_name.trim(),
        password: dialog.password,
        is_owner: dialog.is_owner,
      })
      ElMessage.success('账号已创建，把账号和密码发给试用的人即可登录')
    }
    dialog.open = false
    await loadAccounts()
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '保存失败'))
  }
  finally {
    saving.value = false
  }
}

async function setEnabled(account: TrialAccount, enabled: boolean) {
  try {
    await trialApi.update(account.username, { enabled })
    await loadAccounts()
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '操作失败'))
  }
}

async function remove(account: TrialAccount) {
  try {
    await ElMessageBox.confirm(`删除账号「${account.display_name}（${account.username}）」？删除后无法再登录，已有的操作记录会保留。`, '删除账号', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  }
  catch {
    return
  }
  try {
    await trialApi.remove(account.username)
    await loadAccounts()
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '删除失败'))
  }
}

function showOperations(account: TrialAccount) {
  who.value = account.username
  page.value = 1
  router.push('/agent/content/operations')
}

function reload() {
  page.value = 1
  void loadOperations()
}

watch(tab, (value) => {
  if (value === 'operations') {
    void loadOperations()
  }
  else {
    void loadAccounts()
  }
})
onActivated(() => {
  if (tab.value === 'accounts') {
    void loadAccounts()
  }
  else {
    void loadOperations()
  }
})
</script>

<template>
  <div class="wb-page">
    <header class="wb-page-head">
      <div>
        <h1>{{ tab === 'accounts' ? '账号管理' : '操作记录' }}</h1>
        <p v-if="tab === 'accounts'">给每位试用的人建一个账号。大家共用同一个演示空间，每个账号做了什么都记在操作记录里。</p>
        <p v-else-if="trialMe.is_owner">所有试用账号提交、生成、发布、删除等操作，按时间倒序。</p>
        <p v-else>你的账号（{{ trialMe.display_name }}）做过的操作，按时间倒序。</p>
      </div>
      <div class="wb-page-actions">
        <template v-if="tab === 'accounts'">
          <ElButton type="primary" :icon="Plus" @click="openCreate">新建账号</ElButton>
        </template>
        <template v-else>
          <ElSelect
            v-if="trialMe.is_owner"
            v-model="who"
            placeholder="全部账号"
            clearable
            filterable
            class="filter"
            @change="reload"
            @visible-change="(open: boolean) => open && !accounts.length && loadAccounts()"
          >
            <ElOption
              v-for="account in accounts"
              :key="account.username"
              :value="account.username"
              :label="`${account.display_name}（${account.username}）`"
            />
          </ElSelect>
          <ElDatePicker
            v-model="day"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="全部日期"
            class="filter"
            @change="reload"
          />
          <ElButton :icon="Refresh" @click="loadOperations">刷新</ElButton>
        </template>
      </div>
    </header>

    <section v-if="tab === 'accounts'" class="wb-panel">
      <ElTable
        v-loading="accountsLoading"
        :data="accounts"
        row-key="username"
        empty-text="还没有账号"
      >
        <ElTableColumn label="账号" min-width="180">
          <template #default="{ row }">
            <div class="name-cell">
              <strong>{{ row.display_name }}</strong>
              <small class="wb-muted">{{ row.username }}</small>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="身份" width="110">
          <template #default="{ row }">
            <span class="wb-pill" :class="row.is_owner ? 'is-primary' : 'is-plain'">{{ row.is_owner ? '管理员' : '试用' }}</span>
            <ElTag
              v-if="row.username === trialMe.username"
              size="small"
              type="info"
              class="me-tag"
            >
              当前
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="90">
          <template #default="{ row }">
            <span class="wb-pill" :class="row.enabled ? 'is-success' : 'is-danger'">{{ row.enabled ? '可登录' : '已停用' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="最近使用" width="150">
          <template #default="{ row }">
            <span class="wb-muted wb-num">{{ shortTime(row.last_seen_at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="创建时间" width="150">
          <template #default="{ row }">
            <span class="wb-muted wb-num">{{ shortTime(row.created_at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn width="250" align="right">
          <template #default="{ row }">
            <ElButton link type="primary" @click="showOperations(row)">操作记录</ElButton>
            <ElButton link type="primary" @click="openEdit(row)">编辑</ElButton>
            <template v-if="row.username !== trialMe.username">
              <ElButton link :type="row.enabled ? 'warning' : 'success'" @click="setEnabled(row, !row.enabled)">{{ row.enabled ? '停用' : '启用' }}</ElButton>
              <ElButton link type="danger" @click="remove(row)">删除</ElButton>
            </template>
          </template>
        </ElTableColumn>
      </ElTable>
    </section>

    <section v-else class="wb-panel">
      <ElTable v-loading="opsLoading" :data="operations" empty-text="还没有操作记录">
        <ElTableColumn label="时间" width="150">
          <template #default="{ row }">
            <span class="wb-num">{{ shortTime(row.at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="账号" width="170">
          <template #default="{ row }">
            <div class="name-cell">
              <strong>{{ row.display_name }}</strong>
              <small class="wb-muted">{{ row.username }}</small>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" min-width="180">
          <template #default="{ row }">
            {{ row.action }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="结果" width="90">
          <template #default="{ row }">
            <span class="wb-pill" :class="row.status < 400 ? 'is-success' : 'is-danger'">{{ row.status < 400 ? '成功' : '失败' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="接口" min-width="220">
          <template #default="{ row }">
            <span class="wb-muted wb-ellipsis path">{{ row.method }} {{ row.path }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn v-if="trialMe.is_owner" label="IP" width="130">
          <template #default="{ row }">
            <span class="wb-muted wb-num">{{ row.ip }}</span>
          </template>
        </ElTableColumn>
      </ElTable>
      <WbPagination
        v-model:page="page"
        v-model:page-size="pageSize"
        :total="total"
        @change="loadOperations"
      />
    </section>

    <ElDialog
      v-model="dialog.open"
      :title="dialog.editing ? '编辑账号' : '新建账号'"
      width="440px"
      append-to-body
    >
      <ElForm label-position="top" @submit.prevent>
        <ElFormItem label="登录账号" required>
          <ElInput
            v-model="dialog.username"
            :disabled="!!dialog.editing"
            maxlength="32"
            placeholder="字母、数字、点、下划线或短横线"
          />
        </ElFormItem>
        <ElFormItem label="姓名或备注">
          <ElInput v-model="dialog.display_name" maxlength="40" placeholder="例如：张老师" />
        </ElFormItem>
        <ElFormItem :label="dialog.editing ? '新密码（不改就留空）' : '密码'" :required="!dialog.editing">
          <ElInput
            v-model="dialog.password"
            type="password"
            show-password
            maxlength="128"
            placeholder="至少 6 位"
            autocomplete="new-password"
          />
        </ElFormItem>
        <ElCheckbox v-model="dialog.is_owner" :disabled="dialog.editing === trialMe.username">管理员（可以管理账号、查看所有人的操作记录，不受每日视频次数限制）</ElCheckbox>
      </ElForm>
      <template #footer>
        <ElButton @click="dialog.open = false">取消</ElButton>
        <ElButton
          type="primary"
          :loading="saving"
          :disabled="!dialog.username.trim() || (!dialog.editing && dialog.password.length < 6)"
          @click="save"
        >
          保存
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.filter {
  width: 180px;
}
.name-cell {
  display: grid;
  gap: 2px;
}
.name-cell strong {
  font-weight: 500;
}
.name-cell small {
  font-size: 12px;
}
.me-tag {
  margin-left: 6px;
}
.path {
  display: block;
  font-size: 12px;
}
</style>
