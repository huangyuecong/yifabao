<script setup lang="ts">
import type { ModelConfiguration, ModelConfigurationDocument, ModelOption } from '@/api/agent/aiModels'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref } from 'vue'
import { aiModelsApi } from '@/api/agent/aiModels'
import { canKeepModelSecret, modelFieldKey, modelFormPayload, modelFormValues } from './aiModelForm'

const emit = defineEmits<{ changed: [] }>()
const canManage = ref(false)
const accessMessage = ref('')
const checkingAccess = ref(false)
const open = ref(false)
const busy = ref('')
const error = ref('')
const document = ref<ModelConfigurationDocument>()
const capability = ref('text')
const provider = ref('')
const values = ref<Record<string, string>>({})
const models = ref<ModelOption[]>([])
const loadedFrom = ref('')
const selected = computed(() => document.value?.configurations.find(item => item.capability === capability.value))
const adapter = computed(() => selected.value?.adapters.find(item => item.value === provider.value))
const modelKey = computed(() => modelFieldKey(capability.value))
const advancedFields = computed(() => adapter.value?.fields.filter(field => ['list', 'integers'].includes(field.kind)) ?? [])
const mainFields = computed(() => adapter.value?.fields.filter(field => !['list', 'integers'].includes(field.kind)) ?? [])
const advancedOpen = ref<string[]>([])
let generation = 0

function splitModels(value?: string) {
  return (value ?? '').split(/[,，]/).map(item => item.trim()).filter(Boolean)
}

function resetForm(configuration?: ModelConfiguration) {
  if (!configuration) {
    return
  }
  provider.value = configuration.provider
  values.value = modelFormValues(configuration)
  models.value = []
  loadedFrom.value = ''
  error.value = ''
}

function selectCapability(value: string | number | boolean | undefined) {
  capability.value = String(value)
  resetForm(selected.value)
}

function changeProvider() {
  values.value = provider.value === selected.value?.provider
    ? modelFormValues(selected.value)
    : modelFormValues({ values: adapter.value?.defaults ?? {}, adapters: adapter.value ? [adapter.value] : [] })
  models.value = []
  loadedFrom.value = ''
  advancedOpen.value = ['parameters']
}

function clearModelList() {
  models.value = []
  loadedFrom.value = ''
}

function message(reason: unknown) {
  return reason instanceof Error ? reason.message : '操作失败，请稍后重试'
}

async function handleFailure(reason: unknown) {
  error.value = message(reason)
  if (reason && typeof reason === 'object' && 'status' in reason && [401, 403].includes(Number(reason.status))) {
    canManage.value = false
    accessMessage.value = message(reason)
    open.value = false
    close()
  }
}

async function load() {
  const current = generation
  busy.value = 'load'
  error.value = ''
  try {
    const result = await aiModelsApi.get()
    if (current !== generation) {
      return
    }
    document.value = result
    if (!selected.value) {
      capability.value = result.configurations[0]?.capability || 'text'
    }
    resetForm(selected.value)
  }
  catch (reason) {
    if (current === generation) {
      await handleFailure(reason)
    }
  }
  finally {
    if (current === generation) {
      busy.value = ''
    }
  }
}

async function show(requestedCapability?: string) {
  const current = generation
  await checkAccess()
  if (!active || current !== generation || !canManage.value) {
    return
  }
  if (requestedCapability) {
    capability.value = requestedCapability
  }
  open.value = true
  await load()
}

async function getModels() {
  if (!selected.value || !document.value || busy.value) {
    return
  }
  busy.value = 'models'
  const current = generation
  error.value = ''
  try {
    const result = await aiModelsApi.models(capability.value, modelFormPayload(selected.value, provider.value, values.value, document.value.revision))
    if (current !== generation) {
      return
    }
    models.value = result.models
    loadedFrom.value = result.message
    if (!models.value.length) {
      error.value = '接口没有返回可用模型，请确认当前密钥的模型授权'
    }
  }
  catch (reason) {
    if (current !== generation) {
      return
    }
    models.value = []
    await handleFailure(reason)
  }
  finally {
    if (current === generation) {
      busy.value = ''
    }
  }
}

