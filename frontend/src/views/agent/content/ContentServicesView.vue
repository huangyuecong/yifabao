<script setup lang="ts">
import type {
  ContentCapabilities,
  ContentProviderStatus,
  ContentServiceStatus,
} from '@/api/agent/content'
import { Connection, Cpu, Document, Headset, Microphone, Picture, VideoCamera } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { contentApi } from '@/api/agent/content'
import ContentChatModels from './ContentChatModels.vue'
import ContentModelSettings from './ContentModelSettings.vue'
import {
  capabilityLabels,
  formatDate,
  friendlyContentError,
  modelLabel,
  parameterOptionLabel,
  serviceModeLabels,
} from './contentWorkflow'
import { providerPresentation } from './providerPresentation'
import { serviceStatusPresentation } from './serviceStatusPresentation'

defineOptions({ name: 'ContentServicesView' })

const loading = ref(false)
const capabilities = ref<ContentCapabilities>({ capabilities: [] })
const serviceStatus = ref<ContentServiceStatus>({ overall: 'available', services: [] })
const providers = ref<ContentProviderStatus[]>([])
const loadError = ref('')
const modelSettings = ref<InstanceType<typeof ContentModelSettings>>()
const modelCapabilities = [
  { key: 'text', providerCapability: 'text-generation', label: '文案与图片理解', description: '分类图片、生成商品文案与宣传内容' },
  { key: 'image', providerCapability: 'image-generation', label: '图片生成', description: '创作图片、生成商品展示图' },
  { key: 'video', providerCapability: 'ai-video-generation', label: '视频生成', description: '生成视频镜头与宣传片段' },
  { key: 'speech', providerCapability: 'speech-synthesis', label: '配音', description: '将文案转换为语音' },
  { key: 'music', providerCapability: 'music-generation', label: '背景音乐', description: '为视频生成背景音乐' },
]
const parameterDrawerOpen = ref(false)
const selectedCapability = ref<ContentCapabilities['capabilities'][number]>()

function modelConfiguration(capability: string) {
  return modelCapabilities.find(item => item.providerCapability === capability)
}

function capabilityIcon(capability: string) {
  if (capability.includes('image')) {
    return Picture
  }
  if (capability.includes('video')) {
    return VideoCamera
  }
  if (capability.includes('speech') || capability.includes('voice')) {
    return Microphone
  }
  if (capability.includes('music')) {
    return Headset
  }
  if (capability.includes('text')) {
    return Document
  }
  return Cpu
}

function showParameters(row: ContentCapabilities['capabilities'][number]) {
  selectedCapability.value = row
  parameterDrawerOpen.value = true
}

function configureSelected() {
  const configuration = modelConfiguration(selectedCapability.value?.capability || '')
  if (!configuration) {
    return
  }
  parameterDrawerOpen.value = false
  void modelSettings.value?.show(configuration.key)
}

