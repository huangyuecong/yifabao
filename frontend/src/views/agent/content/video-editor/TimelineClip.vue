<script setup lang="ts">
/* eslint-disable financial/no-division */
import type { ClipResizePatch } from './timelineClipInteraction'
import type { VideoTimelineItemState, VideoTrackKind } from './timelineState'
import interact from 'interactjs'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CLIP_GESTURE_THRESHOLD, resizeTimelineClip } from './timelineClipInteraction'

const props = defineProps<{
  item: VideoTimelineItemState
  items: VideoTimelineItemState[]
  trackKind: VideoTrackKind
  trackName: string
  pixelsPerSecond: number
  selected: boolean
  locked: boolean
  sourceDuration?: number
}>()

const emit = defineEmits<{
  select: [item: VideoTimelineItemState]
  seek: [seconds: number]
  reorder: [itemId: string, targetIndex: number]
  trim: [
    itemId: string,
    patch: Pick<VideoTimelineItemState, 'duration_seconds' | 'trim_in_seconds' | 'trim_out_seconds'>,
  ]
  position: [itemId: string, startSeconds: number]
  resize: [
    itemId: string,
    patch: Pick<
      VideoTimelineItemState,
      'start_seconds' | 'duration_seconds' | 'trim_in_seconds' | 'trim_out_seconds' | 'speed'
    >,
  ]
  remove: [itemId: string]
  move: [itemId: string, offset: number]
}>()

const clipElement = ref<HTMLElement>()
const interacting = ref<'dragging' | 'resizing' | ''>('')
const dragOffsetX = ref(0)
const resizePreview = ref<ClipResizePatch | null>(null)
let interactionItem: VideoTimelineItemState | undefined
let gestureMoved = false
let rawDragOffsetX = 0
let resizeDeltaLeft = 0
let resizeDeltaRight = 0

const displayedItem = computed(() => resizePreview.value || props.item)
const clipStyle = computed(() => ({
  left: `${displayedItem.value.start_seconds * props.pixelsPerSecond}px`,
  width: `${Math.max(1, displayedItem.value.duration_seconds * props.pixelsPerSecond)}px`,
  transform: interacting.value === 'dragging' ? `translateX(${dragOffsetX.value}px)` : '',
}))

function resetTransientStyle() {
  interacting.value = ''
  dragOffsetX.value = 0
  resizePreview.value = null
  interactionItem = undefined
  gestureMoved = false
}

function snapSeconds(pixels: number) {
  return Math.round(pixels / props.pixelsPerSecond * 10) / 10
}

function commitResize(item: VideoTimelineItemState, patch: ClipResizePatch) {
  if (props.locked || Object.entries(patch).every(([key, value]) =>
    Math.abs(value - Number(item[key as keyof ClipResizePatch])) <= 0.001)) {
    return
  }
  if (props.trackKind === 'video') {
    emit('trim', item.id, {
      duration_seconds: patch.duration_seconds,
      trim_in_seconds: patch.trim_in_seconds,
      trim_out_seconds: patch.trim_out_seconds,
    })
  }
  else {
    emit('resize', item.id, patch)
  }
}

