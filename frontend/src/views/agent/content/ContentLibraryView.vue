<script setup lang="ts">
import type {
  LibraryCategory,
  LibraryDataLevel,
  LibraryItem,
  LibraryResourceKind,
  SensitiveRule,
} from '@/api/agent/content'
import { UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { contentApi } from '@/api/agent/content'
import ContentLibraryCatalog from './ContentLibraryCatalog.vue'
import ContentLibraryFilePreview from './ContentLibraryFilePreview.vue'
import { categoryLabel, isProductResource } from './contentResourceCatalog'
import { friendlyContentError } from './contentWorkflow'
import { inspectLibraryFile, libraryFileAccept } from './libraryUpload'
import { useLocalAccess } from './localAccess'

defineOptions({ name: 'ContentLibraryView' })

const activeTab = ref('resources')
const loading = ref(false)
const busy = ref('')
const resources = ref<LibraryItem[]>([])
const categories = ref<LibraryCategory[]>([])
const rules = ref<SensitiveRule[]>([])
const selectedFile = ref<File>()
const versionFile = ref<File>()
const versionKindBeforeReplacement = ref<LibraryResourceKind>('document')
const versionDialogOpen = ref(false)
const createDrawerOpen = ref(false)
const editingResource = ref<LibraryItem>()
const loadError = ref('')
const { access, hasPermission } = useLocalAccess()
const canManageRules = computed(() => hasPermission('rules.manage'))
function canEditResource(item: LibraryItem) {
  return hasPermission('library.manage') || Boolean(access.value?.subject && item.owner_id === access.value.subject)
}

const resourceForm = reactive({
  resource_kind: 'document' as LibraryResourceKind,
  category: '',
  name: '',
  text_content: '',
  data_level: 'internal' as LibraryDataLevel,
  notes: '',
})
const versionForm = reactive({
  resource_kind: 'document' as LibraryResourceKind,
  category: '',
  name: '',
  text_content: '',
  data_level: 'internal' as LibraryDataLevel,
  tags: [] as string[],
  parent_id: '',
  source_url: '',
  crawled_at: '',
  freshness: '',
  notes: '',
})
const ruleForm = reactive({
  phrase: '',
  rule_type: 'sensitive' as 'keyword' | 'sensitive' | 'forbidden',
  replacement: '',
})

const resourceKindOptions: Array<{ value: LibraryResourceKind, label: string }> = [
  { value: 'document', label: '文档' },
  { value: 'product', label: '产品资料' },
  { value: 'logo', label: 'Logo' },
  { value: 'image', label: '图片' },
  { value: 'video', label: '视频' },
  { value: 'audio', label: '音频' },
  { value: 'spec', label: '内容规范' },
]

const dataLevelOptions: Array<{ value: LibraryDataLevel, label: string }> = [
  { value: 'public', label: '公开' },
  { value: 'internal', label: '内部' },
  { value: 'restricted', label: '受限' },
]

function categoryOptions(kind: LibraryResourceKind) {
  const options = new Map<string, { id: string, label: string }>()
  function add(id: string, name = id) {
    const label = categoryLabel(name)
    if (id.trim() && !options.has(label)) {
      options.set(label, { id, label })
    }
  }
  for (const item of categories.value) {
    if (item.resource_kinds?.includes(kind)) {
      add(item.id, item.name)
    }
  }
  for (const item of resources.value) {
    if (item.active && item.resource_kind === kind) {
      add(item.category)
    }
  }
  return [...options.values()].sort((a, b) => a.label.localeCompare(b.label, 'zh-CN'))
}

function categoryValue(value: string, kind: LibraryResourceKind, original = '') {
  const label = value.trim()
  if (original && label === categoryLabel(original)) {
    return original
  }
  return categoryOptions(kind).find(item => item.label === label)?.id || label
}

function suggestCategories(kind: LibraryResourceKind, query: string, callback: (items: Array<{ value: string }>) => void) {
  const search = query.trim().toLocaleLowerCase('zh-CN')
  callback(categoryOptions(kind)
    .filter(item => item.label.toLocaleLowerCase('zh-CN').includes(search))
    .map(item => ({ value: item.label })))
}

function suggestResourceCategories(query: string, callback: (items: Array<{ value: string }>) => void) {
  suggestCategories(resourceForm.resource_kind, query, callback)
}

function suggestVersionCategories(query: string, callback: (items: Array<{ value: string }>) => void) {
  suggestCategories(versionForm.resource_kind, query, callback)
}

watch(() => resourceForm.resource_kind, () => {
  resourceForm.category = ''
}, { flush: 'sync' })
watch(() => versionForm.resource_kind, () => {
  versionForm.category = ''
}, { flush: 'sync' })

function openResourceCreator() {
  selectedFile.value = undefined
  Object.assign(resourceForm, { category: '', name: '', text_content: '', notes: '' })
  createDrawerOpen.value = true
}

function messageOf(error: unknown) {
  return friendlyContentError(error)
}

async function load() {
  loading.value = true
  loadError.value = ''
  const result = await Promise.allSettled([
    contentApi.listLibrary(),
    contentApi.listLibraryCategories(),
    contentApi.listRules(),
  ])
  if (result[0].status === 'fulfilled') {
    resources.value = result[0].value
  }
  else {
    loadError.value = messageOf(result[0].reason)
  }
  if (result[1].status === 'fulfilled') {
    categories.value = result[1].value
  }
  else if (!loadError.value) {
    loadError.value = messageOf(result[1].reason)
  }
  if (result[2].status === 'fulfilled') {
    rules.value = result[2].value
  }
  else if (!loadError.value) {
    loadError.value = messageOf(result[2].reason)
  }
  loading.value = false
}

function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  selectResourceFile(file)
}