const parameterLabels: Record<string, string> = {
  aspect_ratios: '画面比例',
  audio_controls: '音频控制',
  audio_only_api: '纯音频接口',
  audio_tracks: '音频轨道',
  billing_basis: '计费依据',
  control: '控制方式',
  custom_ratio: '自定义比例',
  duration_seconds: '时长（秒）',
  formal_render: '正式渲染',
  instrumental_only: '纯音乐',
  local_file_upload: '本地文件上传',
  logo_intervals: 'Logo 显示区间',
  max_bytes_per_file: '单文件字节上限',
  maximum_duration_seconds: '最长时长（秒）',
  overwrite_requires_opt_in: '覆盖前需确认',
  per_clip_volume: '片段独立音量',
  policies: '处理策略',
  private_temporary_object: '私有临时对象',
  profiles: '输出预设',
  project_asset_transport: '工程素材传输方式',
  project_asset_transport_available: '工程素材传输可用',
  project_asset_transports: '支持的素材传输方式',
  project_asset_url_ttl_seconds: '素材地址有效期（秒）',
  provider_minimum: '接口最小值',
  provider_native_reference_supported: '接口原生参考素材',
  public_paths: '公开文件路径',
  ratio: '画面比例',
  raw_filters: '自定义滤镜',
  reference_videos: '参考视频',
  returns_bytes: '返回文件内容',
  seed: '随机种子',
  size: '尺寸',
  sound_effects: '音效',
  source_audio: '源素材声音',
  split_model: '分段模型',
  temporary_urls_persisted_immediately: '立即保存临时素材',
  total_duration_seconds: '总时长（秒）',
  transports: '传输方式',
  true_xfade: '交叠转场',
  operations: '支持的创作操作',
  structured_output: '结构化输出',
  max_output_tokens: '最大输出长度',
  temperature: '生成随机性',
  primary_model: '主模型',
  fallback_model: '备用模型',
  failover_policy: '备用切换方式',
  configured: '当前值',
  min: '最小值',
  max: '最大值',
  minimum: '最小值',
  maximum: '最大值',
  default: '默认值',
  supported: '支持的选项',
  keep_supported: '保留源声音',
  parameter_source: '参数来源',
  count: '单次候选数量',
  sizes: '图片尺寸',
  ratios: '画面比例',
  reference_images: '参考图',
  watermark: '水印',
  voices: '可用音色',
  formats: '输出格式',
  speed: '语速范围',
  volume: '音量范围',
  timestamps: '字幕时间精度',
  timestamp_languages: '时间戳语言',
  default_max_chars_per_line: '字幕每行最多字数',
  default_max_lines: '字幕最多行数',
  timing_sources: '字幕时间来源',
  async: '异步生成',
  text_to_video: '文字生成视频',
  image_to_video: '图片生成视频',
  durations: '视频时长',
  resolutions: '视频分辨率',
  generate_audio: '生成视频声音',
  return_last_frame: '返回尾帧',
  transitions: '可用转场',
  transition_duration_seconds: '转场时长',
  fit_modes: '画面适配方式',
  source_audio_policy: '源视频声音处理',
  subtitles: '字幕处理',
  subtitle_style: '字幕样式',
  font_size: '字幕字号',
  colors: '颜色格式',
  alignment: '字幕位置',
  voice: '配音合成',
  background_music: '背景音乐',
  background_music_volume: '背景音乐音量',
  logo: '品牌 Logo',
  logo_positions: 'Logo 位置',
  intro_outro: '片头片尾',
  custom_size: '自定义画布',
  defaults: '默认成片参数',
  width: '宽度',
  height: '高度',
  fps: '帧率',
  default_source: '默认值来源',
  stage_operations: '各阶段操作',
  voice_subtitles: '配音字幕阶段',
  video_preview: '视频预览阶段',
}
const valueLabels: Record<string, string> = {
  'topics': '生成选题',
  'copies': '生成文案',
  'transform_copy': '调整文案',
  'storyboard': '生成分镜脚本',
  'voice': '生成配音',
  'subtitle': '生成字幕',
  'clips': '生成镜头片段',
  'compose': '合成成片',
  'provider': '配音服务返回的时间戳',
  'estimated': '系统估算',
  'provider-word-level': '配音服务字级时间戳',
  'per-request': '单次请求失败时尝试备用模型',
  'burn-in-srt': '将 SRT 字幕嵌入画面',
  'none': '无转场',
  'cut': '直接切换',
  'fade': '淡入淡出',
  'fadeblack': '黑场淡化',
  'fadewhite': '白场淡化',
  'zh': '中文',
  'en': '英文',
  'service-configuration': '内容服务配置',
  'provider-contract-and-service-configuration': '配音服务协议与内容服务配置',
  'content-service': '内容服务',
  'contain': '完整显示（可能留边）',
  'cover': '铺满画面（可能裁切）',
  'mute': '移除原视频声音',
  'replace_by_voice': '由主配音替换原声音',
  'top_left': '左上',
  'top_right': '右上',
  'bottom_left': '左下',
  'bottom_right': '右下',
}

interface ParameterItem {
  key: string
  label: string
  value: string
}

const overallPresentation = computed(() => serviceStatusPresentation(serviceStatus.value?.overall))

const serviceRows = computed(() => serviceStatus.value?.services ?? [])

function serviceProvider(row: { capability: string, provider_id: string }) {
  return providers.value.find(item => item.capability === row.capability && item.provider_id === row.provider_id)
}

function serviceInfo(row: { capability: string, provider_id: string }) {
  return providerPresentation(row, providers.value)
}

function messageOf(error: unknown) {
  return friendlyContentError(error, '服务状态读取失败，请确认内容服务已启动并可访问')
}

function modeLabel(value?: string | null) {
  return value ? serviceModeLabels[value] || '其他运行方式' : '未记录'
}

