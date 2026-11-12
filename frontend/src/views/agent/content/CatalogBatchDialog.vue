<script setup lang="ts">
import type { ImageModelList, Product } from '@/api/agent/catalog'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { catalogApi } from '@/api/agent/catalog'
import { friendlyContentError } from './contentWorkflow'

const props = defineProps<{ products: Product[] }>()
const emit = defineEmits<{ created: [batch: { id: string, total: number }] }>()
const AUTO = 'auto'
const open = defineModel<boolean>({ required: true })

const form = reactive({ copy: true, images: false, video: true, videoMode: 'ai' as 'slideshow' | 'ai', clipCount: 3, voice: true, music: false, imageCount: 3, imageModel: AUTO, instructions: '' })
const submitting = ref(false)
const imageModels = ref<ImageModelList>()
const loadingModels = ref(false)
const autoOrder = computed(() => imageModels.value?.models.slice(0, 4).map(model => model.id) ?? [])
const autoLabel = computed(() => autoOrder.value.length ? `自动（${autoOrder.value[0]} 优先）` : '自动（按模型设置的顺序）')
const withoutImages = computed(() => props.products.filter(product => !product.images.length))
const nothingChosen = computed(() => !form.copy && !form.images && !form.video)

async function loadModels(refresh = false) {
  loadingModels.value = true
  try {
    imageModels.value = await catalogApi.imageModels(refresh)
  }
  catch {
    imageModels.value = undefined
  }
  finally {
    loadingModels.value = false
  }
}

watch(open, (value) => {
  if (value) {
    form.instructions = ''
    if (!imageModels.value) {
      void loadModels()
    }
  }
})