function selectResourceFile(file?: File) {
  if (!file || busy.value) {
    return
  }
  const result = inspectLibraryFile(file, resourceForm.resource_kind)
  if (result.error) {
    ElMessage.warning(result.error)
    return
  }
  if (!resourceForm.name.trim() || resourceForm.name === selectedFile.value?.name.replace(/\.[^.]+$/, '')) {
    resourceForm.name = file.name.replace(/\.[^.]+$/, '')
  }
  selectedFile.value = file
  resourceForm.resource_kind = result.kind
}

function changeResourceKind() {
  if (selectedFile.value) {
    const result = inspectLibraryFile(selectedFile.value, resourceForm.resource_kind)
    if (result.kind !== resourceForm.resource_kind) {
      selectedFile.value = undefined
    }
  }
}

function cancelVersionFile() {
  if (busy.value) {
    return
  }
  versionFile.value = undefined
  versionForm.resource_kind = versionKindBeforeReplacement.value
}

async function finishResourceSave(item: LibraryItem) {
  // The write already succeeded. Show it immediately, even if refreshing categories fails.
  resources.value = [item, ...resources.value.filter(resource => resource.id !== item.id)]
  createDrawerOpen.value = false
  selectedFile.value = undefined
  Object.assign(resourceForm, {
    category: '',
    name: '',
    text_content: '',
    notes: '',
  })
  ElMessage.success('资料已保存')
  const result = await Promise.allSettled([contentApi.listLibrary(), contentApi.listLibraryCategories()])
  if (result[0].status === 'fulfilled') {
    resources.value = result[0].value
  }
  if (result[1].status === 'fulfilled') {
    categories.value = result[1].value
  }
  if (result.some(item => item.status === 'rejected')) {
    ElMessage.warning('资料已保存，列表刷新暂时失败，请稍后点击刷新；无需重复保存')
  }
}

async function saveResource() {
  if (busy.value) {
    return
  }
  if (selectedFile.value) {
    await uploadResource()
  }
  else {
    await saveTextResource()
  }
}

function chooseVersionFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  selectVersionFile(file)
}