function configureInteract() {
  const element = clipElement.value
  if (!element) {
    return
  }
  resetTransientStyle()
  interact(element).unset()
  if (props.locked) {
    return
  }
  interact(element)
    .draggable({
      allowFrom: '.clip-drag-surface',
      inertia: false,
      listeners: {
        start() {
          interactionItem = { ...props.item }
          emit('select', props.item)
          interacting.value = 'dragging'
          rawDragOffsetX = 0
          dragOffsetX.value = 0
          gestureMoved = false
        },
        move(event) {
          rawDragOffsetX += event.dx
          gestureMoved ||= Math.abs(rawDragOffsetX) >= CLIP_GESTURE_THRESHOLD
          dragOffsetX.value = gestureMoved ? snapSeconds(rawDragOffsetX) * props.pixelsPerSecond : 0
        },
        end() {
          if (interacting.value !== 'dragging' || !interactionItem) {
            return
          }
          const item = interactionItem
          const offset = dragOffsetX.value
          const moved = gestureMoved && Math.abs(offset) >= 0.001
          resetTransientStyle()
          if (!moved || props.locked) {
            return
          }
          if (props.trackKind === 'video') {
            const draggedCenter
              = item.start_seconds * props.pixelsPerSecond
                + (item.duration_seconds * props.pixelsPerSecond) / 2
                + offset
            const targetIndex = props.items
              .filter(candidate => candidate.id !== item.id)
              .filter(
                item =>
                  draggedCenter
                  > (item.start_seconds + item.duration_seconds / 2) * props.pixelsPerSecond,
              )
              .length
            const currentIndex = props.items.findIndex(candidate => candidate.id === item.id)
            if (currentIndex >= 0 && currentIndex !== targetIndex) {
              emit('reorder', item.id, targetIndex)
            }
            return
          }
          const startSeconds = Math.max(
            0,
            item.start_seconds + offset / props.pixelsPerSecond,
          )
          if (Math.abs(startSeconds - item.start_seconds) > 0.001) {
            emit('position', item.id, startSeconds)
          }
        },
      },
    })
    .resizable({
      edges: {
        left: '.clip-trim-handle-left',
        right: '.clip-trim-handle-right',
        top: false,
        bottom: false,
      },
      inertia: false,
      listeners: {
        start() {
          interactionItem = { ...props.item }
          emit('select', props.item)
          interacting.value = 'resizing'
          resizeDeltaLeft = 0
          resizeDeltaRight = 0
          gestureMoved = false
        },
        move(event) {
          resizeDeltaLeft += event.deltaRect?.left || 0
          resizeDeltaRight += event.deltaRect?.right || 0
          gestureMoved ||= Math.max(Math.abs(resizeDeltaLeft), Math.abs(resizeDeltaRight)) >= CLIP_GESTURE_THRESHOLD
          if (gestureMoved && interactionItem) {
            resizePreview.value = resizeTimelineClip(
              interactionItem,
              props.trackKind,
              snapSeconds(resizeDeltaLeft),
              snapSeconds(resizeDeltaRight),
              props.sourceDuration,
            )
          }
        },
        end() {
          if (interacting.value !== 'resizing' || !interactionItem) {
            return
          }
          const item = interactionItem
          const patch = resizePreview.value
          resetTransientStyle()
          if (patch) {
            commitResize(item, patch)
          }
        },
      },
    })
}

function onClipKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    resetTransientStyle()
    return
  }
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    emit('select', props.item)
    return
  }
  if (event.key === 'Delete' || event.key === 'Backspace') {
    event.preventDefault()
    emit('remove', props.item.id)
    return
  }
  if (event.altKey && ['ArrowLeft', 'ArrowUp'].includes(event.key)) {
    event.preventDefault()
    if (props.trackKind === 'video') {
      emit('move', props.item.id, -1)
    }
    else {
      const step = event.shiftKey ? 1 : 0.1
      emit('position', props.item.id, Math.max(0, props.item.start_seconds - step))
    }
  }
  if (event.altKey && ['ArrowRight', 'ArrowDown'].includes(event.key)) {
    event.preventDefault()
    if (props.trackKind === 'video') {
      emit('move', props.item.id, 1)
    }
    else {
      const step = event.shiftKey ? 1 : 0.1
      emit('position', props.item.id, props.item.start_seconds + step)
    }
  }
}

function onTrimKeydown(event: KeyboardEvent, edge: 'left' | 'right') {
  if (props.locked || !['ArrowLeft', 'ArrowRight'].includes(event.key)) {
    return
  }
  event.preventDefault()
  event.stopPropagation()
  const step = event.shiftKey ? 1 : 0.1
  const direction = event.key === 'ArrowRight' ? 1 : -1
  commitResize(props.item, resizeTimelineClip(
    props.item,
    props.trackKind,
    edge === 'left' ? direction * step : 0,
    edge === 'right' ? direction * step : 0,
    props.sourceDuration,
  ))
}

watch(() => [props.item.id, props.pixelsPerSecond, props.locked, props.trackKind], configureInteract)
onMounted(configureInteract)
onBeforeUnmount(() => {
  resetTransientStyle()
  if (clipElement.value) {
    interact(clipElement.value).unset()
  }
})
</script>

