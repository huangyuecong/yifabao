<script setup lang="ts">
import type { ChatModel, ChatModelConfiguration, ChatModelKind, ChatModelOption, ChatProviderSource } from '@/api/agent/chat'
import { Delete, Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onActivated, onMounted, ref } from 'vue'
import { aiModelsApi } from '@/api/agent/aiModels'
import { chatApi } from '@/api/agent/chat'
import { modelFormPayload, modelFormValues } from './aiModelForm'

const emit = defineEmits<{ changed: [] }>()
const canManage = ref(false)
const open = ref(false)
const busy = ref(false)
const error = ref('')
const configuration = ref<ChatModelConfiguration>()
const models = ref<ChatModel[]>([])
const availableModels = ref<ChatModelOption[]>([])
const fetchingSource = ref<ChatProviderSource>()
const modelListNote = ref('')
const enabledCount = computed(() => models.value.filter(item => item.enabled).length)
const modelGroups = computed(() => [
  { kind: 'chat' as const, label: '文本与多模态', description: '负责连续对话、文档创作，以及已开启的图片和视频阅读。', empty: '添加文本或多模态模型后，才能对话、理解资料和创作新文档。' },
  { kind: 'image' as const, label: '图片生成与修改', description: '需要图片时自动调用；开启参考图修改后，可根据已上传的图片继续创作。', empty: '尚未添加图片模型，AI 对话暂不能生成或修改图片。' },
  { kind: 'video' as const, label: '视频生成', description: '需要视频时自动调用，可使用文字或参考图片生成视频。', empty: '尚未添加视频模型，AI 对话暂不能生成视频。' },
].map(group => ({ ...group, rows: models.value.filter(model => model.kind === group.kind) })))
const sourceLabels: Record<ChatProviderSource, string> = { text: '文案服务', image: '图片服务', video: '视频服务' }

function sourceFor(kind: ChatModelKind): ChatProviderSource {
  return kind === 'chat' ? 'text' : kind
}

function normalizeModel(model: ChatModel): ChatModel {
  const kind = model.kind || 'chat'
  return { ...model, kind, provider_source: sourceFor(kind), supports_image_edit: model.supports_image_edit || false }
}

function optionsFor(row: ChatModel) {
  return availableModels.value.filter(item => (item.provider_source || 'text') === row.provider_source)
}

function errorMessage(reason: unknown) {
  return reason instanceof Error ? reason.message : '模型配置操作失败，请稍后重试'
}

async function checkAccess() {
  try {
    canManage.value = (await aiModelsApi.access()).can_manage
  }
  catch {
    canManage.value = false
  }
  if (!canManage.value) {
    open.value = false
    configuration.value = undefined
    models.value = []
  }
}

async function load() {
  busy.value = true
  error.value = ''
  try {
    configuration.value = await chatApi.adminModels()
    models.value = configuration.value.models.map(normalizeModel)
    availableModels.value = configuration.value.available_models
    modelListNote.value = ''
  }
  catch (reason) {
    error.value = errorMessage(reason)
  }
  finally {
    busy.value = false
  }
}

async function show() {
  await checkAccess()
  if (canManage.value) {
    open.value = true
    await load()
  }
}

function addModel(kind: ChatModelKind) {
  const source = sourceFor(kind)
  const option = availableModels.value.find(item => (item.provider_source || 'text') === source
    && !models.value.some(row => row.model === item.id && row.provider_source === source))
  models.value.push({
    id: `chat-${typeof crypto.randomUUID === 'function' ? crypto.randomUUID().slice(0, 8) : Math.random().toString(36).slice(2, 10)}`,
    name: option?.name || '',
    model: option?.id || '',
    kind,
    provider_source: source,
    supports_image_edit: false,
    enabled: true,
    supports_images: false,
    supports_video: false,
  })
}

function changeModel(row: ChatModel) {
  if (!row.name.trim()) {
    row.name = optionsFor(row).find(item => item.id === row.model)?.name || row.model
  }
}

function removeModel(row: ChatModel) {
  models.value = models.value.filter(model => model.id !== row.id)
}

function preferModel(row: ChatModel) {
  const first = models.value.findIndex(model => model.kind === row.kind)
  models.value = models.value.filter(model => model.id !== row.id)
  models.value.splice(first, 0, row)
}

function changeImages(row: ChatModel) {
  if (!row.supports_images) {
    row.supports_video = false
  }
}

function changeVideo(row: ChatModel) {
  if (row.supports_video) {
    row.supports_images = true
  }
}