function scalarValue(value: unknown, key = ''): string {
  if (typeof value === 'boolean') {
    return value ? '支持' : '不支持'
  }
  if (value === null || value === undefined || value === '') {
    return '未设置'
  }
  if (typeof value === 'string') {
    if (key === 'primary_model' || key === 'fallback_model') {
      return value
    }
    const label = valueLabels[value] || parameterOptionLabel(key, value)
    return label.startsWith('其他') ? value : label
  }
  return String(value)
}

function rangeValue(value: Record<string, unknown>, key: string): string | null {
  const minimum = value.min ?? value.minimum
  const maximum = value.max ?? value.maximum
  if (minimum === undefined || maximum === undefined) {
    return null
  }
  const suffix = key === 'speed' || key === 'volume' ? ' 倍' : ''
  const configured = value.configured ?? value.default
  const configuredText = configured === undefined ? '' : `（默认 ${scalarValue(configured, key)}）`
  return `${scalarValue(minimum, key)}～${scalarValue(maximum, key)}${suffix}${configuredText}`
}

function objectValue(value: Record<string, unknown>, parentKey: string): string {
  const range = rangeValue(value, parentKey)
  if (range) {
    return range
  }
  if (parentKey === 'defaults' && value.width && value.height) {
    const fps = value.fps ? `，${scalarValue(value.fps, 'fps')} 帧/秒` : ''
    return `${scalarValue(value.width, 'width')}×${scalarValue(value.height, 'height')}${fps}`
  }
  return (
    Object.entries(value)
      .filter(([key]) => !/secret|token|password|endpoint|url|key/i.test(key))
      .map(
        ([key, child]) =>
          `${parameterLabels[key] || key}：${displayParameterValue(child, key)}`,
      )
      .join('；') || '未设置固定范围'
  )
}

function displayParameterValue(value: unknown, key: string): string {
  if (Array.isArray(value)) {
    return value.length
      ? value.map(item => displayParameterValue(item, key)).join('、')
      : '未设置固定范围'
  }
  if (value && typeof value === 'object') {
    return objectValue(value as Record<string, unknown>, key)
  }
  return scalarValue(value, key)
}

function parameterItems(parameters?: Record<string, unknown> | null): ParameterItem[] {
  const safeEntries = Object.entries(parameters ?? {}).filter(
    ([key]) => !/secret|token|password|endpoint|url|key/i.test(key),
  )
  if (!safeEntries.length) {
    return []
  }
  return safeEntries.map(([key, value]) => ({
    key,
    label: parameterLabels[key] || key,
    value: displayParameterValue(value, key),
  }))
}

function parameterPreview(parameters?: Record<string, unknown> | null) {
  const preferred = ['sizes', 'resolutions', 'formats', 'ratios', 'durations', 'reference_images', 'structured_output', 'text_to_video', 'image_to_video', 'defaults', 'voices', 'count']
  return parameterItems(parameters)
    .sort((left, right) => {
      const rank = (key: string) => preferred.includes(key) ? preferred.indexOf(key) : preferred.length
      return rank(left.key) - rank(right.key)
    })
    .slice(0, 3)
    .map(item => ({ ...item, shortValue: item.value.length > 28 ? `${item.value.slice(0, 28)}…` : item.value }))
}

async function load() {
  if (loading.value) {
    return
  }
  loading.value = true
  loadError.value = ''
  const results = await Promise.allSettled([
    contentApi.capabilities(),
    contentApi.serviceStatus(),
    contentApi.providers(),
  ])
  if (results[0].status === 'fulfilled') {
    capabilities.value = results[0].value
  }
  if (results[1].status === 'fulfilled') {
    serviceStatus.value = results[1].value
  }
  if (results[2].status === 'fulfilled') {
    providers.value = results[2].value
  }
  const failures = results.filter(result => result.status === 'rejected')
  if (failures.length) {
    loadError.value = failures
      .map(result => (result.status === 'rejected' ? messageOf(result.reason) : ''))
      .filter(Boolean)
      .join('；')
  }
  loading.value = false
}

