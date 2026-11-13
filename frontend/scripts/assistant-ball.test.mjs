import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const ball = read('../src/views/agent/content/AssistantBall.vue')
const panel = read('../src/views/agent/content/AssistantPanel.vue')
const chat = read('../src/views/agent/content/ContentChatView.vue')
const workbench = read('../src/views/agent/ContentWorkbench.vue')

test('the floating assistant is on every workbench page except AI 对话 itself', () => {
  assert.match(workbench, /<AssistantBall :hidden="currentView === 'chat'" \/>/)
  // The dialog is a separate chunk, loaded on first use.
  assert.match(ball, /defineAsyncComponent\(loadPanel\)/)
  assert.match(ball, /import\('\.\/AssistantPanel\.vue'\)/)
})

test('the ball is draggable, snaps to a side and remembers where it was', () => {
  assert.match(ball, /@pointerdown="onPointerDown"/)
  assert.match(ball, /position\.side = drag\.x \+ HALF < \(viewport\.width >> 1\) \? 'left' : 'right'/)
  assert.match(ball, /localStorage\.setItem\(STORAGE_KEY/)
  // A drag never counts as a click that opens the dialog.
  assert.match(ball, /if \(suppressClick\)/)
})

test('the dialog is an ordinary AI 对话 conversation with typing and voice', () => {
  assert.match(panel, /output: 'auto'/)
  assert.match(panel, /useVoiceInput\(/)
  assert.match(panel, /query: \{ conversation: id \}/)
  assert.match(panel, /emit\('close'\)/)
})

test('AI 对话 has no separate 运营助手 switch: the router picks the assistant', () => {
  assert.doesNotMatch(chat, /agentMode/)
  assert.match(chat, /output: 'auto'/)
})
