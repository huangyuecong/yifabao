<script setup lang="ts">
import type { LibraryItem, LibraryResourceKind, StoryboardShot } from '@/api/agent/content'
import { computed, nextTick, ref } from 'vue'
import draggable from 'vuedraggable'
import ContentResourcePicker from './ContentResourcePicker.vue'
import { displayLabel, transitionLabels } from './contentWorkflow'

interface StoryboardAssetOption {
  id: string
  name: string
}

const props = withDefaults(defineProps<{
  modelValue: StoryboardShot[]
  enterpriseAssets?: LibraryItem[]
  taskAssets?: StoryboardAssetOption[]
  transitions?: string[]
  readonly?: boolean
}>(), {
  enterpriseAssets: () => [],
  taskAssets: () => [],
  transitions: () => [],
  readonly: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: StoryboardShot[]]
}>()

const enterpriseKinds: LibraryResourceKind[] = ['image', 'logo']
const enterpriseAssetIds = computed(() => new Set(props.enterpriseAssets.map(item => item.id)))
const taskAssetIds = computed(() => new Set(props.taskAssets.map(item => item.id)))
const activeShotIndex = ref(0)
const totalDuration = computed(() => props.modelValue.reduce(
  (total, shot) => total + Math.max(Number(shot.duration_seconds) || 0, 0),
  0,
))

function shotKey(shot: StoryboardShot, index: number) {
  return shot.id || `shot-${index}-${shot.order}`
}

function timelineWidth(shot: StoryboardShot) {
  return `${Math.max(96, Math.min((Number(shot.duration_seconds) || 1) * 34, 260))}px`
}

