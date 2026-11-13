<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

/*
 * The operations assistant as a floating ball (like the iPhone's AssistiveTouch):
 * drag it anywhere, it snaps to the nearest side, and a click opens a floating dialog.
 * The dialog is loaded on first use, and stays mounted afterwards so a reply in
 * progress keeps streaming while it is collapsed.
 */
const props = defineProps<{ hidden?: boolean }>()

const loadPanel = () => import('./AssistantPanel.vue')
const AssistantPanel = defineAsyncComponent(loadPanel)

const SIZE = 56
const HALF = 28 // SIZE / 2
const MARGIN = 14
const GAP = 12
const PANEL_WIDTH = 400
const STORAGE_KEY = 'yifabao.assistant.position'

const open = ref(false)
const opened = ref(false)
const idle = ref(false)
const status = reactive({ busy: false, unread: false })
const viewport = reactive({ width: window.innerWidth, height: window.innerHeight })
const position = reactive<{ side: 'left' | 'right', ratio: number }>(readPosition())
const drag = reactive({ active: false, x: 0, y: 0 })
const ball = ref<HTMLButtonElement>()
let pointer: { id: number, startX: number, startY: number, offsetX: number, offsetY: number, moved: boolean } | undefined
let suppressClick = false
let idleTimer: ReturnType<typeof setTimeout> | undefined

const compact = computed(() => viewport.width < 640)
const top = computed(() => {
  if (drag.active) {
    return drag.y
  }
  const room = Math.max(0, viewport.height - SIZE - MARGIN * 2)
  return MARGIN + Math.round(room * position.ratio)
})
const left = computed(() => {
  if (drag.active) {
    return drag.x
  }
  return position.side === 'left' ? MARGIN : viewport.width - SIZE - MARGIN
})
const ballStyle = computed(() => ({ transform: `translate3d(${left.value}px, ${top.value}px, 0)` }))
const panelStyle = computed(() => {
  if (compact.value) {
    return {}
  }
  const height = Math.min(640, viewport.height - MARGIN * 2)
  const centered = top.value + HALF - (height >> 1)
  const y = Math.min(Math.max(MARGIN, centered), viewport.height - height - MARGIN)
  const horizontal = position.side === 'right'
    ? { right: `${MARGIN + SIZE + GAP}px` }
    : { left: `${MARGIN + SIZE + GAP}px` }
  const origin = position.side === 'right' ? 'right' : 'left'
  return {
    ...horizontal,
    top: `${y}px`,
    width: `${Math.min(PANEL_WIDTH, viewport.width - MARGIN * 2 - SIZE - GAP)}px`,
    height: `${height}px`,
    transformOrigin: `${origin} ${top.value + HALF - y}px`,
  }
})
const label = computed(() => {
  if (status.busy) {
    return '运营助手正在处理，点击查看'
  }
  return status.unread ? '运营助手有新回复' : '打开运营助手'
})

function readPosition(): { side: 'left' | 'right', ratio: number } {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null') as { side?: string, ratio?: number } | null
    if (saved && (saved.side === 'left' || saved.side === 'right') && typeof saved.ratio === 'number') {
      return { side: saved.side, ratio: Math.min(1, Math.max(0, saved.ratio)) }
    }
  }
  catch {
    // Storage may be unavailable; fall back to the default spot.
  }
  return { side: 'right', ratio: 0.78 }
}

function savePosition() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(position))
  }
  catch {
    // Not remembering the spot is harmless.
  }
}

function wake() {
  idle.value = false
  clearTimeout(idleTimer)
  idleTimer = setTimeout(() => {
    idle.value = !open.value
  }, 3000)
}

function toggle() {
  if (suppressClick) {
    suppressClick = false
    return
  }
  open.value = !open.value
  opened.value = true
  wake()
}

function close() {
  open.value = false
  wake()
  ball.value?.focus()
}

function onPointerDown(event: PointerEvent) {
  if (event.button !== 0) {
    return
  }
  pointer = {
    id: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    offsetX: event.clientX - left.value,
    offsetY: event.clientY - top.value,
    moved: false,
  }
  void loadPanel() // warm the dialog while the finger is down
  wake()
}