async function save() {
  if (!selected.value || !document.value || !adapter.value || busy.value) {
    return
  }
  const missing = adapter.value.fields.find(field => field.required && !values.value[field.key]?.trim()
    && !(field.kind === 'secret' && canKeepModelSecret(selected.value, provider.value, values.value, field.key)))
  if (missing) {
    advancedOpen.value = ['parameters']
    error.value = `请填写${missing.label}`
    return
  }
  busy.value = 'save'
  const current = generation
  error.value = ''
  try {
    const result = await aiModelsApi.save(capability.value, modelFormPayload(selected.value, provider.value, values.value, document.value.revision))
    if (current !== generation) {
      return
    }
    document.value = result
    resetForm(selected.value)
    ElMessage.success('模型配置已保存，后续生成将使用新配置')
    emit('changed')
  }
  catch (reason) {
    if (current === generation) {
      await handleFailure(reason)
    }
  }
  finally {
    if (current === generation) {
      busy.value = ''
    }
  }
}

async function rollback() {
  if (!document.value || busy.value) {
    return
  }
  const current = generation
  try {
    await ElMessageBox.confirm(`恢复${selected.value?.label}的上一版模型配置？后续生成将使用恢复后的配置。`, '恢复上一版', { type: 'warning' })
  }
  catch {
    return
  }
  if (current !== generation) {
    return
  }
  busy.value = 'rollback'
  error.value = ''
  try {
    const result = await aiModelsApi.rollback(capability.value, document.value.revision)
    if (current !== generation) {
      return
    }
    document.value = result
    resetForm(selected.value)
    ElMessage.success('已恢复上一版模型配置')
    emit('changed')
  }
  catch (reason) {
    if (current === generation) {
      await handleFailure(reason)
    }
  }
  finally {
    if (current === generation) {
      busy.value = ''
    }
  }
}

function close() {
  generation++
  busy.value = ''
  values.value = {}
  models.value = []
  document.value = undefined
  error.value = ''
}

async function checkAccess() {
  if (checkingAccess.value) {
    return
  }
  checkingAccess.value = true
  const current = generation
  try {
    const result = await aiModelsApi.access()
    if (current !== generation || !active) {
      return
    }
    canManage.value = result.can_manage
    accessMessage.value = result.message || ''
  }
  catch (reason) {
    if (current !== generation || !active) {
      return
    }
    canManage.value = false
    accessMessage.value = message(reason)
  }
  finally {
    if (current === generation && active) {
      checkingAccess.value = false
      if (!canManage.value) {
        open.value = false
        close()
      }
    }
  }
}
let active = true
const refreshOnFocus = () => {
  if (active) {
    void checkAccess()
  }
}
onMounted(() => {
  void checkAccess()
  if (typeof window !== 'undefined') {
    window.addEventListener('focus', refreshOnFocus)
  }
})
onActivated(() => {
  active = true
  void checkAccess()
})
onDeactivated(() => {
  active = false
  open.value = false
  close()
  checkingAccess.value = false
})
onBeforeUnmount(() => {
  active = false
  close()
  if (typeof window !== 'undefined') {
    window.removeEventListener('focus', refreshOnFocus)
  }
})
defineExpose({ show, canManage })
</script>

