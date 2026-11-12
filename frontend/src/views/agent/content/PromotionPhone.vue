<script setup lang="ts">
import type { PreviewLimits, PreviewPlatform } from './promotionPreview'
import { computed, nextTick, onDeactivated, onMounted, ref, watch } from 'vue'
import PreviewIcon from './PreviewIcon.vue'
import { previewCopy } from './promotionPreview'

/* One platform's publish page as a phone screen: the same video, title and text the
 * publisher will fill in, laid out the way the app shows a new post. */
const props = defineProps<{
  platform: PreviewPlatform
  title: string
  body: string
  videoUrl: string
  limits: PreviewLimits
}>()

const account = '你的账号'
const clock = new Date().toTimeString().slice(0, 5)
const navLabels: Record<PreviewPlatform, string[]> = {
  douyin: ['首页', '朋友', '', '消息', '我'],
  kuaishou: ['首页', '精选', '', '消息', '我'],
  xiaohongshu: [],
}

const copy = computed(() => previewCopy(props.platform, props.title, props.body, props.limits))
const source = computed(() => (props.videoUrl ? `${props.videoUrl}#t=0.1` : undefined))

const video = ref<HTMLVideoElement>()
const playing = ref(false)
const fit = ref<'cover' | 'contain'>('cover')

function measureVideo() {
  const element = video.value
  if (element?.videoWidth && element.videoHeight) {
    // Landscape clips (wider than 7:10) keep their frame instead of being cropped to the portrait screen.
    fit.value = element.videoWidth * 10 < element.videoHeight * 7 ? 'cover' : 'contain'
  }
}

async function toggle() {
  const element = video.value
  if (!element) {
    return
  }
  if (element.paused) {
    try {
      await element.play()
    }
    catch {}
  }
  else {
    element.pause()
  }
}

const text = ref<HTMLElement>()
const expanded = ref(false)
const clamped = ref(false)

function measureText() {
  const element = text.value
  if (!expanded.value) {
    clamped.value = !!element && element.scrollHeight - element.clientHeight > 1
  }
}

watch(() => [props.platform, props.title, props.body, props.limits], () => {
  expanded.value = false
  void nextTick(measureText)
})
watch(() => props.videoUrl, () => {
  playing.value = false
})
onMounted(measureText)
// The workbench keeps this page alive; never leave a video playing behind another page.
onDeactivated(() => video.value?.pause())
</script>

