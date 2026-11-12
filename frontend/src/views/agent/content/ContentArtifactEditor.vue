<script setup lang="ts">
import type { Artifact, ArtifactContent, StoryboardShot, SubtitleSegment } from '@/api/agent/content'
import { computed } from 'vue'
import ContentComplianceHits from './ContentComplianceHits.vue'
import {
  displayLabel,
  transitionLabels,
  voiceLabels,
} from './contentWorkflow'
import StoryboardEditor from './StoryboardEditor.vue'

const props = withDefaults(defineProps<{
  artifact: Artifact
  modelValue: ArtifactContent
  transitions?: string[]
  fixedTitle?: string
}>(), {
  transitions: () => [],
  fixedTitle: '',
})

const emit = defineEmits<{
  'update:modelValue': [value: ArtifactContent]
}>()

const candidates = computed(() => props.modelValue.candidates ?? [])
const shots = computed(() => Array.isArray(props.modelValue.shots) ? props.modelValue.shots : [])
const segments = computed(() => Array.isArray(props.modelValue.segments) ? props.modelValue.segments : [])

function updateContent(patch: Partial<ArtifactContent>) {
  emit('update:modelValue', { ...props.modelValue, ...patch })
}

function candidateValue(candidate: Record<string, unknown>, key: string) {
  const value = candidate[key]
  return typeof value === 'string' ? value : ''
}

function updateCandidate(index: number, key: string, value: string) {
  const next = candidates.value.map((candidate, current) =>
    current === index ? { ...candidate, [key]: value } : candidate,
  )
  const selectedIndex = props.modelValue.selected_candidate_index
  const selectedSnapshot = props.modelValue.selected_candidate
  const editingSelected = selectedIndex === index
    || (
      typeof selectedIndex !== 'number'
      && selectedSnapshot != null
      && JSON.stringify(selectedSnapshot) === JSON.stringify(candidates.value[index])
    )
  updateContent({
    candidates: next,
    ...(editingSelected && next[index] ? { selected_candidate: next[index] } : {}),
  })
}

function updateShots(value: StoryboardShot[]) {
  updateContent({ shots: value })
}

function updateSegment(index: number, patch: Partial<SubtitleSegment>) {
  const next = segments.value.map((segment, current) =>
    current === index ? { ...segment, ...patch } : segment,
  )
  updateContent({ segments: next })
}

function numberValue(value: unknown) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function textValue(key: string) {
  const value = props.modelValue[key]
  return typeof value === 'string' ? value : ''
}
</script>