function selectVersionFile(file?: File) {
  if (!file || busy.value) {
    return
  }
  const result = inspectLibraryFile(file, versionForm.resource_kind)
  if (result.error) {
    ElMessage.warning(result.error)
    return
  }
  if (!versionFile.value) {
    versionKindBeforeReplacement.value = versionForm.resource_kind
  }
  versionFile.value = file
  versionForm.resource_kind = result.kind
}

function dropFile(event: DragEvent, edit = false) {
  if (busy.value) {
    return
  }
  const file = event.dataTransfer?.files[0]
  if (edit) {
    selectVersionFile(file)
  }
  else {
    selectResourceFile(file)
  }
}

async function saveTextResource() {
  if (busy.value) {
    return
  }
  if (!resourceForm.name.trim() || !resourceForm.category.trim()) {
    ElMessage.warning(!resourceForm.name.trim() ? '请填写资料名称' : '请填写资料分类')
    return
  }
  if (!resourceForm.text_content.trim()) {
    ElMessage.warning('请填写文本内容')
    return
  }
  busy.value = 'text'
  try {
    const item = await contentApi.createLibrary({
      resource_kind: resourceForm.resource_kind,
      category: categoryValue(resourceForm.category, resourceForm.resource_kind),
      name: resourceForm.name.trim(),
      text_content: resourceForm.text_content,
      data_level: resourceForm.data_level,
      notes: resourceForm.notes,
    })
    await finishResourceSave(item)
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
  finally {
    busy.value = ''
  }
}

async function uploadResource() {
  if (busy.value) {
    return
  }
  if (!resourceForm.name.trim() || !resourceForm.category.trim() || !selectedFile.value) {
    ElMessage.warning(!resourceForm.name.trim() ? '请填写资料名称' : !resourceForm.category.trim() ? '请填写资料分类' : '请选择要上传的文件')
    return
  }
  const result = inspectLibraryFile(selectedFile.value, resourceForm.resource_kind)
  if (result.error) {
    ElMessage.warning(result.error)
    return
  }
  if (!resourceForm.text_content.trim()) {
    ElMessage.warning('请填写文本内容')
    return
  }
  busy.value = 'upload'
  try {
    const item = await contentApi.uploadLibrary({
      resource_kind: result.kind,
      category: categoryValue(resourceForm.category, result.kind),
      name: resourceForm.name.trim(),
      data_level: resourceForm.data_level,
      notes: resourceForm.notes,
      text_content: resourceForm.text_content,
      file: selectedFile.value,
    })
    await finishResourceSave(item)
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
  finally {
    busy.value = ''
  }
}

function openVersionEditor(item: LibraryItem) {
  editingResource.value = item
  versionFile.value = undefined
  versionKindBeforeReplacement.value = item.resource_kind
  Object.assign(versionForm, {
    resource_kind: isProductResource(item) ? 'product' : item.resource_kind,
    category: item.category ? categoryLabel(item.category) : '',
    name: item.name,
    text_content: item.text_content || '',
    data_level: item.data_level,
    tags: [...(item.tags ?? [])],
    parent_id: item.parent_id || '',
    source_url: item.source_url || '',
    crawled_at: item.crawled_at || '',
    freshness: item.freshness || '',
    notes: item.notes || '',
  })
  versionDialogOpen.value = true
}

async function createVersion() {
  if (busy.value) {
    return
  }
  if (!editingResource.value || !versionForm.name.trim() || !versionForm.category.trim()) {
    ElMessage.warning(!versionForm.name.trim() ? '请填写资料名称' : '请填写资料分类')
    return
  }
  if (!versionForm.text_content.trim()) {
    ElMessage.warning('请填写文本内容')
    return
  }
  busy.value = 'version'
  try {
    const common = {
      expected_version: editingResource.value.version,
      resource_kind: versionForm.resource_kind,
      category: categoryValue(versionForm.category, versionForm.resource_kind, editingResource.value.category),
      name: versionForm.name,
      data_level: versionForm.data_level,
      tags: versionForm.tags,
      parent_id: versionForm.parent_id || null,
      source_url: versionForm.source_url || null,
      crawled_at: versionForm.crawled_at || null,
      freshness: versionForm.freshness || null,
      facts: editingResource.value.facts ?? [],
      notes: versionForm.notes,
      text_content: versionForm.text_content,
    }
    if (versionFile.value) {
      await contentApi.uploadLibraryVersion(editingResource.value.id, {
        ...common,
        file: versionFile.value,
      })
    }
    else {
      await contentApi.createLibraryVersion(editingResource.value.id, {
        ...common,
        metadata: editingResource.value.metadata ?? {},
      })
    }
    await load()
    versionDialogOpen.value = false
    ElMessage.success('已创建资料新版本')
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
  finally {
    busy.value = ''
  }
}

async function deleteResource(item: LibraryItem) {
  try {
    await ElMessageBox.confirm(`确定永久删除“${item.name}”及其全部历史版本和附件吗？删除后无法恢复。`, '永久删除资料', {
      type: 'warning',
      confirmButtonText: '永久删除',
      cancelButtonText: '取消',
    })
    await contentApi.deleteLibrary(item.id)
    resources.value = resources.value.filter(resource => item.resource_entry_id
      ? resource.resource_entry_id !== item.resource_entry_id
      : resource.id !== item.id,
    )
    ElMessage.success('资料已永久删除')
    await load()
  }
  catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(messageOf(error))
    }
  }
}

async function createRule() {
  if (!canManageRules.value) {
    return
  }
  if (!ruleForm.phrase.trim()) {
    ElMessage.warning('请填写检测词')
    return
  }
  busy.value = 'rule'
  try {
    await contentApi.createRule({ ...ruleForm })
    Object.assign(ruleForm, { phrase: '', replacement: '' })
    rules.value = await contentApi.listRules()
    ElMessage.success('检测规则已保存')
  }
  catch (error) {
    ElMessage.error(messageOf(error))
  }
  finally {
    busy.value = ''
  }
}

async function deleteRule(item: SensitiveRule) {
  if (!canManageRules.value) {
    return
  }
  try {
    await ElMessageBox.confirm(`确定永久删除“${item.phrase}”吗？删除后无法恢复，历史检测记录仍保留。`, '删除规则', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await contentApi.deleteRule(item.id)
    rules.value = await contentApi.listRules()
    ElMessage.success('规则已删除')
  }
  catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(messageOf(error))
    }
  }
}

