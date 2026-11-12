<script setup lang="ts">
import type { Artifact, ComplianceResult } from '@/api/agent/content'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ContentComplianceHits from './ContentComplianceHits.vue'
import {
  artifactKindLabels,
  artifactStatusLabels,
  artifactText,
  candidateText,
  displayLabel,
  formatDate,
  isArtifactContentEditable,
  voiceLabels,
} from './contentWorkflow'

const props = defineProps<{
  artifact: Artifact
  selected?: boolean
  mediaUrl?: string
  compliance?: ComplianceResult
  selectionDisabled?: boolean
  fixedTitle?: string
  currentlyConfirmed: boolean
}>()

const emit = defineEmits<{
  select: [artifact: Artifact]
  edit: [artifact: Artifact]
  remove: [artifact: Artifact]
  preview: [artifact: Artifact]
  download: [artifact: Artifact]
  compliance: [artifact: Artifact]
  chooseCandidate: [artifact: Artifact, index: number]
}>()

const selectedCandidate = computed(() => {
  const value = props.artifact.content.selected_candidate_index
  return typeof value === 'number' ? value : undefined
})
const candidates = computed(() => props.artifact.content.candidates ?? [])
const isCurrentlyConfirmed = computed(() => props.currentlyConfirmed)
const effectiveStatus = computed(() => {
  if (isCurrentlyConfirmed.value) {
    return 'confirmed'
  }
  if (props.selected) {
    return 'selected'
  }
  // Older data kept the historical `confirmed` / `selected` flag on an
  // artifact after the stage moved to another version.  The stage binding is
  // authoritative, so these superseded versions are ordinary drafts again.
  if (['confirmed', 'selected'].includes(props.artifact.status)) {
    return 'draft'
  }
  return props.artifact.status
})
const canRemove = computed(() => effectiveStatus.value === 'draft')
const effectiveStatusLabel = computed(() =>
  props.artifact.kind === 'copy' && (props.selected || isCurrentlyConfirmed.value)
    ? '当前文案'
    : displayLabel(effectiveStatus.value, artifactStatusLabels, '其他状态'),
)
const removeDisabledReason = computed(() => {
  if (props.selected) {
    return props.artifact.kind === 'copy'
      ? '当前文案不能删除，请先选择其他版本'
      : '当前使用中的版本不能删除，请先选择其他版本'
  }
  if (isCurrentlyConfirmed.value) {
    return '已确认版本不能删除'
  }
  return '只有待确认的草稿版本可以删除'
})
const complianceSummary = computed<ComplianceResult | undefined>(() => {
  if (props.compliance) {
    return props.compliance
  }
  if (
    props.artifact.kind === 'compliance'
    && typeof props.artifact.content.passed === 'boolean'
    && Array.isArray(props.artifact.content.hits)
  ) {
    return {
      artifact_id: props.artifact.id,
      passed: props.artifact.content.passed,
      hits: props.artifact.content.hits,
    }
  }
  return undefined
})
const statusType = computed(() => {
  if (effectiveStatus.value === 'confirmed') {
    return 'success'
  }
  if (effectiveStatus.value === 'stale') {
    return 'warning'
  }
  return 'info'
})
const mediaFacts = computed(() => {
  const content = props.artifact.content
  const facts: Array<{ label: string, value: string }> = []
  const add = (label: string, value: unknown) => {
    if (value !== undefined && value !== null && value !== '') {
      facts.push({ label, value: String(value) })
    }
  }
  if (props.artifact.kind === 'voice') {
    add('音色', displayLabel(String(content.voice || ''), voiceLabels, '其他音色'))
    add('时长', content.duration ? `${Number(content.duration).toFixed(2)} 秒` : '')
    add('语速', content.speed)
    add('音量', content.volume)
  }
  if (props.artifact.kind === 'ai_video_clip') {
    add('对应镜头', content.shot_index != null ? `镜头 ${Number(content.shot_index) + 1}` : '')
    add('镜头时长', content.duration_seconds ? `${content.duration_seconds} 秒` : '')
    add('画面比例', content.ratio)
    add('清晰度', content.resolution)
  }
  if (props.artifact.kind === 'video') {
    add('成片尺寸', content.width && content.height ? `${content.width} × ${content.height}` : '')
    add('成片时长', content.duration_seconds ? `${Number(content.duration_seconds).toFixed(2)} 秒` : '')
    add('镜头数量', content.clip_count ? `${content.clip_count} 个` : '')
    add('主配音', content.voice_applied ? '已加入' : '未加入')
    add('字幕', content.subtitle_burned ? '已加入' : '未加入')
    add('背景音乐', content.background_music_applied ? '已加入' : '未加入')
    add('Logo', content.logo_applied ? '已加入' : '未加入')
  }
  return facts
})
const imagePreviewOpen = ref(false)
const previewRetryAvailable = ref(false)
let previewFallbackTimer: ReturnType<typeof setTimeout> | undefined

