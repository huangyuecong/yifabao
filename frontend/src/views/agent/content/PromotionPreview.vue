<script setup lang="ts">
import type { PreviewLimits } from './promotionPreview'
import type { ProductContent, PromotionPlatform } from '@/api/agent/catalog'
import { ArrowLeft, ArrowRight } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'
import { catalogMedia } from '@/api/agent/catalog'
import { platformName } from './catalogPresentation'
import PromotionPhone from './PromotionPhone.vue'
import { isOver, previewChecks, previewPlatforms } from './promotionPreview'

/* "Preview before publishing": the selected content as each promotion platform will show it. */
const props = defineProps<{
  content: ProductContent | null
  position: number
  total: number
  chosen: boolean
  platforms: PromotionPlatform[]
  limits: PreviewLimits
}>()
const emit = defineEmits<{ step: [delta: number] }>()

const active = ref<PromotionPlatform>(props.platforms[0] ?? 'douyin')

const videoUrl = computed(() => {
  const video = props.content?.media.find(item => item.kind === 'video')
  return props.content && video ? catalogMedia.content(props.content.id, video.id) : ''
})
const checks = computed(() => props.content
  ? previewChecks(active.value, props.content.title, props.content.body, props.limits)
  : [])
const overflowing = computed(() => Object.fromEntries(previewPlatforms.map(platform => [
  platform,
  !!props.content && previewChecks(platform, props.content.title, props.content.body, props.limits).some(isOver),
])) as Record<PromotionPlatform, boolean>)
</script>

<template>
  <section class="wb-panel preview-panel">
    <div class="wb-panel-head">
      <h2>发布预览</h2>
      <div v-if="content && total > 1" class="pager">
        <ElButton
          link
          :icon="ArrowLeft"
          aria-label="上一条内容"
          @click="emit('step', -1)"
        />
        <span class="wb-num">{{ position }} / {{ total }}</span>
        <ElButton
          link
          :icon="ArrowRight"
          aria-label="下一条内容"
          @click="emit('step', 1)"
        />
      </div>
    </div>

    <div v-if="!content" class="wb-empty">选择一条内容，这里会显示它发布到抖音、小红书、快手后的样子。</div>
    <div v-else class="wb-panel-body preview-body">
      <div class="wb-tabs" role="tablist">
        <button
          v-for="platform in previewPlatforms"
          :key="platform"
          type="button"
          role="tab"
          :aria-selected="active === platform"
          :class="{ active: active === platform, unused: !platforms.includes(platform) }"
          @click="active = platform"
        >
          {{ platformName(platform) }}
          <i v-if="overflowing[platform]" class="flag" title="字数超出平台限制" />
        </button>
      </div>

      <p class="preview-name">
        <span class="wb-ellipsis">{{ content.product.name }}</span>
        <ElTag
          v-if="!chosen"
          size="small"
          type="info"
        >
          未选择
        </ElTag>
      </p>

      <PromotionPhone
        :key="content.id"
        :platform="active"
        :title="content.title"
        :body="content.body"
        :video-url="videoUrl"
        :limits="limits"
      />

      <ul class="checks">
        <li
          v-for="check in checks"
          :key="check.key"
          :class="{ over: isOver(check) }"
        >
          <span>{{ check.label }}</span>
          <span class="wb-num">{{ check.count }} / {{ check.limit }}</span>
          <em v-if="isOver(check)">超出 {{ check.count - check.limit }} 字</em>
        </li>
      </ul>
      <p v-if="!platforms.includes(active)" class="note">这个平台当前没有勾选，不会发布到这里。</p>
      <p v-if="active === 'kuaishou'" class="note">快手没有单独的标题栏，标题会排在作品描述的第一行。</p>
      <p class="note">按各平台发布页模拟，点击画面可播放；实际显示以平台为准。</p>
    </div>
  </section>
</template>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  gap: 2px;
  font-size: 13px;
}
.pager .el-button {
  margin: 0;
}
.preview-body {
  display: grid;
  gap: 12px;
}
.wb-tabs button.unused {
  color: var(--xi-semantic-color-text-placeholder);
}
.wb-tabs button.active.unused {
  color: var(--xi-semantic-color-text-tertiary);
}
.flag {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-left: 4px;
  vertical-align: middle;
  background: var(--xi-semantic-color-feedback-danger-default);
  border-radius: 50%;
}
.preview-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  margin: 0;
  font-size: 13px;
  font-weight: 500;
}
.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}
.checks li {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 8px;
  color: var(--xi-semantic-color-text-secondary);
  font-size: 12px;
  background: var(--wb-hover);
  border-radius: 3px;
}
.checks li.over {
  color: var(--xi-semantic-color-feedback-danger-text);
  background: var(--xi-semantic-color-feedback-danger-background-subtle);
}
.checks em {
  font-style: normal;
  font-weight: 600;
}
.note {
  margin: 0;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  line-height: 1.6;
}
</style>