onMounted(load)
</script>

<template>
  <div class="content-library-view" :aria-busy="loading">
    <section class="view-heading">
      <div>
        <h2>企业资料与素材</h2>
        <p>整理产品资料与创作素材，让每次创作都有据可循。</p>
      </div>
      <div class="heading-actions">
        <ElButton :loading="loading" @click="load">刷新</ElButton>
      </div>
    </section>

    <ElAlert
      v-if="loadError"
      type="error"
      title="资料服务暂时不可用"
      :description="loadError"
      :closable="false"
      show-icon
    />

    <ElTabs v-model="activeTab" class="library-tabs">
      <ElTabPane label="资料与素材" name="resources">
        <ContentLibraryCatalog
          :resources="resources"
          :categories="categories"
          :loading="loading"
          :can-edit="canEditResource"
          @create="openResourceCreator"
          @edit="openVersionEditor"
          @delete="deleteResource"
        />

        <ElDrawer
          v-model="createDrawerOpen"
          title="新增资料或素材"
          size="min(680px, 94vw)"
          class="library-editor-drawer"
          destroy-on-close
          :close-on-click-modal="!busy"
          :close-on-press-escape="!busy"
          :show-close="!busy"
        >
          <div class="library-layout library-create-layout">
            <section class="form-panel">
              <p class="editor-description">上传文件或填写正文，保存到你的素材库。</p>
              <ElForm
                label-position="top"
                class="create-form"
                :disabled="Boolean(busy)"
                @submit.prevent
              >
                <ElFormItem label="资料文件（可选）" class="full-width-field">
                  <div class="library-file-field">
                    <label
                      class="file-selector"
                      :aria-disabled="Boolean(busy)"
                      @dragover.prevent
                      @drop.prevent="dropFile($event)"
                    >
                      <ElIcon><UploadFilled /></ElIcon>
                      <span>{{ selectedFile ? '拖入或点击替换文件' : '拖入文件，或点击选择' }}</span>
                      <small>支持图片、音视频及文档</small>
                      <input
                        type="file"
                        :accept="libraryFileAccept"
                        :disabled="Boolean(busy)"
                        aria-label="选择资料文件"
                        @change="chooseFile"
                      >
                    </label>
                    <ContentLibraryFilePreview
                      v-if="selectedFile"
                      :file="selectedFile"
                      :disabled="Boolean(busy)"
                      @remove="selectedFile = undefined"
                    />
                  </div>
                </ElFormItem>
                <ElFormItem label="资源类型" required>
                  <ElSelect v-model="resourceForm.resource_kind" @change="changeResourceKind">
                    <ElOption
                      v-for="item in resourceKindOptions"
                      :key="item.value"
                      :label="item.label"
                      :value="item.value"
                    />
                  </ElSelect>
                </ElFormItem>
                <ElFormItem label="资料分类" required>
                  <ElAutocomplete
                    :key="resourceForm.resource_kind"
                    v-model="resourceForm.category"
                    :fetch-suggestions="suggestResourceCategories"
                    :debounce="0"
                    clearable
                    :maxlength="128"
                    class="category-input"
                    placeholder="选择已有分类或输入新分类"
                    aria-label="资料分类"
                  />
                </ElFormItem>
                <ElFormItem label="资料名称" required>
                  <ElInput v-model="resourceForm.name" placeholder="例如：产品卖点手册" />
                </ElFormItem>
                <ElFormItem label="数据等级">
                  <ElSelect v-model="resourceForm.data_level">
                    <ElOption
                      v-for="item in dataLevelOptions"
                      :key="item.value"
                      :label="item.label"
                      :value="item.value"
                    />
                  </ElSelect>
                </ElFormItem>
                <ElFormItem label="文本内容" required class="full-width-field">
                  <ElInput
                    v-model="resourceForm.text_content"
                    type="textarea"
                    :rows="4"
                    :maxlength="1000000"
                    placeholder="请输入资料正文或素材说明"
                  />
                </ElFormItem>
                <ElFormItem label="备注" class="full-width-field">
                  <ElInput v-model="resourceForm.notes" type="textarea" :rows="3" />
                </ElFormItem>
              </ElForm>
            </section>
          </div>
          <template #footer>
            <ElButton :disabled="Boolean(busy)" @click="createDrawerOpen = false">取消</ElButton>
            <ElButton type="primary" :loading="Boolean(busy)" @click="saveResource">保存</ElButton>
          </template>
        </ElDrawer>
      </ElTabPane>

      <ElTabPane label="关键词、敏感词与禁用词" name="rules">
        <ElAlert
          title="命中规则后只提示用户，由用户修改、重新生成或确认知悉；系统不会自动替换或封禁。"
          type="info"
          :closable="false"
          show-icon
        />
        <div class="rule-layout" :class="{ 'rules-readonly': !canManageRules }">
          <section v-if="canManageRules" class="form-panel">
            <h3>新增检测规则</h3>
            <ElForm label-position="top">
              <ElFormItem label="检测词" required><ElInput v-model="ruleForm.phrase" /></ElFormItem>
              <ElFormItem label="规则类型">
                <ElRadioGroup v-model="ruleForm.rule_type">
                  <ElRadio value="keyword">关键词</ElRadio>
                  <ElRadio value="sensitive">敏感词</ElRadio>
                  <ElRadio value="forbidden">禁用词</ElRadio>
                </ElRadioGroup>
              </ElFormItem>
              <ElFormItem label="修改建议（仅提示）">
                <ElInput v-model="ruleForm.replacement" />
              </ElFormItem>
              <ElButton type="primary" :loading="busy === 'rule'" @click="createRule">保存规则</ElButton>
            </ElForm>
          </section>
          <section class="list-panel">
            <ElTable :data="rules" height="480" empty-text="暂无检测规则">
              <ElTableColumn prop="phrase" label="检测词" min-width="180" />
              <ElTableColumn label="规则类型" width="110">
                <template #default="scope">
                  <ElTag :type="scope.row.rule_type === 'forbidden' ? 'danger' : scope.row.rule_type === 'sensitive' ? 'warning' : 'info'">
                    {{ scope.row.rule_type === 'forbidden' ? '禁用词' : scope.row.rule_type === 'sensitive' ? '敏感词' : '关键词' }}
                  </ElTag>
                </template>
              </ElTableColumn>
              <ElTableColumn prop="replacement" label="人工修改建议" min-width="180" />
              <ElTableColumn v-if="canManageRules" label="操作" width="90">
                <template #default="scope">
                  <ElButton link type="danger" @click="deleteRule(scope.row)">删除</ElButton>
                </template>
              </ElTableColumn>
            </ElTable>
          </section>
        </div>
      </ElTabPane>
    </ElTabs>

    <ElDrawer
      v-model="versionDialogOpen"
      title="编辑资料"
      size="min(680px, 94vw)"
      class="library-editor-drawer"
      destroy-on-close
      :close-on-click-modal="!busy"
      :close-on-press-escape="!busy"
      :show-close="!busy"
    >
      <p class="editor-description">保存为新版本，保留历史记录。不选择新文件则保留原件。</p>
      <ElForm
        label-position="top"
        class="version-form"
        :disabled="Boolean(busy)"
        @submit.prevent
      >
        <div class="version-form-grid">
          <ElFormItem label="资源类型" required>
            <ElSelect v-model="versionForm.resource_kind">
              <ElOption
                v-for="item in resourceKindOptions"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="资料分类" required>
            <ElAutocomplete
              :key="versionForm.resource_kind"
              v-model="versionForm.category"
              :fetch-suggestions="suggestVersionCategories"
              :debounce="0"
              clearable
              :maxlength="128"
              class="category-input"
              placeholder="选择已有分类或输入新分类"
              aria-label="资料分类"
            />
          </ElFormItem>
          <ElFormItem label="资料名称" required>
            <ElInput v-model="versionForm.name" />
          </ElFormItem>
          <ElFormItem label="数据等级">
            <ElSelect v-model="versionForm.data_level">
              <ElOption
                v-for="item in dataLevelOptions"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </ElSelect>
          </ElFormItem>
        </div>
        <ElFormItem label="文本内容" required>
          <ElInput
            v-model="versionForm.text_content"
            type="textarea"
            :rows="4"
            :maxlength="1000000"
            placeholder="请输入资料正文或素材说明"
          />
        </ElFormItem>
        <ElFormItem label="替换文件（可选）">
          <div class="library-file-field">
            <label
              class="file-selector"
              :aria-disabled="Boolean(busy)"
              @dragover.prevent
              @drop.prevent="dropFile($event, true)"
            >
              <ElIcon><UploadFilled /></ElIcon>
              <span>{{ versionFile ? '拖入或点击重新选择' : '拖入文件，或点击替换原件' }}</span>
              <small>不选择文件则保留当前原件</small>
              <input
                type="file"
                :accept="libraryFileAccept"
                :disabled="Boolean(busy)"
                aria-label="选择替换文件"
                @change="chooseVersionFile"
              >
            </label>
            <ContentLibraryFilePreview
              v-if="versionFile"
              :file="versionFile"
              :disabled="Boolean(busy)"
              remove-label="取消替换"
              @remove="cancelVersionFile"
            />
          </div>
        </ElFormItem>
        <ElFormItem label="备注">
          <ElInput v-model="versionForm.notes" type="textarea" :rows="3" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton :disabled="Boolean(busy)" @click="versionDialogOpen = false">取消</ElButton>
        <ElButton type="primary" :loading="busy === 'version'" @click="createVersion">
          创建新版本
        </ElButton>
      </template>
    </ElDrawer>
  </div>
