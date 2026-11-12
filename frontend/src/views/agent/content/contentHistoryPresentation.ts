/* eslint-disable financial/no-division */

import type { Artifact, ArtifactContent, ComplianceHit } from '@/api/agent/content'
import {
  artifactKindLabels,
  complianceHitPresentation,
  modelLabel,
  providerLabel,
  transitionLabels,
  voiceLabels,
} from './contentWorkflow'

export interface HistoryRecordField {
  label: string
  value: string
}

export interface HistoryRecordSection {
  title: string
  fields: HistoryRecordField[]
}

export type HistoryMediaKind = 'image' | 'audio' | 'video'

const historyKindLabels: Record<string, string> = {
  ...artifactKindLabels,
  audio: '音频素材',
  music: '背景音乐',
  sfx: '音效',
  source_audio: '原始音频',
}

const roleLabels: Record<string, string> = {
  audio: '音频素材',
  music: '背景音乐',
  sfx: '音效',
  subtitle: '字幕',
  video: '视频成片',
  voice: '配音',
}

const genericFieldLabels: Record<string, string> = {
  direction: '内容方向',
  body: '正文',
  format: '文件格式',
  instruction: '生成要求',
  media_type: '媒体类型',
  prompt: '生成提示词',
  summary: '摘要',
  text: '文字内容',
  title: '标题',
}

function recordValue(value: unknown): string {
  if (typeof value === 'string') {
    return value.trim()
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return ''
}

function recordObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function firstValue(...values: unknown[]) {
  return values.map(recordValue).find(Boolean) || ''
}

function addField(fields: HistoryRecordField[], label: string, value: unknown) {
  const normalized = recordValue(value)
  if (normalized) {
    fields.push({ label, value: normalized })
  }
}

function addNumberField(
  fields: HistoryRecordField[],
  label: string,
  value: unknown,
  suffix = '',
) {
  if (value === undefined || value === null || value === '') {
    return
  }
  const number = Number(value)
  if (Number.isFinite(number)) {
    const formatted = Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/u, '').replace(/\.$/u, '')
    addField(fields, label, `${formatted}${suffix}`)
  }
}