<template>
  <div class="artifact-business-editor">
    <template v-if="['topic', 'copy'].includes(artifact.kind)">
      <section v-for="(candidate, index) in candidates" :key="index" class="candidate-editor">
        <h4 v-if="artifact.kind === 'copy'">候选文案 {{ index + 1 }}</h4>
        <div class="editor-grid" :class="{ 'topic-editor-grid': artifact.kind === 'topic' }">
          <ElFormItem v-if="artifact.kind === 'topic'" label="标题">
            <ElInput
              :model-value="candidateValue(candidate, 'title')"
              @update:model-value="updateCandidate(index, 'title', String($event))"
            />
          </ElFormItem>
          <ElFormItem v-else label="已确认标题" class="full-field">
            <div class="fixed-copy-title">{{ fixedTitle || '请返回上一步确认标题' }}</div>
          </ElFormItem>
          <ElFormItem v-if="artifact.kind === 'copy'" label="文案正文" class="full-field">
            <ElInput
              :model-value="candidateValue(candidate, 'body')"
              type="textarea"
              :rows="12"
              @update:model-value="updateCandidate(index, 'body', String($event))"
            />
          </ElFormItem>
        </div>
      </section>
      <ElForm v-if="!candidates.length" label-position="top">
        <ElFormItem v-if="artifact.kind === 'copy'" label="已确认标题">
          <div class="fixed-copy-title">{{ fixedTitle || '请返回上一步确认标题' }}</div>
        </ElFormItem>
        <ElFormItem :label="artifact.kind === 'copy' ? '文案正文' : '标题'">
          <ElInput
            :model-value="textValue(modelValue.body != null ? 'body' : 'text')"
            :type="artifact.kind === 'copy' ? 'textarea' : undefined"
            :rows="artifact.kind === 'copy' ? 12 : undefined"
            @update:model-value="updateContent({ [modelValue.body != null ? 'body' : 'text']: String($event) })"
          />
        </ElFormItem>
      </ElForm>
    </template>

    <template v-else-if="artifact.kind === 'storyboard'">
      <StoryboardEditor
        :model-value="shots"
        :transitions="transitions"
        @update:model-value="updateShots"
      />
    </template>

    <template v-else-if="artifact.kind === 'subtitle'">
      <section v-for="(segment, index) in segments" :key="segment.id || index" class="subtitle-editor">
        <strong>字幕 {{ index + 1 }}</strong>
        <ElFormItem label="字幕文字" class="subtitle-text">
          <ElInput
            :model-value="segment.text"
            @update:model-value="updateSegment(index, { text: String($event), timing_source: 'edited' })"
          />
        </ElFormItem>
        <ElFormItem label="开始时间（秒）">
          <ElInputNumber
            :model-value="numberValue(segment.start_seconds)"
            :min="0"
            :precision="3"
            @update:model-value="updateSegment(index, { start_seconds: Number($event) || 0, timing_source: 'edited' })"
          />
        </ElFormItem>
        <ElFormItem label="结束时间（秒）">
          <ElInputNumber
            :model-value="numberValue(segment.end_seconds)"
            :min="0"
            :precision="3"
            @update:model-value="updateSegment(index, { end_seconds: Number($event) || 0, timing_source: 'edited' })"
          />
        </ElFormItem>
      </section>
    </template>

    <template v-else-if="artifact.kind === 'image'">
      <ElDescriptions :column="2" border>
        <ElDescriptionsItem label="图片提示词" :span="2">{{ textValue('prompt') || '—' }}</ElDescriptionsItem>
        <ElDescriptionsItem label="场景">{{ textValue('scene') || '—' }}</ElDescriptionsItem>
        <ElDescriptionsItem label="画面风格">{{ textValue('style') || '—' }}</ElDescriptionsItem>
        <ElDescriptionsItem label="图片比例">{{ textValue('ratio') || '由模型决定' }}</ElDescriptionsItem>
        <ElDescriptionsItem label="图片尺寸">{{ textValue('size') || '由模型决定' }}</ElDescriptionsItem>
      </ElDescriptions>
    </template>

    <template v-else-if="artifact.kind === 'voice'">
      <ElDescriptions :column="2" border>
        <ElDescriptionsItem label="配音文本" :span="2">{{ textValue('text') || '—' }}</ElDescriptionsItem>
        <ElDescriptionsItem label="音色">
          {{ displayLabel(textValue('voice'), voiceLabels, '其他音色') }}
        </ElDescriptionsItem>
        <ElDescriptionsItem label="时长">{{ numberValue(modelValue.duration).toFixed(2) }} 秒</ElDescriptionsItem>
        <ElDescriptionsItem label="语速">{{ modelValue.speed ?? '默认' }}</ElDescriptionsItem>
        <ElDescriptionsItem label="音量">{{ modelValue.volume ?? '默认' }}</ElDescriptionsItem>
      </ElDescriptions>
    </template>

    <template v-else-if="artifact.kind === 'compliance'">
      <ElAlert
        :title="modelValue.passed ? '未发现需要提示的规则命中' : '检测到需要人工处理的内容'"
        :type="modelValue.passed ? 'success' : 'warning'"
        :closable="false"
        show-icon
      />
      <ElEmpty v-if="!modelValue.hits?.length" description="没有规则命中" />
      <ContentComplianceHits v-else :hits="modelValue.hits ?? []" />
    </template>

    <template v-else>
      <ElDescriptions :column="1" border>
        <ElDescriptionsItem label="内容说明">{{ textValue('text') || textValue('summary') || '请通过预览查看结果' }}</ElDescriptionsItem>
        <ElDescriptionsItem v-if="modelValue.transition" label="转场">
          {{ displayLabel(String(modelValue.transition), transitionLabels, '其他转场') }}
        </ElDescriptionsItem>
      </ElDescriptions>
    </template>
  </div>
</template>

<style scoped>
.artifact-business-editor,
.candidate-editor,
.editor-grid,
.subtitle-editor {
  display: grid;
  gap: var(--xi-component-content-gap-md);
}

.candidate-editor,
.subtitle-editor {
  padding: var(--xi-component-content-gap-md);
  background: var(--xi-semantic-color-background-page);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.candidate-editor h4 {
  margin: 0;
}

.editor-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.topic-editor-grid {
  grid-template-columns: 1fr;
}

.full-field,
.subtitle-text {
  grid-column: 1 / -1;
}

.fixed-copy-title {
  width: 100%;
  min-height: var(--xi-component-content-control-height);
  padding: var(--xi-component-content-gap-xs) var(--xi-component-content-gap-sm);
  overflow-wrap: anywhere;
  color: var(--xi-semantic-color-text-primary);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand-subtle);
  border-radius: var(--xi-component-content-radius-control);
}

.subtitle-editor {
  grid-template-columns: minmax(240px, 1fr) auto auto;
  align-items: end;
}

.subtitle-editor > strong {
  grid-column: 1 / -1;
}

@media (max-width: 900px) {
  .editor-grid,
  .subtitle-editor {
    grid-template-columns: 1fr;
  }
}
</style>