function onPointerMove(event: PointerEvent) {
  if (!pointer || event.pointerId !== pointer.id) {
    return
  }
  if (!pointer.moved && Math.hypot(event.clientX - pointer.startX, event.clientY - pointer.startY) < 6) {
    return
  }
  if (!pointer.moved) {
    pointer.moved = true
    ball.value?.setPointerCapture(event.pointerId)
  }
  drag.active = true
  drag.x = Math.min(Math.max(0, event.clientX - pointer.offsetX), viewport.width - SIZE)
  drag.y = Math.min(Math.max(0, event.clientY - pointer.offsetY), viewport.height - SIZE)
}

function onPointerUp(event: PointerEvent) {
  if (!pointer || event.pointerId !== pointer.id) {
    return
  }
  if (pointer.moved) {
    // Snap to the nearer side and keep the height, like AssistiveTouch.
    suppressClick = true
    position.side = drag.x + HALF < (viewport.width >> 1) ? 'left' : 'right'
    const room = Math.max(1, viewport.height - SIZE - MARGIN * 2)
    // eslint-disable-next-line financial/no-division -- Screen position ratio, not financial arithmetic.
    position.ratio = Math.min(1, Math.max(0, (drag.y - MARGIN) / room))
    savePosition()
    setTimeout(() => {
      suppressClick = false
    }, 0)
  }
  drag.active = false
  pointer = undefined
  wake()
}

function onResize() {
  viewport.width = window.innerWidth
  viewport.height = window.innerHeight
}

function onStatus(value: { busy: boolean, unread: boolean }) {
  status.busy = value.busy
  status.unread = value.unread
}

onMounted(() => {
  window.addEventListener('resize', onResize)
  wake()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  clearTimeout(idleTimer)
})
</script>

<template>
  <div class="assistant-float" :class="{ 'is-hidden': props.hidden }">
    <Transition name="assistant-pop">
      <div
        v-if="opened"
        v-show="open && !props.hidden"
        class="assistant-window"
        :class="{ 'is-compact': compact }"
        :style="panelStyle"
      >
        <AssistantPanel :open="open && !props.hidden" @close="close" @status="onStatus" />
      </div>
    </Transition>
    <div
      v-if="compact && open && !props.hidden"
      class="assistant-scrim"
      aria-hidden="true"
      @click="close"
    />

    <button
      v-show="!props.hidden && !(compact && open)"
      ref="ball"
      type="button"
      class="assistant-ball"
      :class="{
        'is-open': open,
        'is-idle': idle && !open && !status.busy && !status.unread,
        'is-dragging': drag.active,
        'is-busy': status.busy,
      }"
      :style="ballStyle"
      :aria-label="label"
      :title="label"
      :aria-expanded="open"
      @click="toggle"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
      @mouseenter="wake"
      @focus="wake"
    >
      <span class="ring-outer ring" aria-hidden="true" />
      <span class="ring-middle ring" aria-hidden="true" />
      <span class="ring-core ring" aria-hidden="true" />
      <span v-if="status.unread && !open" class="badge" aria-hidden="true" />
    </button>
  </div>
</template>