function formatBytes(value: unknown) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) {
    return ''
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1).replace(/\.0$/u, '')} KB`
  }
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / 1024 / 1024).toFixed(1).replace(/\.0$/u, '')} MB`
  }
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1).replace(/\.0$/u, '')} GB`
}

function formatTimestamp(value: unknown) {
  const seconds = Number(value)
  if (!Number.isFinite(seconds) || seconds < 0) {
    return ''
  }
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds - minutes * 60
  return `${String(minutes).padStart(2, '0')}:${remainder.toFixed(2).padStart(5, '0')}`
}

function mediaFileFields(artifact: Artifact, content: ArtifactContent) {
  const fields: HistoryRecordField[] = []
  const filename = firstValue(
    content.filename,
    content.file_name,
    content.name,
    artifact.storage_key?.split(/[\\/]/u).pop(),
  )
  addField(fields, '文件名称', filename)
  addField(fields, '文件格式', firstValue(content.format, content.media_type))
  addField(fields, '文件大小', formatBytes(content.size_bytes))
  addNumberField(fields, '文件时长', content.duration_seconds ?? content.duration, ' 秒')
  return fields
}

function generationFields(content: ArtifactContent, usedKeys: Set<string>) {
  const fields: HistoryRecordField[] = []
  const generation = recordObject(content.generation)
  const provenance = recordObject(content.provenance)
  const request = recordObject(provenance.request)
  const instruction = firstValue(content.instruction, request.instruction)
  const prompt = firstValue(content.prompt, request.prompt)
  if (!usedKeys.has('instruction')) {
    addField(fields, '生成要求', instruction)
  }
  if (!usedKeys.has('prompt')) {
    addField(fields, '生成提示词', prompt)
  }
  const provider = firstValue(
    generation.provider_id,
    provenance.provider_id,
    content.provider,
  )
  const model = firstValue(generation.model, provenance.model, content.model)
  if (provider) {
    addField(fields, '生成服务', providerLabel(provider))
  }
  if (model) {
    addField(fields, '模型', modelLabel(model))
  }
  return fields
}

function titleSections(content: ArtifactContent) {
  const sections: HistoryRecordSection[] = []
  const candidates = Array.isArray(content.candidates) ? content.candidates : []
  if (candidates.length) {
    const fields: HistoryRecordField[] = []
    candidates.forEach((candidate, index) => {
      const item = recordObject(candidate)
      addField(
        fields,
        `候选标题 ${index + 1}`,
        firstValue(item.title, item.text, candidate),
      )
      addField(fields, `候选 ${index + 1} 说明`, item.summary)
      addField(fields, `候选 ${index + 1} 创意方向`, item.direction)
    })
    if (fields.length) {
      sections.push({ title: '标题候选', fields })
    }
  }
  else {
    const fields: HistoryRecordField[] = []
    addField(fields, '标题', firstValue(content.title, content.text))
    addField(fields, '标题说明', content.summary)
    addField(fields, '补充说明', content.body)
    if (fields.length) {
      sections.push({ title: '标题内容', fields })
    }
  }
  return sections
}

function copySections(content: ArtifactContent) {
  const sections: HistoryRecordSection[] = []
  const candidates = Array.isArray(content.candidates) ? content.candidates : []
  if (candidates.length) {
    const fields: HistoryRecordField[] = []
    candidates.forEach((candidate, index) => {
      const item = recordObject(candidate)
      addField(fields, `候选 ${index + 1} 标题`, firstValue(item.title, candidate))
      addField(fields, `候选 ${index + 1} 摘要`, item.summary)
      addField(
        fields,
        `候选 ${index + 1} 正文`,
        firstValue(item.body, item.text, candidate),
      )
      addField(fields, `候选 ${index + 1} 内容方向`, item.direction)
    })
    if (fields.length) {
      sections.push({ title: '文案候选', fields })
    }
  }
  else {
    const fields: HistoryRecordField[] = []
    addField(fields, '文案标题', content.title)
    addField(fields, '文案摘要', content.summary)
    addField(fields, '文案正文', firstValue(content.body, content.text))
    if (fields.length) {
      sections.push({ title: '文案内容', fields })
    }
  }
  return sections
}

function storyboardSections(content: ArtifactContent) {
  const fields: HistoryRecordField[] = []
  const shots = Array.isArray(content.shots) ? content.shots : []
  shots.forEach((shot, index) => {
    const item = recordObject(shot)
    const lines = [
      `画面：${firstValue(item.visual) || '未填写'}`,
      `旁白：${firstValue(item.narration) || '未填写'}`,
      `字幕：${firstValue(item.subtitle) || '未填写'}`,
      `时长：${firstValue(item.duration_seconds, item.duration) || '0'} 秒`,
      `转场：${transitionLabels[firstValue(item.transition)] ?? '自定义转场'}`,
    ]
    addField(fields, `镜头 ${firstValue(item.order) || index + 1}`, lines.join('\n'))
  })
  return fields.length ? [{ title: '分镜脚本', fields }] : []
}

function subtitleSections(content: ArtifactContent) {
  const fields: HistoryRecordField[] = []
  const segments = Array.isArray(content.segments) ? content.segments : []
  segments.forEach((segment, index) => {
    const item = recordObject(segment)
    const start = formatTimestamp(item.start_seconds)
    const end = formatTimestamp(item.end_seconds)
    const timing = start && end ? `（${start} - ${end}）` : ''
    addField(fields, `字幕 ${firstValue(item.index) || index + 1}${timing}`, item.text)
  })
  if (!fields.length) {
    addField(fields, '字幕文字', content.text)
  }
  return fields.length ? [{ title: '字幕文字', fields }] : []
}

function complianceSections(content: ArtifactContent) {
  const summary = recordObject(content.compliance)
  const rawHits = Array.isArray(summary.hits) ? summary.hits : Array.isArray(content.hits) ? content.hits : []
  const fields: HistoryRecordField[] = []
  if (rawHits.length) {
    rawHits.forEach((hit, index) => {
      const presented = complianceHitPresentation(hit as ComplianceHit)
      const snippet = `${presented.prefix}${presented.match ? `【${presented.match}】` : ''}${presented.suffix}`.trim()
      const lines = [
        `${presented.typeLabel}：${presented.match || '未记录'}`,
        `位置：${presented.fieldLabel}`,
        `原文片段：${snippet || '未记录'}`,
      ]
      if (presented.replacement) {
        lines.push(`建议替换为：${presented.replacement}`)
      }
      addField(fields, `命中 ${index + 1}`, lines.join('\n'))
    })
  }
  else {
    addField(fields, '检测结论', '检测完成，未发现需要提示的关键词、敏感词或禁用词。')
  }
  return [{ title: '合规检测内容', fields }]
}

function mediaSections(artifact: Artifact, content: ArtifactContent) {
  const fields: HistoryRecordField[] = []
  const kind = artifact.kind
  if (kind === 'voice') {
    addField(fields, '配音文字', content.text)
    const voice = firstValue(content.voice)
    addField(fields, '音色', voiceLabels[voice] ?? voice)
    addNumberField(fields, '语速', content.speed)
    addNumberField(fields, '音量', content.volume)
  }
  else if (kind === 'music' || kind === 'sfx') {
    addField(fields, kind === 'music' ? '背景音乐提示词' : '音效提示词', content.prompt)
    addField(fields, '音乐类型', content.instrumental === true ? '纯音乐' : content.instrumental === false ? '含人声' : '')
    addNumberField(fields, '音频时长', content.duration_seconds ?? content.duration, ' 秒')
  }
  else if (kind === 'image') {
    addField(fields, '图片生成提示词', content.prompt)
    addField(fields, '图片比例', content.ratio)
    addField(fields, '图片尺寸', content.size)
    if (content.watermark !== undefined) {
      addField(fields, '水印', content.watermark ? '有水印' : '无水印')
    }
  }
  else if (kind === 'ai_video_clip') {
    addField(fields, '视频镜头提示词', content.prompt)
    if (content.shot_index !== undefined) {
      addField(fields, '对应镜头', `镜头 ${Number(content.shot_index) + 1}`)
    }
    addNumberField(fields, '视频时长', content.duration_seconds, ' 秒')
    addField(fields, '画面比例', content.ratio)
    addField(fields, '清晰度', content.resolution)
    if (content.generate_audio !== undefined) {
      addField(fields, '生成声音', content.generate_audio ? '是' : '否')
    }
  }
  else if (kind === 'video') {
    addField(fields, '成片尺寸', content.width && content.height ? `${content.width} × ${content.height}` : '')
    addNumberField(fields, '成片时长', content.duration_seconds, ' 秒')
    addNumberField(fields, '镜头数量', content.clip_count, ' 个')
    addField(fields, '主配音', content.voice_applied ? '已加入' : content.voice_applied === false ? '未加入' : '')
    addField(fields, '背景音乐', content.background_music_applied ? '已加入' : content.background_music_applied === false ? '未加入' : '')
    addField(fields, '字幕', content.subtitle_burned ? '已加入' : content.subtitle_burned === false ? '未加入' : '')
  }
  else if (kind === 'audio') {
    addField(fields, '音频说明', firstValue(content.text, content.prompt))
  }
  if (!fields.length) {
    return []
  }
  const label = kind === 'voice'
    ? '配音内容'
    : kind === 'music'
      ? '背景音乐内容'
      : kind === 'sfx'
        ? '音效内容'
        : kind === 'image'
          ? '图片内容'
          : kind === 'ai_video_clip'
            ? 'AI 镜头视频内容'
            : kind === 'video'
              ? '视频成片内容'
              : '媒体内容'
  return [{ title: label, fields }]
}

function genericSections(content: ArtifactContent) {
  const fields: HistoryRecordField[] = []
  for (const [key, label] of Object.entries(genericFieldLabels)) {
    addField(fields, label, content[key])
  }
  return fields.length ? [{ title: '内容说明', fields }] : []
}

function appendGenerationSection(
  sections: HistoryRecordSection[],
  content: ArtifactContent,
) {
  const usedKeys = new Set<string>()
  for (const field of sections.flatMap(section => section.fields)) {
    if (field.label === '生成要求') {
      usedKeys.add('instruction')
    }
    if (field.label.includes('提示词')) {
      usedKeys.add('prompt')
    }
  }
  const fields = generationFields(content, usedKeys)
  if (fields.length) {
    sections.push({ title: '生成信息', fields })
  }
}

export function historyArtifactKindLabel(artifact: Pick<Artifact, 'kind' | 'content'>) {
  const direct = historyKindLabels[artifact.kind]
  if (direct) {
    return direct
  }
  const content = artifact.content
  const role = firstValue(content.role, content.track_kind, content.media_role)
  if (roleLabels[role]) {
    return roleLabels[role]
  }
  const filename = firstValue(content.filename, content.file_name, content.name)
  if (/背景音乐|bgm|music/iu.test(filename)) {
    return '背景音乐'
  }
  if (/配音|voice/iu.test(filename)) {
    return '配音'
  }
  if (/字幕|subtitle|\.srt\b|\.vtt\b/iu.test(filename)) {
    return '字幕'
  }
  if (/[\u3400-\u9FFF]/u.test(artifact.kind)) {
    return artifact.kind
  }
  return '未标记内容'
}

export function historyArtifactMediaKind(
  artifact: Pick<Artifact, 'kind' | 'content'>,
): HistoryMediaKind | null {
  if (['topic', 'copy', 'storyboard', 'subtitle', 'compliance'].includes(artifact.kind)) {
    return null
  }
  const mediaType = firstValue(artifact.content.media_type, artifact.content.mime_type).toLowerCase()
  const role = firstValue(
    artifact.content.role,
    artifact.content.track_kind,
    artifact.content.media_role,
  )
  if (artifact.kind === 'image' || mediaType.startsWith('image/')) {
    return 'image'
  }
  if (
    ['voice', 'music', 'sfx', 'audio'].includes(artifact.kind)
    || ['voice', 'music', 'sfx', 'audio'].includes(role)
    || mediaType.startsWith('audio/')
  ) {
    return 'audio'
  }
  if (
    ['ai_video_clip', 'video'].includes(artifact.kind)
    || ['video', 'clip'].includes(role)
    || mediaType.startsWith('video/')
  ) {
    return 'video'
  }
  return null
}

export function historyArtifactSections(artifact: Artifact): HistoryRecordSection[] {
  const content = artifact.content
  let sections: HistoryRecordSection[] = []
  if (artifact.kind === 'topic') {
    sections = titleSections(content)
  }
  else if (artifact.kind === 'copy') {
    sections = copySections(content)
  }
  else if (artifact.kind === 'storyboard') {
    sections = storyboardSections(content)
  }
  else if (artifact.kind === 'subtitle') {
    sections = subtitleSections(content)
  }
  else if (artifact.kind === 'compliance') {
    sections = complianceSections(content)
  }
  else {
    sections = mediaSections(artifact, content)
  }
  if (!sections.length) {
    sections = genericSections(content)
  }
  appendGenerationSection(sections, content)
  if (historyArtifactMediaKind(artifact) && (artifact.storage_key || content.media_type || content.size_bytes)) {
    const fileFields = mediaFileFields(artifact, content)
    if (fileFields.length) {
      sections.push({ title: '文件信息', fields: fileFields })
    }
  }
  if (!sections.length) {
    sections.push({
      title: historyArtifactKindLabel(artifact),
      fields: [{ label: '内容说明', value: '该版本已保存，可通过“导出”下载完整文件。' }],
    })
  }
  return sections
}

export function historyArtifactSummary(artifact: Artifact) {
  const fields = historyArtifactSections(artifact)
    .flatMap(section => section.fields)
    .filter(field => field.value)
  if (!fields.length) {
    return `${historyArtifactKindLabel(artifact)}文件`
  }
  const summary = fields
    .slice(0, 2)
    .map(field => `${field.label}：${field.value.replace(/\s+/gu, ' ')}`)
    .join(' · ')
  return summary.length > 180 ? `${summary.slice(0, 177)}…` : summary
}