<template>
  <div class="phone" :class="`is-${platform}`">
    <div class="screen">
      <video
        ref="video"
        class="media"
        :class="{ 'is-contain': fit === 'contain' }"
        :src="source"
        preload="metadata"
        playsinline
        loop
        @loadedmetadata="measureVideo"
        @play="playing = true"
        @pause="playing = false"
      />
      <p v-if="!videoUrl" class="no-video">这条内容没有视频</p>
      <button
        type="button"
        class="hit"
        :aria-label="playing ? '暂停预览视频' : '播放预览视频'"
        @click="toggle"
      />
      <div class="scrim" aria-hidden="true" />
      <span v-if="videoUrl && !playing" class="play-badge" aria-hidden="true"><PreviewIcon name="play" /></span>

      <div class="status" aria-hidden="true">
        <span>{{ clock }}</span>
        <i class="battery" />
      </div>

      <div v-if="platform === 'xiaohongshu'" class="xhs-top" aria-hidden="true">
        <PreviewIcon name="back" class="xhs-back" />
        <span class="avatar">我</span>
        <strong>{{ account }}</strong>
        <em class="follow">关注</em>
        <PreviewIcon name="more" class="xhs-more" />
      </div>
      <div v-else class="tabs" aria-hidden="true">
        <template v-if="platform === 'douyin'">
          <span>关注</span>
          <b>推荐</b>
        </template>
        <template v-else>
          <span>关注</span>
          <b>发现</b>
          <span>同城</span>
        </template>
        <PreviewIcon name="search" class="tabs-search" />
      </div>

      <div v-if="platform !== 'xiaohongshu'" class="rail" aria-hidden="true">
        <span class="avatar">我<i class="badge"><PreviewIcon name="plus" /></i></span>
        <span class="rail-item"><PreviewIcon name="heart" />0</span>
        <span class="rail-item"><PreviewIcon name="comment" />0</span>
        <span v-if="platform === 'douyin'" class="rail-item"><PreviewIcon name="star" />0</span>
        <span class="rail-item"><PreviewIcon name="share" />{{ platform === 'douyin' ? 0 : '分享' }}</span>
      </div>

      <div class="caption">
        <b v-if="platform !== 'xiaohongshu'" class="who">@{{ account }}</b>
        <p v-if="copy.title.length" class="head">
          <span
            v-for="(run, index) in copy.title"
            :key="index"
            :class="{ 'is-topic': run.topic, 'is-over': run.over }"
          >{{ run.text }}</span>
        </p>
        <p
          v-if="copy.body.length"
          ref="text"
          class="text"
          :class="{ 'is-open': expanded }"
        >
          <span
            v-for="(run, index) in copy.body"
            :key="index"
            :class="{ 'is-topic': run.topic, 'is-over': run.over }"
          >{{ run.text }}</span>
        </p>
        <button
          v-if="clamped || expanded"
          type="button"
          class="more"
          :aria-expanded="expanded"
          @click="expanded = !expanded"
        >
          {{ expanded ? '收起' : '展开' }}
        </button>
        <span v-if="platform === 'xiaohongshu'" class="date">刚刚</span>
        <span v-if="platform === 'douyin'" class="music"><PreviewIcon name="music" />{{ account }}创作的原声</span>
      </div>

      <div v-if="platform === 'xiaohongshu'" class="bar" aria-hidden="true">
        <span class="bar-input"><PreviewIcon name="edit" />说点什么...</span>
        <span class="bar-item"><PreviewIcon name="heart" />0</span>
        <span class="bar-item"><PreviewIcon name="star" />0</span>
        <span class="bar-item"><PreviewIcon name="comment" />0</span>
      </div>
      <div v-else class="nav" aria-hidden="true">
        <template v-for="(label, index) in navLabels[platform]" :key="index">
          <i v-if="!label" class="nav-plus"><PreviewIcon name="plus" /></i>
          <span v-else :class="{ 'is-on': index === 0 }">{{ label }}</span>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.phone {
  --accent: #fe2c55;
  --nav: 44px;
  --lines: 2;
  width: 288px;
  max-width: 100%;
  padding: 6px;
  margin: 0 auto;
  background: #1d2024;
  border-radius: 34px;
}
.phone.is-xiaohongshu {
  --accent: #ff2442;
  --nav: 50px;
  --lines: 3;
}
.phone.is-kuaishou {
  --accent: #ff5000;
}
.screen {
  position: relative;
  height: 552px;
  overflow: hidden;
  color: #fff;
  font-size: 12px;
  line-height: 1.5;
  background: #000;
  border-radius: 28px;
  isolation: isolate;
}
.media {
  position: absolute;
  inset: 0 0 var(--nav);
  width: 100%;
  height: calc(100% - var(--nav));
  object-fit: cover;
  background: #000;
}
.media.is-contain {
  object-fit: contain;
}
.no-video {
  position: absolute;
  inset: 0 0 var(--nav);
  display: grid;
  place-items: center;
  margin: 0;
  color: #8a8d93;
}
.hit {
  position: absolute;
  inset: 0 0 var(--nav);
  z-index: 1;
  padding: 0;
  background: none;
  border: 0;
  cursor: pointer;
}
.hit:focus-visible {
  outline: 2px solid #fff;
  outline-offset: -4px;
}

/* Everything drawn over the video ignores the pointer, so a click anywhere plays or pauses. */
.scrim,
.play-badge,
.status,
.tabs,
.xhs-top,
.rail,
.caption,
.nav,
.bar {
  position: absolute;
  z-index: 2;
  pointer-events: none;
}
.scrim {
  inset: 0 0 var(--nav);
  background:
    linear-gradient(to bottom, rgb(0 0 0 / 0.4), transparent 22%),
    linear-gradient(to top, rgb(0 0 0 / 0.72), rgb(0 0 0 / 0.28) 42%, transparent 64%);
}
.play-badge {
  top: calc((100% - var(--nav)) / 2);
  left: 50%;
  display: grid;
  width: 52px;
  height: 52px;
  margin: -26px 0 0 -26px;
  place-items: center;
  background: rgb(0 0 0 / 0.4);
  border-radius: 50%;
}
.play-badge svg {
  width: 28px;
  height: 28px;
  margin-left: 2px;
}
.status {
  inset: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 26px;
  padding: 0 18px;
  font-size: 11px;
  font-weight: 600;
}
.battery {
  position: relative;
  width: 20px;
  height: 10px;
  border: 1px solid #fff;
  border-radius: 3px;
}
.battery::before {
  position: absolute;
  inset: 1px 4px 1px 1px;
  content: '';
  background: #fff;
  border-radius: 1px;
}