</template>

<style scoped>
.category-input {
  width: 100%;
}

.content-library-view {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
  container: content-library / inline-size;
  gap: var(--xi-component-content-gap-md);
}

.view-heading,
.panel-heading,
.panel-heading > div,
.form-actions,
.heading-actions {
  display: flex;
  align-items: center;
}

.view-heading,
.panel-heading {
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-md);
}

.view-heading h2,
.view-heading p,
.form-panel h3,
.form-panel p,
.panel-heading h3 {
  margin: 0;
}

.view-heading h2 {
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading1-font-size);
  font-weight: 600;
  line-height: 28px;
}

.view-heading p {
  margin-top: 2px;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 13px;
}

.form-panel p {
  margin-top: var(--xi-component-content-gap-xxs);
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-body-font-size);
}

.library-layout,
.rule-layout {
  display: grid;
  grid-template-columns: minmax(var(--xi-component-content-card-min-width), 0.75fr) minmax(0, 2fr);
  gap: var(--xi-component-content-gap-md);
}

.library-create-layout {
  grid-template-columns: minmax(0, 1fr);
}

.library-create-layout .form-panel {
  padding: 0;
  border: 0;
}

.library-tabs :deep(.el-tabs__header) {
  margin-bottom: 20px;
}

.library-tabs :deep(.el-tabs__item) {
  height: 44px;
}