async function getModels(kind: ChatModelKind) {
  if (busy.value || !configuration.value) {
    return
  }
  busy.value = true
  const source = sourceFor(kind)
  fetchingSource.value = source
  error.value = ''
  modelListNote.value = ''
  try {
    const document = await aiModelsApi.get()
    const provider = document.configurations.find(item => item.capability === source)
    if (!provider || provider.provider === 'mock') {
      throw new Error(`请先在模型配置中连接${sourceLabels[source]}`)
    }
    const result = await aiModelsApi.models(source, modelFormPayload(provider, provider.provider, modelFormValues(provider), document.revision))
    const options = [...availableModels.value, ...result.models.map(item => ({ ...item, provider_source: source }))]
    availableModels.value = [...new Map(options.map(item => [`${item.provider_source || 'text'}:${item.id}`, item])).values()]
    modelListNote.value = result.models.length ? `已获取 ${result.models.length} 个接口模型` : '接口未返回模型列表，可以填写已授权的模型 ID'
  }
  catch (reason) {
    error.value = errorMessage(reason)
  }
  finally {
    busy.value = false
    fetchingSource.value = undefined
  }
}

async function save() {
  if (busy.value || !configuration.value) {
    return
  }
  const missing = models.value.find(item => !item.name.trim() || !item.model.trim())
  if (missing) {
    error.value = '请为每个模型填写显示名称并选择模型 ID'
    return
  }
  const identifiers = models.value.map(item => `${item.provider_source}:${item.model.trim()}`)
  if (new Set(identifiers).size !== identifiers.length) {
    error.value = '同一个模型 ID 只需添加一次'
    return
  }
  busy.value = true
  error.value = ''
  try {
    configuration.value = await chatApi.saveModels({
      expected_revision: configuration.value.revision,
      models: models.value.map(({ id, name, model, kind, provider_source, supports_image_edit, enabled, supports_images, supports_video }) => ({
        id,
        name: name.trim(),
        model: model.trim(),
        kind,
        provider_source,
        supports_image_edit,
        enabled,
        supports_images,
        supports_video,
      })),
    })
    models.value = configuration.value.models.map(normalizeModel)
    ElMessage.success('对话模型配置已生效')
    emit('changed')
  }
  catch (reason) {
    error.value = errorMessage(reason)
  }
  finally {
    busy.value = false
  }
}

function close() {
  models.value = []
  configuration.value = undefined
  availableModels.value = []
  modelListNote.value = ''
  error.value = ''
}

onMounted(() => void checkAccess())
onActivated(() => void checkAccess())
</script>

<template>
  <ElButton v-if="canManage" @click="show">AI 对话模型</ElButton>
  <ElDrawer
    v-model="open"
    title="AI 对话模型管理"
    size="min(760px, 94vw)"
    destroy-on-close
    :close-on-click-modal="!busy"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    @closed="close"
  >
    <div class="chat-model-settings" :aria-busy="busy">
      <p class="settings-note">分别添加文本与多模态、图片、视频模型，每类可以同时启用多个。AI 会根据对话需求和输入资料自动选择适用模型，用户无需切换；未配置的能力将不可用。</p>
      <ElAlert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
        show-icon
      />
      <template v-if="configuration">
        <div class="model-section-heading">
          <div><h3>自动调用的模型</h3><p>已启用 {{ enabledCount }} 个，共 {{ models.length }} 个</p></div>
        </div>
        <p class="settings-note">各类模型沿用「模型配置」中的对应接口。同类模型按列表顺序优先使用满足输入能力的模型。</p>
        <p v-if="modelListNote" class="settings-note" role="status">{{ modelListNote }}</p>
        <ElForm label-position="top" :disabled="busy" @submit.prevent>
          <section
            v-for="group in modelGroups"
            :key="group.kind"
            class="model-group"
            :aria-label="group.label"
          >
            <div class="model-section-heading model-group-heading">
              <div><h3>{{ group.label }}</h3><p>已启用 {{ group.rows.filter(row => row.enabled).length }} 个，共 {{ group.rows.length }} 个</p></div>
              <div class="model-list-actions">
                <ElButton
                  :icon="Refresh"
                  :loading="fetchingSource === sourceFor(group.kind)"
                  :disabled="busy"
                  :aria-label="`获取${group.label}接口模型`"
                  @click="getModels(group.kind)"
                >
                  获取接口模型
                </ElButton>
                <ElButton
                  :icon="Plus"
                  :disabled="busy || models.length >= 32"
                  :aria-label="`添加${group.label}模型`"
                  @click="addModel(group.kind)"
                >
                  添加模型
                </ElButton>
              </div>
            </div>
            <p class="settings-note">{{ group.description }}</p>
            <p v-if="!group.rows.length" class="model-group-empty">{{ group.empty }}</p>
            <div v-for="(row, index) in group.rows" :key="row.id" class="chat-model-card">
              <div class="model-card-heading">
                <strong>{{ row.name || `${group.label}模型 ${index + 1}` }}</strong>
                <div class="model-card-actions">
                  <ElSwitch v-model="row.enabled" :aria-label="`启用 ${row.name || '此模型'}`" active-text="启用" />
                  <ElButton
                    text
                    :icon="Delete"
                    :disabled="busy"
                    :aria-label="`移除 ${row.name || '此模型'}`"
                    @click="removeModel(row)"
                  />
                </div>
              </div>
              <div class="model-service-row">
                <span class="model-service-label">{{ sourceLabels[row.provider_source] }}</span>
                <ElButton
                  v-if="index"
                  text
                  :disabled="busy"
                  :aria-label="`优先使用 ${row.name || '此模型'}`"
                  @click="preferModel(row)"
                >
                  设为优先
                </ElButton>
                <span v-else class="model-priority-label">优先使用</span>
              </div>
              <div class="model-fields">
                <ElFormItem label="显示名称" required><ElInput v-model="row.name" maxlength="100" placeholder="用于区分已配置的模型" /></ElFormItem>
                <ElFormItem label="模型 ID" required>
                  <ElSelect
                    v-model="row.model"
                    filterable
                    allow-create
                    default-first-option
                    placeholder="选择已有模型或填写已授权的模型 ID"
                    @change="changeModel(row)"
                  >
                    <ElOption
                      v-for="option in optionsFor(row)"
                      :key="`${option.provider_source || 'text'}:${option.id}`"
                      :value="option.id"
                      :label="option.name"
                    />
                  </ElSelect>
                </ElFormItem>
              </div>
              <div v-if="row.kind === 'chat'" class="model-capabilities">
                <ElCheckbox v-model="row.supports_images" @change="changeImages(row)">图片识别</ElCheckbox>
                <ElCheckbox v-model="row.supports_video" @change="changeVideo(row)">视频画面识别</ElCheckbox>
                <span>文本与文档阅读默认支持</span>
              </div>
              <div v-else-if="row.kind === 'image'" class="model-capabilities">
                <ElCheckbox v-model="row.supports_image_edit">支持参考图修改</ElCheckbox><span>请确认所选模型支持图片输入</span>
              </div>
              <p v-if="row.configured === false" class="disabled-note">{{ sourceLabels[row.provider_source] }}尚未连接，可先保存模型，再到「AI 服务状态 → 模型配置」连接接口。</p>
            </div>
          </section>
        </ElForm>
        <p class="settings-note">对话模型按实际能力开启图片、视频阅读；视频阅读采用关键帧，不含音轨。图片模型可开启参考图修改，视频模型用于生成视频。</p>
        <p v-if="!enabledCount" class="disabled-note">当前没有启用模型，保存后暂不能进行 AI 创作；已有文档仍可转换格式。</p>
      </template>
      <ElButton v-else :loading="busy" @click="load">重新加载配置</ElButton>
    </div>
    <template #footer>
      <div class="settings-actions">
        <ElButton :disabled="busy" @click="load">重新加载</ElButton><ElButton :disabled="busy" @click="open = false">关闭</ElButton><ElButton
          type="primary"
          :loading="busy"
          :disabled="!configuration"
          @click="save"
        >
          保存并生效
        </ElButton>
      </div>
    </template>
  </ElDrawer>