.tabs {
  inset: 28px 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 18px;
  font-size: 14px;
}
.tabs span {
  opacity: 0.72;
}
.tabs b {
  position: relative;
  font-weight: 600;
}
.tabs b::after {
  position: absolute;
  bottom: -5px;
  left: 50%;
  width: 18px;
  height: 2px;
  margin-left: -9px;
  content: '';
  background: #fff;
  border-radius: 1px;
}
.tabs-search {
  position: absolute;
  right: 12px;
  width: 20px;
  height: 20px;
}
.xhs-top {
  inset: 28px 8px auto;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.xhs-top strong {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.xhs-back,
.xhs-more {
  width: 24px;
  height: 24px;
  flex: none;
}
.follow {
  padding: 1px 12px;
  color: #fff;
  font-size: 12px;
  font-style: normal;
  background: var(--accent);
  border-radius: 14px;
}

.avatar {
  position: relative;
  display: inline-grid;
  width: 26px;
  height: 26px;
  flex: none;
  place-items: center;
  color: #fff;
  font-size: 11px;
  background: #5b616a;
  border: 1.5px solid #fff;
  border-radius: 50%;
}
.badge {
  position: absolute;
  bottom: -8px;
  left: 50%;
  display: grid;
  width: 16px;
  height: 16px;
  margin-left: -8px;
  place-items: center;
  background: var(--accent);
  border-radius: 50%;
}
.badge svg {
  width: 12px;
  height: 12px;
}
.rail {
  right: 8px;
  bottom: calc(var(--nav) + 14px);
  display: flex;
  width: 44px;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  text-shadow: 0 1px 2px rgb(0 0 0 / 0.45);
}
.rail .avatar {
  width: 40px;
  height: 40px;
  margin-bottom: 6px;
  font-size: 14px;
}
.rail-item {
  display: grid;
  justify-items: center;
  gap: 2px;
  font-size: 11px;
}
.rail-item svg {
  width: 30px;
  height: 30px;
  filter: drop-shadow(0 1px 2px rgb(0 0 0 / 0.4));
}

.caption {
  right: 62px;
  bottom: calc(var(--nav) + 12px);
  left: 12px;
  display: grid;
  gap: 4px;
  text-shadow: 0 1px 2px rgb(0 0 0 / 0.5);
}
.is-xiaohongshu .caption {
  right: 12px;
}
.who {
  font-size: 14px;
  font-weight: 600;
}
.head,
.text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
.head {
  font-size: 13px;
  font-weight: 600;
}
.is-xiaohongshu .head {
  font-size: 15px;
  line-height: 1.4;
}
/* Kuaishou has no title field: the title is just the first line of the description. */
.is-kuaishou .head {
  font-size: inherit;
  font-weight: inherit;
}
.text {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: var(--lines);
}
.text.is-open {
  display: block;
  max-height: 190px;
  overflow-y: auto;
  pointer-events: auto;
}
.more {
  justify-self: start;
  padding: 0;
  color: rgb(255 255 255 / 0.85);
  font: inherit;
  background: none;
  border: 0;
  cursor: pointer;
  pointer-events: auto;
}
.is-topic {
  font-weight: 600;
}
.is-xiaohongshu .is-topic {
  color: #8cc2ff;
  font-weight: 400;
}
.is-over {
  color: #ff9a9a;
  text-decoration: underline wavy #ff4d4f;
  text-underline-offset: 2px;
}
.date {
  font-size: 11px;
  opacity: 0.7;
}
.music {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
}
.music svg {
  width: 12px;
  height: 12px;
}

.nav,
.bar {
  inset: auto 0 0;
  display: flex;
  align-items: center;
  height: var(--nav);
  background: #0d0d0d;
}
.nav {
  justify-content: space-around;
  padding: 0 6px;
  font-size: 13px;
}
.nav span {
  color: #8a8d93;
}
.nav span.is-on {
  color: #fff;
  font-weight: 600;
}
.nav-plus {
  display: grid;
  width: 36px;
  height: 26px;
  place-items: center;
  color: #000;
  background: #fff;
  border-radius: 8px;
}
.is-kuaishou .nav-plus {
  width: 32px;
  height: 32px;
  color: #fff;
  background: var(--accent);
  border-radius: 50%;
}
.nav-plus svg {
  width: 20px;
  height: 20px;
}
.bar {
  gap: 12px;
  padding: 0 12px;
}
.bar-input {
  display: flex;
  height: 30px;
  flex: 1;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  color: rgb(255 255 255 / 0.6);
  background: rgb(255 255 255 / 0.14);
  border-radius: 15px;
}
.bar-input svg {
  width: 14px;
  height: 14px;
}
.bar-item {
  display: flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
}
.bar-item svg {
  width: 20px;
  height: 20px;
}
</style>