function clearPreviewFallbackTimer() {
  if (previewFallbackTimer !== undefined) {
    clearTimeout(previewFallbackTimer)
    previewFallbackTimer = undefined
  }
}

function requestImagePreview() {
  if (props.artifact.kind !== 'image' || props.mediaUrl) {
    return
  }
  previewRetryAvailable.value = false
  emit('preview', props.artifact)
  clearPreviewFallbackTimer()
  previewFallbackTimer = setTimeout(() => {
    previewRetryAvailable.value = !props.mediaUrl
  }, 8000)
}

function openImagePreview() {
  if (props.artifact.kind === 'image' && props.mediaUrl) {
    imagePreviewOpen.value = true
  }
}

watch(
  () => props.mediaUrl,
  (value) => {
    if (!value) {
      return
    }
    previewRetryAvailable.value = false
    clearPreviewFallbackTimer()
  },
)

onMounted(requestImagePreview)
onBeforeUnmount(clearPreviewFallbackTimer)

function canPreviewMedia(kind: string) {
  return ['image', 'voice', 'ai_video_clip', 'video'].includes(kind)
}

function topicTitle(candidate: Record<string, unknown>) {
  return String(candidate.title || candidate.text || '未填写标题')
}

function copyBody(candidate: Record<string, unknown>) {
  return String(candidate.body || candidate.text || '').trim()
}
</script>