</template>

<style scoped>
.chat-model-settings {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.settings-note {
  margin: 0;
  color: var(--xi-semantic-color-text-secondary);
  font-size: 14px;
  line-height: 1.8;
}
.model-section-heading,
.model-card-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.model-section-heading h3 {
  margin: 0;
  font-size: 18px;
}
.model-section-heading p {
  margin: 8px 0 0;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
}
.model-list-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.model-list-actions .el-button {
  margin: 0;
  min-height: 44px;
}
.model-group {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.model-group + .model-group {
  margin-top: 28px;
  padding-top: 28px;
  border-top: var(--xi-component-content-border);
}
.model-group-heading h3 {
  font-size: 16px;
}
.model-group-empty {
  margin: 0;
  padding: 20px;
  color: var(--xi-semantic-color-text-tertiary);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
  line-height: 1.8;
}
.model-service-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.model-priority-label {
  color: var(--xi-semantic-color-text-brand);
  font-size: 12px;
}
.model-service-label {
  display: flex;
  align-items: center;
  min-height: 32px;
  color: var(--xi-semantic-color-text-secondary);
}
.chat-model-card {
  padding: 20px;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}
.model-card-heading {
  margin-bottom: 16px;
}
.model-card-heading strong {
  min-width: 0;
  font-size: 15px;
  overflow-wrap: anywhere;
}
.model-card-actions {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  gap: 16px;
}
.model-card-actions .el-button {
  min-width: 44px;
  height: 44px;
  padding: 8px;
}
.model-fields {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
}
.model-fields .el-select {
  width: 100%;
}
.model-capabilities {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 20px;
}
.model-capabilities .el-checkbox {
  margin: 0;
  min-height: 44px;
}
.model-capabilities > span {
  font-size: 12px;
  color: var(--xi-semantic-color-text-tertiary);
}
.disabled-note {
  margin: 0;
  color: var(--xi-semantic-color-feedback-warning-text);
  font-size: 13px;
  line-height: 1.8;
}
.settings-actions {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
.settings-actions .el-button {
  margin: 0;
  min-height: 44px;
}
@media (width <= 600px) {
  .model-section-heading {
    flex-wrap: wrap;
  }
  .model-fields {
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
  }
  .chat-model-card {
    padding: 16px;
  }
  .model-card-heading {
    align-items: flex-start;
  }
}
</style>