<template>
  <ElButton v-if="canManage" type="primary" @click="show()">修改模型</ElButton>
  <div v-else-if="accessMessage" class="model-access-note" role="status">
    <span>{{ accessMessage }}</span>
    <ElButton text :loading="checkingAccess" @click="checkAccess">重新检查权限</ElButton>
  </div>
  <ElDrawer
    v-model="open"
    title="AI 模型配置"
    size="min(680px, 94vw)"
    destroy-on-close
    :close-on-click-modal="!busy"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    @closed="close"
  >
    <div class="model-settings">
      <p class="model-settings-note">选择创作功能，修改接口地址和模型 ID，保存后用于后续生成。已有密钥不会回显；留空保留，更换接口地址时重新填写。</p>
      <ElAlert
        v-if="error"
        type="error"
        :title="error"
        :closable="false"
        show-icon
      />
      <ElForm
        v-if="document"
        label-position="top"
        :disabled="Boolean(busy)"
        @submit.prevent
      >
        <ElFormItem label="创作功能">
          <ElSelect :model-value="capability" @update:model-value="selectCapability">
            <ElOption
              v-for="item in document.configurations"
              :key="item.capability"
              :label="item.label"
              :value="item.capability"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="模型接口" required>
          <ElSelect v-model="provider" @change="changeProvider">
            <ElOption
              v-if="selected?.provider === 'mock'"
              label="本地模拟（当前配置）"
              value="mock"
              disabled
            />
            <ElOption
              v-for="item in selected?.adapters"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem
          v-for="field in mainFields"
          :key="field.key"
          :label="field.label"
          :required="field.required"
        >
          <div v-if="field.key === modelKey" class="model-selection">
            <ElSelect
              v-model="values[field.key]"
              filterable
              allow-create
              default-first-option
              placeholder="获取列表后选择，或填写模型 ID"
            >
              <ElOption
                v-for="model in models"
                :key="model.id"
                :value="model.id"
                :label="model.name === model.id ? model.id : `${model.name}（${model.id}）`"
              />
            </ElSelect>
            <ElButton :loading="busy === 'models'" @click="getModels">获取模型列表</ElButton>
            <span v-if="loadedFrom" class="model-list-note">{{ loadedFrom }}</span>
          </div>
          <ElSelect
            v-else-if="field.kind === 'models'"
            :model-value="splitModels(values[field.key])"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="可多选；点上方「获取模型列表」后选择，或直接输入模型 ID"
            @update:model-value="values[field.key] = ($event as string[]).join(', ')"
          >
            <ElOption
              v-for="model in models.filter(item => item.id !== values[modelKey])"
              :key="model.id"
              :value="model.id"
              :label="model.name === model.id ? model.id : `${model.name}（${model.id}）`"
            />
          </ElSelect>
          <ElInput
            v-else
            v-model="values[field.key]"
            :type="field.kind === 'secret' ? 'password' : 'text'"
            :show-password="field.kind === 'secret'"
            autocomplete="off"
            :placeholder="field.kind === 'secret' && canKeepModelSecret(selected, provider, values, field.key) ? '•••••••• · 已配置，留空保留' : `请输入${field.label}`"
            :maxlength="field.kind === 'secret' ? 8192 : 2048"
            @input="clearModelList"
          />
          <p v-if="field.kind === 'secret'" class="model-settings-note secret-note">{{ canKeepModelSecret(selected, provider, values, field.key) ? '密钥已保存。仅在更换密钥时输入新值。' : '填写当前接口的密钥，保存后仅显示配置状态。' }}</p>
        </ElFormItem>
        <ElCollapse v-if="advancedFields.length" v-model="advancedOpen">
          <ElCollapseItem title="模型能力参数" name="parameters">
            <p class="model-settings-note">请按所选模型实际支持的范围填写。图片参数留空时使用接口的默认范围。</p>
            <ElFormItem
              v-for="field in advancedFields"
              :key="field.key"
              :label="field.label"
              :required="field.required"
            >
              <ElInput v-model="values[field.key]" :placeholder="`请输入${field.label}`" />
            </ElFormItem>
          </ElCollapseItem>
        </ElCollapse>
      </ElForm>
      <ElButton v-else :loading="busy === 'load'" @click="load">重新加载配置</ElButton>
      <ElCollapse v-if="document?.history.length" class="model-history">
        <ElCollapseItem title="最近修改记录" name="history">
          <ul>
            <li v-for="item in document.history" :key="item.revision">
              {{ item.name }} · {{ document.configurations.find(config => config.capability === item.capability)?.label }} · {{ item.action === 'rollback' ? '恢复上一版' : '保存配置' }} · {{ new Date(item.at).toLocaleString() }}
            </li>
          </ul>
        </ElCollapseItem>
      </ElCollapse>
    </div>
    <template #footer>
      <div class="model-settings-actions">
        <ElButton :disabled="Boolean(busy)" @click="load">重新加载</ElButton>
        <ElButton :disabled="Boolean(busy) || !selected?.can_rollback" :loading="busy === 'rollback'" @click="rollback">恢复上一版</ElButton>
        <ElButton :disabled="Boolean(busy)" @click="open = false">关闭</ElButton>
        <ElButton
          type="primary"
          :disabled="Boolean(busy) || !adapter"
          :loading="busy === 'save'"
          @click="save"
        >
          保存并生效
        </ElButton>
      </div>
    </template>
  </ElDrawer>
</template>

<style scoped>
.model-access-note {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--xi-component-content-gap-xs);
  max-width: 360px;
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}
.model-settings {
  display: grid;
  gap: var(--xi-component-content-gap-md);
}
.model-settings-note,
.model-list-note {
  margin: 0;
  color: var(--xi-semantic-color-text-secondary);
  line-height: 1.6;
  font-size: var(--xi-semantic-typography-caption-font-size);
}
.secret-note {
  margin-top: 6px;
}
.model-selection {
  display: flex;
  flex-wrap: wrap;
  width: 100%;
  gap: var(--xi-component-content-gap-sm);
}
.model-selection .el-select {
  flex: 1;
  min-width: 180px;
}
.model-list-note {
  flex-basis: 100%;
}
.model-settings-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--xi-component-content-gap-sm);
}
.model-settings-actions .el-button {
  margin-left: 0;
}
.model-history ul {
  padding-left: 20px;
}
.model-history li {
  margin-bottom: var(--xi-component-content-gap-xs);
}
</style>