<template>
  <div
    ref="clipElement"
    class="timeline-item"
    :class="{
      selected,
      disabled: !item.enabled,
      dragging: interacting === 'dragging',
      resizing: interacting === 'resizing',
    }"
    :style="clipStyle"
    role="option"
    tabindex="0"
    :aria-selected="selected"
    :aria-grabbed="interacting === 'dragging'"
    :aria-label="`${trackName}片段 ${item.label}，开始 ${item.start_seconds.toFixed(1)} 秒，时长 ${item.duration_seconds.toFixed(1)} 秒`"
    @click="emit('select', item)"
    @dblclick="emit('seek', item.start_seconds)"
    @keydown="onClipKeydown"
    @pointercancel="resetTransientStyle"
  >
    <span
      v-if="!locked"
      class="clip-trim-handle clip-trim-handle-left"
      role="slider"
      tabindex="0"
      :aria-label="trackKind === 'video' ? '调整片段左侧入点' : '调整片段开始时间'"
      :aria-valuemin="0"
      :aria-valuemax="
        trackKind === 'video'
          ? item.trim_in_seconds + Math.max(0, item.duration_seconds - 0.1) * item.speed
          : item.start_seconds + item.duration_seconds - 0.1
      "
      :aria-valuenow="trackKind === 'video' ? item.trim_in_seconds : item.start_seconds"
      :aria-valuetext="
        trackKind === 'video'
          ? `入点 ${item.trim_in_seconds.toFixed(1)} 秒`
          : `开始 ${item.start_seconds.toFixed(1)} 秒`
      "
      @click.stop
      @keydown="onTrimKeydown($event, 'left')"
    />
    <span class="clip-drag-surface">
      <strong>{{ item.label }}</strong>
      <small>{{ displayedItem.duration_seconds.toFixed(1) }}s<span
        v-if="item.metadata.transition && item.metadata.transition !== 'none'"
      >
        · {{ item.metadata.transition }}</span></small>
    </span>
    <span
      v-if="!locked"
      class="clip-trim-handle clip-trim-handle-right"
      role="slider"
      tabindex="0"
      :aria-label="trackKind === 'video' ? '调整片段右侧出点' : '调整片段结束时间'"
      :aria-valuemin="0"
      :aria-valuemax="
        trackKind === 'video'
          ? item.trim_out_seconds + Math.max(0, item.duration_seconds - 0.1) * item.speed
          : 600
      "
      :aria-valuenow="
        trackKind === 'video'
          ? item.trim_out_seconds
          : item.start_seconds + item.duration_seconds
      "
      :aria-valuetext="
        trackKind === 'video'
          ? `出点 ${item.trim_out_seconds.toFixed(1)} 秒`
          : `结束 ${(item.start_seconds + item.duration_seconds).toFixed(1)} 秒`
      "
      @click.stop
      @keydown="onTrimKeydown($event, 'right')"
    />
  </div>
</template>

<style scoped>
.timeline-item {
  box-sizing: border-box;
  min-width: 0;
  touch-action: none;
  user-select: none;
}

.clip-drag-surface {
  display: grid;
  min-width: 0;
  height: 100%;
  padding: 3px 7px;
  cursor: grab;
}

.dragging .clip-drag-surface {
  cursor: grabbing;
}

.timeline-item strong,
.timeline-item small {
  overflow: hidden;
  font-size: var(--xi-semantic-typography-caption-font-size);
  line-height: 1.1;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.timeline-item small {
  opacity: 0.75;
}

.clip-trim-handle {
  position: absolute;
  z-index: 3;
  top: 1px;
  bottom: 1px;
  width: min(7px, 25%);
  cursor: ew-resize;
  background: currentcolor;
  border-radius: calc(var(--xi-primitive-radius-xs) * 1px);
  opacity: 0;
}

.clip-trim-handle-left {
  left: 1px;
}

.clip-trim-handle-right {
  right: 1px;
}

.timeline-item:is(:hover, :focus-within, .selected) .clip-trim-handle {
  opacity: 0.5;
}

.clip-trim-handle:focus-visible {
  opacity: 1;
  outline: 2px solid var(--xi-semantic-color-text-inverse);
  outline-offset: -3px;
}

.dragging,
.resizing {
  z-index: 8 !important;
  outline: 2px solid var(--xi-semantic-color-border-brand-default);
}

@media (prefers-reduced-motion: reduce) {
  .timeline-item {
    transition: none;
  }
}
</style>