<template>
  <article class="content-artifact-card" :class="{ 'is-selected': selected }">
    <header>
      <div class="artifact-heading">
        <ElCheckbox
          :model-value="selected"
          :disabled="selectionDisabled"
          :aria-label="`选择${displayLabel(artifact.kind, artifactKindLabels, '其他内容')}版本 ${artifact.version}`"
          @change="emit('select', artifact)"
        />
        <div>
          <strong>{{ displayLabel(artifact.kind, artifactKindLabels, '其他内容') }} · 版本 {{ artifact.version }}</strong>
          <span>{{ formatDate(artifact.created_at) }}</span>
        </div>
      </div>
      <ElTag :type="statusType" effect="light">
        {{ effectiveStatusLabel }}
      </ElTag>
    </header>

    <div v-if="canPreviewMedia(artifact.kind)" class="artifact-media">
      <template v-if="mediaUrl">
        <button
          v-if="artifact.kind === 'image'"
          type="button"
          class="image-preview-trigger"
          :aria-label="`打开图片版本 ${artifact.version} 的大图预览`"
          title="双击查看大图"
          @dblclick="openImagePreview"
          @keydown.enter="openImagePreview"
          @keydown.space.prevent="openImagePreview"
        >
          <img :src="mediaUrl" :alt="`生成图片版本 ${artifact.version} 预览`" draggable="false">
          <span class="image-preview-hint" aria-hidden="true">双击查看大图</span>
        </button>
        <audio
          v-else-if="artifact.kind === 'voice'"
          :src="mediaUrl"
          controls
          preload="metadata"
        />
        <video
          v-else
          :src="mediaUrl"
          controls
          preload="metadata"
        />
      </template>
      <div
        v-else-if="artifact.kind === 'image'"
        class="image-loading-state"
        role="status"
        aria-live="polite"
      >
        <span class="image-loading-indicator" aria-hidden="true" />
        <span>{{ previewRetryAvailable ? '图片暂未加载完成' : '图片加载中…' }}</span>
        <ElButton v-if="previewRetryAvailable" size="small" @click="requestImagePreview">
          重新加载
        </ElButton>
      </div>
      <ElEmpty v-else description="点击预览加载媒体" :image-size="140">
        <ElButton @click="emit('preview', artifact)">加载预览</ElButton>
      </ElEmpty>
    </div>

    <dl v-if="mediaFacts.length" class="media-facts">
      <div v-for="fact in mediaFacts" :key="fact.label">
        <dt>{{ fact.label }}</dt>
        <dd>{{ fact.value }}</dd>
      </div>
    </dl>

    <div v-else-if="candidates.length" class="candidate-list">
      <section
        v-for="(candidate, index) in candidates"
        :key="index"
        class="candidate-item"
        :class="{ 'is-chosen': selectedCandidate === index }"
      >
        <div class="candidate-index">候选 {{ index + 1 }}</div>
        <button
          v-if="artifact.kind === 'topic'"
          type="button"
          class="topic-title-button"
          @click="emit('edit', artifact)"
        >
          <strong>{{ topicTitle(candidate) }}</strong>
        </button>
        <div v-else-if="artifact.kind === 'copy'" class="copy-candidate-content">
          <strong>{{ fixedTitle || '请先确认标题' }}</strong>
          <pre>{{ copyBody(candidate) || '未填写文案正文' }}</pre>
        </div>
        <pre v-else>{{ candidateText(candidate, artifact.kind) }}</pre>
        <ElButton
          v-if="
            ['topic', 'copy'].includes(artifact.kind)
              || effectiveStatus !== 'confirmed'
              || artifact.linked
          "
          size="small"
          :type="selectedCandidate === index ? 'primary' : ''"
          :plain="selectedCandidate !== index"
          @click="emit('chooseCandidate', artifact, index)"
        >
          {{
            selectedCandidate === index
              ? '已选此候选'
              : effectiveStatus === 'confirmed'
                ? '改用此候选'
                : '使用此候选'
          }}
        </ElButton>
      </section>
    </div>

    <div
      v-else-if="artifact.kind === 'copy'"
      class="copy-candidate-content artifact-text"
    >
      <strong>{{ fixedTitle || '请先确认标题' }}</strong>
      <pre>{{ String(artifact.content.body || artifact.content.text || '').trim() || '未填写文案正文' }}</pre>
    </div>
    <pre v-else-if="!canPreviewMedia(artifact.kind)" class="artifact-text">{{ artifactText(artifact) }}</pre>

    <div
      v-if="complianceSummary"
      class="compliance-summary"
      :class="{ 'has-hits': complianceSummary.hits.length > 0 }"
      role="status"
    >
      <strong>{{ complianceSummary.hits.length ? `检测到 ${complianceSummary.hits.length} 项提示` : '未命中已维护规则' }}</strong>
      <ContentComplianceHits v-if="complianceSummary.hits.length" :hits="complianceSummary.hits" />
    </div>

    <footer class="artifact-card-actions">
      <ElButton
        v-if="artifact.kind === 'copy'"
        link
        type="warning"
        @click="emit('compliance', artifact)"
      >
        合规检测
      </ElButton>
      <ElButton link type="primary" @click="emit('edit', artifact)">
        {{ artifact.kind === 'image' ? '查看/再生成' : isArtifactContentEditable(artifact.kind) ? '查看/修改' : '查看详情' }}
      </ElButton>
      <ElButton link @click="emit('download', artifact)">下载文件</ElButton>
      <ElButton
        v-if="canRemove"
        link
        type="danger"
        @click="emit('remove', artifact)"
      >
        删除
      </ElButton>
      <ElTooltip v-else :content="removeDisabledReason" placement="top">
        <span class="disabled-action">
          <ElButton link type="danger" disabled>删除</ElButton>
        </span>
      </ElTooltip>
    </footer>
  </article>

  <ElDialog
    v-model="imagePreviewOpen"
    :title="`图片 · 版本 ${artifact.version}`"
    width="min(1120px, 94vw)"
    destroy-on-close
    append-to-body
    align-center
  >
    <div class="image-preview-dialog-body">
      <img
        v-if="mediaUrl"
        :src="mediaUrl"
        :alt="`生成图片版本 ${artifact.version} 大图预览`"
      >
    </div>
    <template #footer>
      <ElButton @click="imagePreviewOpen = false">关闭</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.content-artifact-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  overflow: hidden;
  color: var(--xi-semantic-color-text-primary);
  background: var(--xi-component-card-background);
  border: var(--xi-component-content-border);
  border-radius: var(--xi-component-content-radius-card);
}