async function submit() {
  if (nothingChosen.value || withoutImages.value.length) {
    return
  }
  submitting.value = true
  try {
    const batch = await catalogApi.createBatch({
      product_ids: props.products.map(product => product.id),
      with_copy: form.copy,
      with_images: form.images,
      with_video: form.video,
      video_voice: form.video && form.voice,
      video_music: form.video && form.music,
      video_mode: form.videoMode,
      ai_clip_count: form.clipCount,
      image_count: form.imageCount,
      image_model: form.images && form.imageModel !== AUTO ? form.imageModel : '',
      instructions: form.instructions.trim(),
    })
    emit('created', batch)
    open.value = false
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '提交失败'))
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <ElDialog
    v-model="open"
    title="批量生成"
    width="520px"
    append-to-body
  >
    <p class="batch-target">
      已选 <strong>{{ products.length }}</strong> 个商品，每个商品单独生成一份，生成后进入审核。
    </p>

    <div class="wb-field-label">生成内容</div>
    <div class="batch-options">
      <label class="batch-option">
        <ElCheckbox v-model="form.copy" />
        <span><strong>标题和文案</strong><small>宣传标题、宣传正文，以及上架用的商品标题和描述</small></span>
      </label>
      <label class="batch-option">
        <ElCheckbox v-model="form.images" />
        <span><strong>宣传图片</strong><small>以商品原图为参考生成场景图，需要已配置生图模型</small></span>
        <ElSelect
          v-model="form.imageCount"
          :disabled="!form.images"
          size="small"
          class="batch-count"
          @click.prevent
        >
          <ElOption
            v-for="count in 6"
            :key="count"
            :label="`${count} 张`"
            :value="count"
          />
        </ElSelect>
      </label>
      <div v-if="form.images" class="batch-model">
        <span class="wb-field-label">图片模型</span>
        <ElSelect v-model="form.imageModel" :loading="loadingModels" class="batch-model-select">
          <ElOption :value="AUTO" :label="autoLabel" />
          <ElOption
            v-for="model in imageModels?.models ?? []"
            :key="model.id"
            :value="model.id"
            :label="model.id === imageModels?.default ? `${model.id}（默认）` : model.id"
          />
        </ElSelect>
        <ElButton
          link
          type="primary"
          :loading="loadingModels"
          @click="loadModels(true)"
        >
          刷新
        </ElButton>
        <small class="wb-muted">
          {{ imageModels?.error
            || (form.imageModel === AUTO && autoOrder.length
              ? `依次尝试：${autoOrder.join(' → ')}。实际用的模型会记在审核页。`
              : '优先用选中的模型；它失败时自动换下一个，最多试 4 个。实际用的模型会记在审核页。') }}
        </small>
      </div>
      <label class="batch-option">
        <ElCheckbox v-model="form.video" />
        <span><strong>宣传视频</strong><small>竖版 720×1280：图片缓慢推拉、交叉转场、顶部标题。抖音、小红书、快手发布需要视频</small></span>
      </label>
      <div v-if="form.video" class="batch-sub">
        <ElRadioGroup v-model="form.videoMode" class="video-mode">
          <ElRadio value="ai">
            AI 动态镜头
            <ElSelect
              v-model="form.clipCount"
              size="small"
              class="clip-count"
              :disabled="form.videoMode !== 'ai'"
              @click.prevent
            >
              <ElOption :value="2" label="2 段" />
              <ElOption :value="3" label="3 段" />
            </ElSelect>
          </ElRadio>
          <ElRadio value="slideshow">图片动画</ElRadio>
        </ElRadioGroup>
        <small class="wb-muted mode-hint">
          {{ form.videoMode === 'ai'
            ? 'Seedance 以商品图为首帧生成每段 5 秒的真实运动镜头，镜头之间直接切换，每个商品约 2–4 分钟，按次计费；失败时自动改用图片动画'
            : '图片缓慢推拉、逐张切换，几秒内完成，不产生费用' }}
        </small>
        <ElCheckbox v-model="form.voice">配音朗读宣传文案，并配同步字幕</ElCheckbox>
        <ElCheckbox v-model="form.music">背景音乐 <small class="wb-muted">（需音乐服务可用，失败时自动跳过）</small></ElCheckbox>
      </div>
    </div>

    <div class="wb-field-label">补充要求（选填）</div>
    <ElInput
      v-model="form.instructions"
      type="textarea"
      :rows="3"
      maxlength="2000"
      placeholder="例如：面向商务宴请场景，语气稳重，不要使用感叹号"
    />

    <ElAlert
      v-if="withoutImages.length"
      class="batch-alert"
      type="warning"
      :closable="false"
      :title="`${withoutImages.length} 个商品还没有图片：${withoutImages.slice(0, 3).map(item => item.name).join('、')}${withoutImages.length > 3 ? ' 等' : ''}。请先上传图片。`"
    />

    <template #footer>
      <ElButton @click="open = false">取消</ElButton>
      <ElButton
        type="primary"
        :loading="submitting"
        :disabled="nothingChosen || withoutImages.length > 0"
        @click="submit"
      >
        开始生成
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.batch-target {
  margin: 0 0 16px;
  color: var(--xi-semantic-color-text-secondary);
  font-size: 13px;
}
.batch-options {
  display: grid;
  margin-bottom: 16px;
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 6px;
}
.batch-option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  cursor: pointer;
}
.batch-option + .batch-option {
  border-top: 1px solid var(--xi-semantic-color-border-subtle);
}
.batch-option > span {
  display: grid;
  flex: 1;
  gap: 2px;
}
.batch-option strong {
  font-size: 14px;
  font-weight: 500;
}
.batch-option small {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  line-height: 1.5;
}
.batch-option :deep(.el-checkbox) {
  height: 20px;
}
.batch-count {
  width: 84px;
}
.batch-sub {
  display: grid;
  gap: 2px;
  padding: 0 14px 12px 38px;
}
.video-mode {
  display: flex;
  gap: 4px 20px;
  flex-wrap: wrap;
}
.clip-count {
  width: 72px;
  margin-left: 6px;
}
.mode-hint {
  margin-bottom: 4px;
  line-height: 1.5;
}
.batch-sub small {
  font-size: 12px;
}
.batch-model {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 4px 8px;
  padding: 0 14px 12px 38px;
}
.batch-model .wb-field-label {
  grid-column: 1 / -1;
  margin: 0;
}
.batch-model small {
  grid-column: 1 / -1;
  font-size: 12px;
  line-height: 1.5;
}
.batch-alert {
  margin-top: 12px;
}
</style>