async function focusShot(index: number) {
  activeShotIndex.value = index
  await nextTick()
  document.getElementById(`storyboard-shot-${index + 1}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function commit(next: StoryboardShot[]) {
  emit('update:modelValue', next.map((shot, index) => ({ ...shot, order: index + 1 })))
}

function updateShot(index: number, patch: Partial<StoryboardShot>) {
  commit(props.modelValue.map((shot, current) => current === index ? { ...shot, ...patch } : shot))
}

function updateEnterpriseAsset(index: number, shot: StoryboardShot, ids: string[]) {
  if (ids.length) {
    updateShot(index, { asset_id: ids.at(-1) || null })
  }
  else if (shot.asset_id && enterpriseAssetIds.value.has(shot.asset_id)) {
    updateShot(index, { asset_id: null })
  }
}

function updateTaskAsset(index: number, shot: StoryboardShot, value: unknown) {
  if (value) {
    updateShot(index, { asset_id: String(value) })
  }
  else if (shot.asset_id && taskAssetIds.value.has(shot.asset_id)) {
    updateShot(index, { asset_id: null })
  }
}

function addShot() {
  commit([
    ...props.modelValue,
    {
      order: props.modelValue.length + 1,
      visual: '',
      narration: '',
      subtitle: '',
      duration_seconds: 3,
      transition: '',
      transition_duration_seconds: 0.4,
      fit_mode: 'contain',
      asset_id: null,
    },
  ])
}

function removeShot(index: number) {
  commit(props.modelValue.filter((_, current) => current !== index))
}

function moveShot(index: number, offset: number) {
  const target = index + offset
  if (target < 0 || target >= props.modelValue.length) {
    return
  }
  const next = [...props.modelValue]
  const [current] = next.splice(index, 1)
  if (!current) {
    return
  }
  next.splice(target, 0, current)
  commit(next)
}
</script>

<template>
  <div class="storyboard-editor">
    <div v-if="!modelValue.length" class="empty-shots">
      <ElEmpty description="暂无镜头，请添加镜头或重新生成脚本" :image-size="140" />
    </div>
    <template v-else>
      <section class="storyboard-timeline" aria-label="分镜时间线">
        <header>
          <div>
            <strong>分镜时间线</strong>
            <span>{{ modelValue.length }} 个镜头 · 约 {{ totalDuration.toFixed(1) }} 秒</span>
          </div>
          <small>拖动镜头可调整成片顺序</small>
        </header>
        <draggable
          :model-value="modelValue"
          item-key="order"
          class="timeline-track"
          handle=".timeline-drag-handle"
          :disabled="readonly"
          @update:model-value="commit($event)"
        >
          <template #item="{ element: shot, index }">
            <button
              type="button"
              class="timeline-clip"
              :class="{ 'is-active': activeShotIndex === index }"
              :style="{ width: timelineWidth(shot) }"
              :aria-label="`查看镜头 ${index + 1}，建议时长 ${shot.duration_seconds} 秒`"
              @click="focusShot(index)"
            >
              <span class="timeline-drag-handle" aria-hidden="true">⋮⋮</span>
              <strong>镜头 {{ index + 1 }}</strong>
              <small>{{ shot.duration_seconds }} 秒</small>
              <em v-if="shot.transition">{{ displayLabel(shot.transition, transitionLabels, '其他转场') }}</em>
            </button>
          </template>
        </draggable>
      </section>

      <draggable
        :model-value="modelValue"
        tag="ol"
        item-key="order"
        aria-label="视频分镜列表"
        handle=".shot-drag-handle"
        :disabled="readonly"
        @update:model-value="commit($event)"
      >
        <template #item="{ element: shot, index }">
          <li
            :id="`storyboard-shot-${index + 1}`"
            :key="shotKey(shot, index)"
            class="shot-card"
            :class="{ 'is-active': activeShotIndex === index }"
            @focusin="activeShotIndex = index"
          >
            <header>
              <div>
                <button
                  v-if="!readonly"
                  type="button"
                  class="shot-drag-handle"
                  :aria-label="`拖动镜头 ${index + 1} 调整顺序`"
                  title="拖动调整顺序"
                >
                  ⋮⋮
                </button>
                <span class="shot-index">{{ String(index + 1).padStart(2, '0') }}</span>
                <strong>镜头 {{ index + 1 }}</strong>
              </div>
              <div v-if="!readonly" class="shot-actions">
                <ElButton
                  text
                  :disabled="index === 0"
                  :aria-label="`镜头 ${index + 1} 上移`"
                  @click="moveShot(index, -1)"
                >
                  上移
                </ElButton>
                <ElButton
                  text
                  :disabled="index === modelValue.length - 1"
                  :aria-label="`镜头 ${index + 1} 下移`"
                  @click="moveShot(index, 1)"
                >
                  下移
                </ElButton>
                <ElButton text type="danger" @click="removeShot(index)">删除</ElButton>
              </div>
            </header>

            <div class="shot-form">
              <ElFormItem label="画面 / 素材说明" class="wide-field">
                <ElInput
                  :model-value="shot.visual"
                  type="textarea"
                  :rows="2"
                  :readonly="readonly"
                  placeholder="描述人物、产品、环境、构图与动作"
                  @update:model-value="updateShot(index, { visual: String($event) })"
                />
              </ElFormItem>
              <ElFormItem label="口播 / 旁白" class="wide-field">
                <ElInput
                  :model-value="shot.narration"
                  type="textarea"
                  :rows="2"
                  :readonly="readonly"
                  @update:model-value="updateShot(index, { narration: String($event) })"
                />
              </ElFormItem>
              <ElFormItem label="字幕文本" class="wide-field">
                <ElInput
                  :model-value="shot.subtitle"
                  type="textarea"
                  :rows="2"
                  :readonly="readonly"
                  @update:model-value="updateShot(index, { subtitle: String($event) })"
                />
              </ElFormItem>
              <ElFormItem label="建议时长（秒）">
                <ElInputNumber
                  :model-value="shot.duration_seconds"
                  :min="1"
                  :disabled="readonly"
                  @update:model-value="updateShot(index, { duration_seconds: Number($event) || 1 })"
                />
              </ElFormItem>
              <ElFormItem label="镜头转场">
                <ElSelect
                  v-if="transitions.length"
                  :model-value="shot.transition"
                  :disabled="readonly"
                  placeholder="选择转场"
                  @update:model-value="updateShot(index, { transition: String($event) })"
                >
                  <ElOption
                    v-for="transition in transitions"
                    :key="transition"
                    :label="displayLabel(transition, transitionLabels, '其他转场')"
                    :value="transition"
                  />
                </ElSelect>
                <ElInput
                  v-else
                  :model-value="displayLabel(shot.transition, transitionLabels, '其他转场')"
                  readonly
                  placeholder="当前合成服务未声明可用转场"
                />
              </ElFormItem>
              <ElFormItem label="转场时长（秒）">
                <ElInputNumber
                  :model-value="shot.transition_duration_seconds ?? 0.4"
                  :min="0"
                  :max="2"
                  :step="0.1"
                  :precision="1"
                  :disabled="readonly || !shot.transition || shot.transition === 'none'"
                  @update:model-value="updateShot(index, { transition_duration_seconds: Number($event) || 0 })"
                />
              </ElFormItem>
              <ElFormItem label="画面适配">
                <ElSelect
                  :model-value="shot.fit_mode || 'contain'"
                  :disabled="readonly"
                  @update:model-value="updateShot(index, { fit_mode: String($event) })"
                >
                  <ElOption label="完整显示（可能留边）" value="contain" />
                  <ElOption label="铺满画面（可能裁切）" value="cover" />
                </ElSelect>
              </ElFormItem>
              <ElFormItem v-if="enterpriseAssets.length" label="企业画面素材（可选）" class="wide-field">
                <ContentResourcePicker
                  :model-value="shot.asset_id && enterpriseAssetIds.has(shot.asset_id) ? [shot.asset_id] : []"
                  :resources="enterpriseAssets"
                  :kinds="enterpriseKinds"
                  title="选择本镜头企业素材"
                  description="按分类搜索并预览企业图片或 Logo；每个镜头最多绑定一项。"
                  button-text="浏览企业图片库"
                  :multiple="false"
                  :preserve-unknown="false"
                  @update:model-value="updateEnterpriseAsset(index, shot, $event)"
                />
              </ElFormItem>
              <ElFormItem v-if="taskAssets.length" label="本任务生成图片（可选）">
                <ElSelect
                  :model-value="shot.asset_id && taskAssetIds.has(shot.asset_id) ? shot.asset_id : undefined"
                  clearable
                  filterable
                  :disabled="readonly"
                  placeholder="选择本任务已有图片"
                  @update:model-value="updateTaskAsset(index, shot, $event)"
                >
                  <ElOption
                    v-for="asset in taskAssets"
                    :key="asset.id"
                    :label="asset.name"
                    :value="asset.id"
                  />
                </ElSelect>
              </ElFormItem>
            </div>
          </li>
        </template>
      </draggable>
    </template>

    <ElButton
      v-if="!readonly"
      class="add-shot"
      plain
      type="primary"
      @click="addShot"
    >
      新增镜头
    </ElButton>
  </div>
</template>

<style scoped>
.storyboard-editor,
.storyboard-editor ol {
  display: grid;
  gap: var(--xi-component-content-gap-md);
}

.storyboard-timeline {
  display: grid;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-md);
  overflow: hidden;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.storyboard-timeline header,
.storyboard-timeline header > div {
  display: flex;
  align-items: center;
}

.storyboard-timeline header {
  justify-content: space-between;
  gap: var(--xi-component-content-gap-md);
}

.storyboard-timeline header > div {
  gap: var(--xi-component-content-gap-sm);
}

.storyboard-timeline header span,
.storyboard-timeline header small,
.timeline-clip small {
  color: var(--xi-semantic-color-text-secondary);
}

.timeline-track {
  display: flex;
  gap: var(--xi-component-content-gap-xs);
  padding-bottom: var(--xi-component-content-gap-xs);
  overflow-x: auto;
}

.timeline-clip {
  display: grid;
  min-width: 96px;
  flex: 0 0 auto;
  grid-template-columns: auto 1fr auto;
  gap: var(--xi-component-content-gap-xs);
  align-items: center;
  padding: var(--xi-component-content-gap-sm);
  color: var(--xi-semantic-color-text-primary);
  text-align: left;
  cursor: pointer;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-control);
}

.timeline-clip.is-active,
.shot-card.is-active {
  border: var(--xi-component-content-border-brand);
}

.timeline-clip em {
  grid-column: 2 / -1;
  overflow: hidden;
  color: var(--xi-semantic-color-text-brand);
  font-style: normal;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.timeline-drag-handle,
.shot-drag-handle {
  color: var(--xi-semantic-color-text-secondary);
  cursor: grab;
}

.shot-drag-handle {
  padding: var(--xi-component-content-gap-xs);
  background: transparent;
  border: 0;
}

.storyboard-editor ol {
  padding: 0;
  margin: 0;
  list-style: none;
}

.shot-card {
  overflow: hidden;
  background: var(--xi-semantic-color-background-container);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.shot-card header,
.shot-card header > div,
.shot-actions {
  display: flex;
  align-items: center;
}

.shot-card header {
  justify-content: space-between;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-sm) var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-brand-subtle);
  border-bottom: var(--xi-component-content-border);
}

.shot-card header > div,
.shot-actions {
  gap: var(--xi-component-content-gap-xs);
}

.shot-index {
  display: inline-grid;
  width: var(--xi-component-content-control-height);
  height: var(--xi-component-content-control-height);
  color: var(--xi-semantic-color-text-inverse);
  background: var(--xi-semantic-color-background-brand-section);
  place-items: center;
}

.shot-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(var(--xi-component-content-card-min-width), 1fr));
  gap: 0 var(--xi-component-content-gap-md);
  padding: var(--xi-component-content-gap-md);
}

.wide-field {
  grid-column: 1 / -1;
}

.add-shot {
  justify-self: start;
}

.empty-shots {
  border: var(--xi-component-content-border);
}

@media (max-width: 960px) {
  .storyboard-timeline header {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