.editor-description {
  margin: 0 0 20px;
  color: var(--xi-semantic-color-text-secondary);
  line-height: 1.6;
}

.create-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 20px;
}

.full-width-field {
  grid-column: 1 / -1;
}

.form-panel,
.list-panel {
  min-width: 0;
  padding: var(--xi-component-content-gap-lg);
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.form-panel h3,
.panel-heading h3 {
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading3-font-size);
}

.form-panel :deep(.el-form) {
  margin-top: var(--xi-component-content-gap-md);
}

.form-actions,
.panel-heading > div,
.heading-actions {
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-xs);
}

.library-file-field {
  display: flex;
  width: 100%;
  min-width: 0;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--xi-component-content-gap-sm);
}

.file-selector {
  position: relative;
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  overflow-wrap: anywhere;
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  justify-content: center;
  min-height: 128px;
  padding: 20px;
  color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-page);
  border: 1px dashed var(--xi-semantic-color-border-default);
  border-radius: 12px;
  cursor: pointer;
  transition:
    background-color 180ms ease,
    border-color 180ms ease;
}

.file-selector:hover,
.file-selector:focus-within {
  border-color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-brand-subtle);
}

.file-selector .el-icon {
  font-size: 28px;
}

.file-selector small {
  color: var(--xi-semantic-color-text-tertiary);
}

.file-selector[aria-disabled='true'] {
  cursor: not-allowed;
  opacity: 0.6;
}

.file-selector span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-selector input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  cursor: pointer;
  opacity: 0;
}

.file-selector:focus-within {
  outline: 2px solid var(--xi-semantic-color-text-brand);
  outline-offset: 2px;
}

.panel-heading {
  margin-bottom: var(--xi-component-content-gap-md);
}

.panel-heading span {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.rule-layout {
  margin-top: var(--xi-component-content-gap-md);
}
.rule-layout.rules-readonly {
  grid-template-columns: minmax(0, 1fr);
}

.version-form {
  margin-top: var(--xi-component-content-gap-md);
}

.version-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 var(--xi-component-content-gap-md);
}

@container content-library (max-width: 760px) {
  .library-layout,
  .rule-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .form-panel,
  .list-panel {
    padding: var(--xi-component-content-gap-md);
  }
}

@media (max-width: 640px) {
  .version-form-grid,
  .create-form {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (prefers-reduced-motion: reduce) {
  .file-selector {
    transition: none;
  }
}
</style>