.content-artifact-card.is-selected {
  border: var(--xi-component-content-border-brand);
}

.disabled-action {
  display: inline-flex;
}

.media-facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: var(--xi-component-content-gap-xs) var(--xi-component-content-gap-md);
  padding: 0 var(--xi-component-content-gap-md) var(--xi-component-content-gap-md);
  margin: 0;
}

.media-facts > div {
  display: grid;
  gap: var(--xi-component-content-gap-xxs);
}

.media-facts dt {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.media-facts dd {
  margin: 0;
  color: var(--xi-semantic-color-text-primary);
}

header,
footer,
.artifact-heading,
.compliance-summary {
  display: flex;
  align-items: center;
}

header {
  justify-content: space-between;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-md);
  border-bottom: var(--xi-component-content-border);
}

.artifact-heading {
  min-width: 0;
  gap: var(--xi-component-content-gap-sm);
}

.artifact-heading > div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: var(--xi-component-content-gap-xxs);
}

.artifact-heading strong {
  overflow: hidden;
  font-size: var(--xi-semantic-typography-body-font-size);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-heading span,
.candidate-index {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.artifact-media {
  display: grid;
  min-height: var(--xi-component-content-preview-height);
  place-items: center;
  background: var(--xi-semantic-color-background-page);
}

.artifact-media :is(img, video),
.image-preview-trigger {
  display: block;
  width: 100%;
  height: var(--xi-component-content-preview-height);
}

.artifact-media :is(img, video) {
  object-fit: contain;
}

.image-preview-trigger {
  position: relative;
  padding: 0;
  overflow: hidden;
  font: inherit;
  cursor: zoom-in;
  background: transparent;
  border: 0;
}

.image-preview-trigger:focus-visible {
  outline: var(--xi-component-content-focus-width) solid var(--xi-semantic-color-border-brand-default);
  outline-offset: calc(-1 * var(--xi-component-content-focus-width));
}

.image-preview-hint {
  position: absolute;
  right: var(--xi-component-content-gap-sm);
  bottom: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-xxs) var(--xi-component-content-gap-xs);
  color: var(--xi-semantic-color-text-inverse);
  font-size: var(--xi-semantic-typography-caption-font-size);
  background: var(--xi-semantic-color-background-inverse);
  border-radius: var(--xi-component-content-radius-control);
  opacity: 0.82;
}

.image-loading-state {
  display: grid;
  justify-items: center;
  gap: var(--xi-component-content-gap-sm);
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-body-font-size);
}