<style scoped>
.assistant-float {
  --ab-accent: var(--xi-primitive-color-brand-primary-7, #197465);
  --ab-accent-light: var(--xi-primitive-color-brand-primary-5, #8acabe);
}
.assistant-ball {
  position: fixed;
  top: 0;
  left: 0;
  z-index: 2000;
  display: grid;
  width: 56px;
  height: 56px;
  padding: 0;
  place-items: center;
  background: rgb(22 28 30 / 72%);
  backdrop-filter: blur(16px) saturate(1.6);
  border: 1px solid rgb(255 255 255 / 14%);
  border-radius: 18px;
  box-shadow:
    0 10px 28px rgb(0 0 0 / 22%),
    inset 0 1px 0 rgb(255 255 255 / 12%);
  cursor: pointer;
  touch-action: none;
  user-select: none;
  transition:
    transform 0.32s cubic-bezier(0.2, 0.9, 0.3, 1.15),
    opacity 0.3s,
    background 0.2s,
    box-shadow 0.2s;
  -webkit-tap-highlight-color: transparent;
}
.assistant-ball.is-dragging {
  transition:
    opacity 0.3s,
    background 0.2s;
  cursor: grabbing;
}
.assistant-ball.is-idle {
  opacity: 0.55;
}
.assistant-ball:hover,
.assistant-ball:focus-visible,
.assistant-ball.is-open {
  opacity: 1;
  background: rgb(18 24 26 / 86%);
  box-shadow:
    0 14px 34px rgb(0 0 0 / 28%),
    0 0 0 4px rgb(25 116 101 / 22%),
    inset 0 1px 0 rgb(255 255 255 / 14%);
}
.assistant-ball:focus-visible {
  outline: 2px solid var(--ab-accent-light);
  outline-offset: 3px;
}
.ring {
  position: absolute;
  border-radius: 50%;
  pointer-events: none;
  transition:
    transform 0.25s,
    background 0.25s,
    border-color 0.25s;
}
.ring-outer {
  inset: 9px;
  border: 2px solid rgb(255 255 255 / 22%);
}
.ring-middle {
  inset: 15px;
  border: 2px solid rgb(255 255 255 / 45%);
}
.ring-core {
  inset: 21px;
  background: radial-gradient(circle at 35% 30%, #fff 0%, #e8f4f1 60%, #cfe7e1 100%);
  box-shadow: 0 0 10px rgb(255 255 255 / 45%);
}
.assistant-ball:hover .ring-core,
.assistant-ball.is-open .ring-core {
  transform: scale(1.08);
}
.assistant-ball.is-open .ring-middle {
  border-color: var(--ab-accent-light);
}
.assistant-ball.is-busy .ring-outer {
  border-color: rgb(255 255 255 / 12%);
  border-top-color: var(--ab-accent-light);
  border-right-color: var(--ab-accent-light);
  animation: ab-spin 1s linear infinite;
}
.assistant-ball.is-busy .ring-core {
  background: radial-gradient(circle at 35% 30%, #d9fff5 0%, var(--ab-accent-light) 70%, var(--ab-accent) 100%);
  animation: ab-breathe 1.6s ease-in-out infinite;
}
.badge {
  position: absolute;
  top: -3px;
  right: -3px;
  width: 12px;
  height: 12px;
  background: #ff453a;
  border: 2px solid #fff;
  border-radius: 50%;
  box-shadow: 0 2px 6px rgb(255 69 58 / 45%);
}

.assistant-window {
  position: fixed;
  z-index: 2001;
}
.assistant-window.is-compact {
  right: 0;
  bottom: 0;
  left: 0;
  height: min(86dvh, 680px);
}
.assistant-window.is-compact :deep(.assistant-panel) {
  border-radius: 20px 20px 0 0;
}
.assistant-scrim {
  position: fixed;
  inset: 0;
  z-index: 2000;
  background: rgb(0 0 0 / 28%);
}

.assistant-pop-enter-active,
.assistant-pop-leave-active {
  transition:
    opacity 0.22s ease,
    transform 0.28s cubic-bezier(0.2, 0.9, 0.3, 1.1);
}
.assistant-pop-enter-from,
.assistant-pop-leave-to {
  opacity: 0;
  transform: scale(0.6);
}
.assistant-window.is-compact.assistant-pop-enter-from,
.assistant-window.is-compact.assistant-pop-leave-to {
  transform: translateY(40px);
}

@keyframes ab-spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes ab-breathe {
  0%,
  100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.15);
  }
}
@media (prefers-reduced-motion: reduce) {
  .assistant-ball,
  .assistant-pop-enter-active,
  .assistant-pop-leave-active {
    transition: opacity 0.2s;
  }
  .assistant-ball.is-busy .ring-outer,
  .assistant-ball.is-busy .ring-core {
    animation: none;
  }
}
</style>