let statusTimer: ReturnType<typeof setInterval> | undefined
let statusRefreshing = false
onMounted(() => {
  void load()
  statusTimer = setInterval(async () => {
    if (loading.value || statusRefreshing) {
      return
    }
    statusRefreshing = true
    try {
      serviceStatus.value = await contentApi.serviceStatus()
      if (loadError.value === '服务状态读取失败，请点击刷新服务信息重试') {
        loadError.value = ''
      }
    }
    catch {
      loadError.value = '服务状态读取失败，请点击刷新服务信息重试'
    }
    finally {
      statusRefreshing = false
    }
  }, 10000)
})
onBeforeUnmount(() => clearInterval(statusTimer))
</script>

<template>
  <div class="content-services-view" :aria-busy="loading">
    <section class="view-heading">
      <div>
        <div class="title-with-status">
          <h2>模型与服务</h2>
          <ElTag :type="loading ? 'info' : overallPresentation.type" :class="{ 'xi-status-invalid': overallPresentation.neutral }" effect="light">
            <span class="status-dot" aria-hidden="true" />
            {{ loading ? '正在检查' : overallPresentation.label }}
          </ElTag>
        </div>
        <p>为每一项创作能力，选择合适的 AI 模型。</p>
      </div>
      <div class="service-actions">
        <ElButton :loading="loading" @click="load">刷新服务信息</ElButton>
        <ContentChatModels @changed="load" />
        <ContentModelSettings ref="modelSettings" @changed="load" />
      </div>
    </section>

    <ElAlert
      v-if="loadError"
      type="error"
      title="部分服务状态无法读取"
      :description="loadError"
      :closable="false"
      show-icon
    />

    <section aria-labelledby="service-status-title">
      <div class="section-heading">
        <div><h3 id="service-status-title">AI 能力</h3><p>模型、连接状态与配置入口，集中管理。</p></div>
        <span>{{ serviceRows.length }} 项能力</span>
      </div>
      <div v-if="serviceRows.length" class="service-grid">
        <article
          v-for="service in serviceRows"
          :key="`${service.capability}:${service.provider_id}`"
          :class="{ 'configurable-service': modelConfiguration(service.capability) }"
        >
          <header>
            <span class="capability-icon" aria-hidden="true"><component :is="capabilityIcon(service.capability)" /></span>
            <h4>{{ capabilityLabels[service.capability] || '其他能力' }}</h4>
            <ElTag :type="serviceStatusPresentation(service.status).type" :class="{ 'xi-status-invalid': serviceStatusPresentation(service.status).neutral }">
              {{ serviceStatusPresentation(service.status).label }}
            </ElTag>
          </header>
          <p class="service-model" :title="serviceProvider(service)?.model || serviceInfo(service).name">{{ serviceProvider(service)?.model || serviceInfo(service).name }}</p>
          <p class="service-description">{{ modelConfiguration(service.capability)?.description || serviceInfo(service).name }}</p>
          <footer>
            <small :title="formatDate(service.checked_at)">{{ serviceProvider(service)?.external ? '云端服务' : '服务组件' }}</small>
            <ElButton
              v-if="modelSettings?.canManage && modelConfiguration(service.capability)"
              link
              type="primary"
              @click="modelSettings?.show(modelConfiguration(service.capability)?.key)"
            >
              配置模型 <span aria-hidden="true">↗</span>
            </ElButton>
            <span v-else class="component-note"><Connection aria-hidden="true" />{{ modeLabel(serviceProvider(service)?.mode) }}</span>
          </footer>
        </article>
      </div>
      <ElEmpty v-else description="服务状态接口未返回项目" />
    </section>

    <section class="capabilities-panel" aria-labelledby="capability-title">
      <div class="section-heading">
        <h3 id="capability-title">可用能力与参数</h3>
        <span>常用参数一目了然，更多细节按需展开</span>
      </div>
      <ElTable class="capability-table" :data="capabilities.capabilities" empty-text="能力接口未返回项目">
        <ElTableColumn label="能力" min-width="150">
          <template #default="scope">
            {{
              capabilityLabels[scope.row.capability] || '其他能力'
            }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="当前服务" min-width="170">
          <template #default="scope">
            <div class="technical-cell">
              <strong>{{ serviceInfo(scope.row).name }}</strong>
              <small>{{ serviceInfo(scope.row).model }}</small>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="接入方式" width="110">
          <template #default="scope">{{ modeLabel(scope.row.mode) }}</template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="100">
          <template #default="scope">
            <ElTag :type="scope.row.enabled ? 'success' : 'info'">
              {{
                scope.row.enabled ? '可用' : '未启用'
              }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="核心参数" min-width="300">
          <template #default="scope">
            <div v-if="parameterItems(scope.row.parameters).length" class="parameter-preview">
              <span v-for="item in parameterPreview(scope.row.parameters)" :key="item.key" :title="`${item.label}：${item.value}`">{{ item.label }} · {{ item.shortValue }}</span>
            </div>
            <span v-else class="empty-parameter">无需额外参数</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="120" fixed="right">
          <template #default="scope"><ElButton link type="primary" @click="showParameters(scope.row)">查看完整参数</ElButton></template>
        </ElTableColumn>
      </ElTable>
    </section>

    <ElDrawer
      v-model="parameterDrawerOpen"
      title="能力参数"
      size="min(640px, 94vw)"
      destroy-on-close
    >
      <div v-if="selectedCapability" class="capability-detail">
        <div class="capability-detail-heading">
          <span class="capability-icon" aria-hidden="true"><component :is="capabilityIcon(selectedCapability.capability)" /></span>
          <div><h3>{{ capabilityLabels[selectedCapability.capability] || '其他能力' }}</h3><p>{{ serviceInfo(selectedCapability).model || serviceInfo(selectedCapability).name }}</p></div>
          <ElTag :type="selectedCapability.enabled ? 'success' : 'info'">{{ selectedCapability.enabled ? '可用' : '未启用' }}</ElTag>
        </div>
        <dl v-if="parameterItems(selectedCapability.parameters).length" class="parameter-list">
          <div v-for="item in parameterItems(selectedCapability.parameters)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div>
        </dl>
        <ElEmpty v-else description="该能力无需额外参数" />
      </div>
      <template #footer>
        <ElButton @click="parameterDrawerOpen = false">关闭</ElButton>
        <ElButton v-if="modelSettings?.canManage && modelConfiguration(selectedCapability?.capability || '')" type="primary" @click="configureSelected">配置此模型</ElButton>
      </template>
    </ElDrawer>

    <ElCollapse class="technical-panel">
      <ElCollapseItem name="providers" title="技术接入详情（仅供管理员排查）">
        <section aria-labelledby="provider-title">
          <div class="section-heading">
            <h3 id="provider-title">服务接入信息</h3>
            <span>下列技术标识不会用于日常内容创作页面</span>
          </div>
          <ElTable :data="providers" empty-text="服务接入接口未返回项目">
            <ElTableColumn label="服务名称" min-width="210">
              <template #default="scope">
                <div class="technical-cell">
                  <strong>{{ serviceInfo(scope.row).name }}</strong>
                  <small>技术名称：{{ scope.row.name }}</small>
                </div>
              </template>
            </ElTableColumn>
            <ElTableColumn label="主要用途" min-width="160">
              <template #default="scope">
                {{
                  capabilityLabels[scope.row.capability] || '其他能力'
                }}
              </template>
            </ElTableColumn>
            <ElTableColumn label="运行方式" width="140">
              <template #default="scope">{{ modeLabel(scope.row.mode) }}</template>
            </ElTableColumn>
            <ElTableColumn label="技术标识" min-width="200">
              <template #default="scope">
                <span class="technical-value">{{ scope.row.provider_id }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="模型/组件" min-width="220">
              <template #default="scope">
                <div class="technical-cell">
                  <strong>{{ scope.row.model ? modelLabel(scope.row.model) : '本地组件' }}</strong>
                  <small>{{
                    scope.row.model ? `模型标识：${scope.row.model}` : '无独立模型'
                  }}</small>
                </div>
              </template>
            </ElTableColumn>
            <ElTableColumn label="通道" width="100">
              <template #default="scope">
                <ElTag :type="scope.row.external ? 'warning' : 'success'">
                  {{
                    scope.row.external ? '外部' : '本地'
                  }}
                </ElTag>
              </template>
            </ElTableColumn>
          </ElTable>
        </section>
      </ElCollapseItem>
    </ElCollapse>
  </div>
</template>

<style scoped>
.content-services-view {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
  gap: 28px;
}
.content-services-view > section {
  min-width: 0;
}
.view-heading,
.section-heading,
.title-with-status,
.service-actions,
.service-grid header,
.service-grid footer,
.capability-detail-heading {
  display: flex;
  align-items: center;
  gap: 12px;
}
.view-heading,
.section-heading,
.service-grid header,
.service-grid footer {
  justify-content: space-between;
}
.view-heading,
.section-heading,
.service-actions,
.title-with-status {
  flex-wrap: wrap;
}
.view-heading h2,
.view-heading p,
.section-heading h3,
.section-heading p,
.service-grid h4,
.service-grid p,
.capability-detail h3,
.capability-detail p {
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
.status-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 6px;
  border-radius: 50%;
  background: currentColor;
}
.service-actions .el-button {
  margin-left: 0;
}
.section-heading {
  margin-bottom: 16px;
}
.section-heading h3 {
  color: var(--xi-semantic-color-text-primary);
  font-size: 16px;
}
.section-heading p {
  margin-top: 6px;
  font-size: 13px;
  color: var(--xi-semantic-color-text-secondary);
}
.section-heading > span,
.service-grid small,
.component-note {
  color: var(--xi-semantic-color-text-secondary);
  font-size: 12px;
}
.service-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}
.service-grid article {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 20px;
  background: var(--xi-semantic-color-background-container);
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 12px;
}
.capability-icon {
  display: grid;
  place-items: center;
  flex: none;
  width: 38px;
  height: 38px;
  color: var(--xi-semantic-color-text-brand);
  background: var(--xi-semantic-color-background-brand-subtle);
  border-radius: 10px;
}
.capability-icon svg {
  width: 20px;
  height: 20px;
}
.service-grid h4 {
  flex: 1;
  margin: 0;
  font-size: 15px;
  color: var(--xi-semantic-color-text-primary);
}
.service-grid .service-model {
  margin-top: 14px;
  color: var(--xi-semantic-color-text-primary);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: 13px;
}
.service-grid .service-description {
  margin: 6px 0 18px;
  color: var(--xi-semantic-color-text-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.service-grid footer {
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--xi-semantic-color-border-subtle);
}
.component-note {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.component-note svg {
  width: 14px;
  height: 14px;
}
.capabilities-panel {
  padding: 22px;
  background: var(--xi-semantic-color-background-container);
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 12px;
}
.technical-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.technical-cell strong {
  color: var(--xi-semantic-color-text-primary);
  font-weight: 500;
}
.technical-cell small,
.technical-value,
.empty-parameter {
  color: var(--xi-semantic-color-text-secondary);
  font-size: 12px;
}
.capability-table .technical-cell strong,
.capability-table .technical-cell small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.capability-table :deep(.el-table__row td) {
  height: 64px;
}
.parameter-preview {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
}
.parameter-preview span {
  flex: 0 1 auto;
  min-width: 0;
  max-width: 140px;
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--xi-semantic-color-background-page);
  color: var(--xi-semantic-color-text-secondary);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.technical-panel {
  padding: 0 20px;
  background: var(--xi-semantic-color-background-container);
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 12px;
}
.technical-panel section {
  padding-bottom: 18px;
}
.technical-value {
  overflow-wrap: anywhere;
}
.capability-detail {
  display: grid;
  gap: 24px;
}
.capability-detail-heading {
  align-items: flex-start;
  padding: 18px;
  background: var(--xi-semantic-color-background-page);
  border-radius: 12px;
}
.capability-detail-heading > div {
  flex: 1;
  min-width: 0;
}
.capability-detail-heading h3 {
  font-size: 17px;
  color: var(--xi-semantic-color-text-primary);
}
.capability-detail-heading p {
  margin-top: 6px;
  color: var(--xi-semantic-color-text-secondary);
  overflow-wrap: anywhere;
  font-size: 13px;
}
.parameter-list {
  display: grid;
  gap: 0;
  margin: 0;
}
.parameter-list > div {
  display: grid;
  grid-template-columns: minmax(110px, 0.35fr) minmax(0, 1fr);
  gap: 16px;
  padding: 15px 0;
  border-bottom: 1px solid var(--xi-semantic-color-border-subtle);
}
.parameter-list dt {
  color: var(--xi-semantic-color-text-secondary);
  font-size: 13px;
}
.parameter-list dd {
  margin: 0;
  color: var(--xi-semantic-color-text-primary);
  overflow-wrap: anywhere;
  font-size: 13px;
  line-height: 1.7;
}
@media (max-width: 1000px) {
  .service-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 600px) {
  .service-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .capabilities-panel {
    padding: 16px;
  }
  .parameter-list > div {
    grid-template-columns: 1fr;
    gap: 7px;
  }
}
</style>