.image-loading-indicator {
  width: var(--xi-component-content-control-height);
  height: var(--xi-component-content-control-height);
  border: var(--xi-component-content-focus-width) solid var(--xi-semantic-color-border-default);
  border-top-color: var(--xi-semantic-color-border-brand-default);
  border-radius: 50%;
  animation: image-preview-loading 0.8s linear infinite;
}

.image-preview-dialog-body {
  display: grid;
  min-height: 240px;
  max-height: 76vh;
  overflow: auto;
  background: var(--xi-semantic-color-background-page);
  place-items: center;
}

.image-preview-dialog-body img {
  display: block;
  max-width: 100%;
  max-height: 76vh;
  object-fit: contain;
}

@keyframes image-preview-loading {
  to {
    transform: rotate(360deg);
  }
}

.artifact-media audio {
  width: calc(100% - var(--xi-component-content-gap-xl));
}

.candidate-list {
  display: grid;
  gap: var(--xi-component-content-gap-sm);
  padding: var(--xi-component-content-gap-md);
}

.candidate-item {
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-page);
  border-left: var(--xi-component-content-focus-width) solid var(--xi-semantic-color-border-default);
}

.candidate-item.is-chosen {
  background: var(--xi-semantic-color-background-brand-subtle);
  border-left-color: var(--xi-semantic-color-border-brand-default);
}

.candidate-item pre,
.artifact-text {
  margin: var(--xi-component-content-gap-xs) 0;
  overflow-wrap: anywhere;
  color: var(--xi-semantic-color-text-secondary);
  font: inherit;
  font-size: var(--xi-semantic-typography-body-font-size);
  white-space: pre-wrap;
}

.copy-candidate-content {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-xs);
}

.copy-candidate-content > strong {
  overflow-wrap: anywhere;
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading3-font-size);
}

.copy-candidate-content > pre {
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--xi-semantic-color-text-secondary);
  font: inherit;
  white-space: pre-wrap;
}

.topic-title-button {
  display: grid;
  width: 100%;
  gap: var(--xi-component-content-gap-xxs);
  padding: var(--xi-component-content-gap-xs) 0;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.topic-title-button strong {
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-heading3-font-size);
}

.topic-title-button:hover strong,
.topic-title-button:focus-visible strong {
  color: var(--xi-semantic-color-text-brand);
}

.topic-title-button:focus-visible {
  border-radius: var(--xi-component-content-radius-control);
  outline: var(--xi-component-content-focus-width) solid var(--xi-semantic-color-border-brand-default);
  outline-offset: var(--xi-component-content-focus-offset);
}

.artifact-text {
  max-height: var(--xi-component-content-editor-min-height);
  padding: var(--xi-component-content-gap-md);
  overflow: auto;
}

.compliance-summary {
  align-items: flex-start;
  flex-direction: column;
  gap: var(--xi-component-content-gap-xxs);
  padding: var(--xi-component-content-gap-sm) var(--xi-component-content-gap-md);
  color: var(--xi-semantic-color-feedback-success-text);
  background: var(--xi-semantic-color-feedback-success-background-subtle);
}

.compliance-summary.has-hits {
  color: var(--xi-semantic-color-feedback-warning-text);
  background: var(--xi-semantic-color-feedback-warning-background-subtle);
}

.compliance-summary span,
.compliance-summary small {
  font-size: var(--xi-semantic-typography-caption-font-size);
}

footer,
.artifact-card-actions {
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: var(--xi-component-content-gap-xxs);
  padding: var(--xi-component-content-gap-xs) var(--xi-component-content-gap-sm);
  margin-top: auto;
  min-width: 0;
  border-top: var(--xi-component-content-border);
}

.artifact-card-actions :deep(.el-button),
.artifact-card-actions :deep(.el-button + .el-button) {
  flex: none;
  height: var(--el-component-size-small);
  margin-left: 0;
  font-size: var(--xi-semantic-typography-caption-font-size);
}
</style>
