<script setup lang="ts">
/* eslint-disable financial/no-division */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import WaveSurfer from 'wavesurfer.js'

const props = defineProps<{
  itemId: string
  label: string
  url: string
  clipStart: number
  clipDuration: number
  trimIn: number
  trimOut: number
  speed: number
  volume: number
  loop: boolean
  playhead: number
}>()

const emit = defineEmits<{
  seek: [seconds: number]
  mediaElement: [itemId: string, element: HTMLAudioElement | null]
  error: [message: string]
  ready: []
}>()

const host = ref<HTMLElement>()
const audioElement = ref<HTMLAudioElement>()
const loading = ref(true)
const failure = ref('')
let waveSurfer: WaveSurfer | undefined
let sourceGeneration = 0

const clipEnd = computed(() => props.clipStart + props.clipDuration)
const accessibleValue = computed(() => Math.max(props.clipStart, Math.min(props.playhead, clipEnd.value)))

function playheadForSourceTime(value: number) {
  const relative = (value - props.trimIn) / Math.max(0.1, props.speed)
  return Math.max(props.clipStart, Math.min(clipEnd.value, props.clipStart + relative))
}

function isAbortError(error: unknown) {
  return typeof error === 'object'
    && error !== null
    && 'name' in error
    && error.name === 'AbortError'
}

function fail(error: unknown, generation: number, instance?: WaveSurfer) {
  const isCurrent = generation === sourceGeneration && (!instance || waveSurfer === instance)
  if (!isCurrent || isAbortError(error)) {
    if (isCurrent) {
      loading.value = false
    }
    return
  }
  const message = error instanceof Error ? error.message : '音频解码失败'
  failure.value = `波形不可用，已回退音频预览：${message}`
  loading.value = false
  instance?.destroy()
  if (!instance || waveSurfer === instance) {
    waveSurfer = undefined
  }
  emit('error', failure.value)
}

async function createWaveform() {
  const generation = ++sourceGeneration
  failure.value = ''
  loading.value = true
  waveSurfer?.destroy()
  waveSurfer = undefined
  await nextTick()
  if (!host.value || !audioElement.value || !props.url || generation !== sourceGeneration) {
    return
  }
  emit('mediaElement', props.itemId, audioElement.value)
  try {
    const theme = getComputedStyle(host.value)
    const tokenColor = (name: string) => theme.getPropertyValue(name).trim()
    const instance = WaveSurfer.create({
      container: host.value,
      media: audioElement.value,
      url: props.url,
      backend: 'MediaElement',
      height: 58,
      waveColor: tokenColor('--xi-semantic-color-border-brand-strong'),
      progressColor: tokenColor('--xi-semantic-color-action-primary-default'),
      cursorColor: tokenColor('--xi-semantic-color-feedback-danger-default'),
      cursorWidth: 2,
      barWidth: 2,
      barGap: 2,
      barRadius: 2,
      normalize: true,
      interact: true,
      dragToSeek: { debounceTime: 40 },
    })
    waveSurfer = instance
    instance.on('ready', () => {
      if (generation !== sourceGeneration || waveSurfer !== instance) {
        return
      }
      loading.value = false
      emit('ready')
    })
    instance.on('interaction', (time) => {
      if (generation !== sourceGeneration || waveSurfer !== instance) {
        return
      }
      const target = playheadForSourceTime(time)
      emit('seek', target)
    })
    instance.on('error', error => fail(error, generation, instance))
    // External media and `url` are loaded automatically by WaveSurfer after creation.
  }
  catch (error) {
    fail(error, generation)
  }
}

function onKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    return
  }
  event.preventDefault()
  let target = accessibleValue.value
  if (event.key === 'Home') {
    target = props.clipStart
  }
  else if (event.key === 'End') {
    target = clipEnd.value
  }
  else {
    const direction = event.key === 'ArrowRight' ? 1 : -1
    target += direction * (event.shiftKey ? 1 : 0.1)
  }
  emit('seek', Math.max(props.clipStart, Math.min(clipEnd.value, target)))
}

watch(() => props.url, createWaveform)
// WaveSurfer follows the shared element's native timeupdate events. The workspace
// alone owns playback position; a playhead watcher here would repeatedly seek audio.

onMounted(createWaveform)

onBeforeUnmount(() => {
  sourceGeneration += 1
  waveSurfer?.destroy()
  waveSurfer = undefined
  emit('mediaElement', props.itemId, null)
})
</script>

<template>
  <section class="audio-waveform" :aria-busy="loading">
    <header>
      <strong>{{ label }}</strong>
      <span>{{ loading ? '正在生成波形' : failure ? 'HTML5 回退' : '可拖动定位' }}</span>
    </header>
    <div
      ref="host"
      class="wave-host"
      role="slider"
      tabindex="0"
      :aria-label="`${label} 音频波形播放头`"
      :aria-valuemin="clipStart"
      :aria-valuemax="clipEnd"
      :aria-valuenow="accessibleValue"
      :aria-valuetext="`${accessibleValue.toFixed(1)} 秒`"
      @keydown="onKeydown"
    >
      <span v-if="loading" class="wave-placeholder" aria-hidden="true" />
    </div>
    <audio
      ref="audioElement"
      class="wave-audio"
      crossorigin="anonymous"
      :src="url"
      preload="metadata"
      :aria-label="`${label} 音频回退播放器`"
      @loadedmetadata="emit('ready')"
    />
    <p v-if="failure" role="status">{{ failure }}</p>
  </section>
</template>

<style scoped>
.audio-waveform {
  display: grid;
  gap: 8px;
  width: 100%;
  padding: 8px;
  background: var(--xi-semantic-color-background-brand-subtle);
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: calc(var(--xi-primitive-radius-md) * 1px);
}

.audio-waveform header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.audio-waveform header strong {
  overflow: hidden;
  color: var(--xi-semantic-color-text-primary);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 20px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.audio-waveform header span,
.audio-waveform p {
  margin: 0;
  color: var(--xi-semantic-color-text-secondary);
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 20px;
}

.wave-host {
  position: relative;
  min-height: 58px;
  overflow: hidden;
  cursor: ew-resize;
  background: var(--xi-semantic-color-background-container);
  border-radius: calc(var(--xi-primitive-radius-sm) * 1px);
}

.wave-host:focus-visible {
  outline: 2px solid var(--xi-semantic-color-border-brand-default);
  outline-offset: 2px;
}

.wave-placeholder {
  position: absolute;
  inset: 10px 0;
  background: repeating-linear-gradient(90deg, var(--xi-semantic-color-border-brand-subtle) 0 2px, transparent 2px 6px);
  opacity: 0.65;
}

.wave-audio {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}

@media (prefers-reduced-motion: reduce) {
  .wave-placeholder {
    animation: none;
  }
}
</style>
