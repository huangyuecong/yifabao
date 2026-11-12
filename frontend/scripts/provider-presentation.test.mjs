import assert from 'node:assert/strict'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import { providerPresentation } from '../src/views/agent/content/providerPresentation.ts'

test('old provider IDs cannot override the active text and music adapters', () => {
  const text = { capability: 'text-generation', provider_id: 'hiagent-chat-ds-flash' }
  const music = { capability: 'music-generation', provider_id: 'volcengine-ark-seedance-music' }
  const providers = [
    { ...text, name: 'openai-compatible-text', model: 'chat-max', external: true },
    { ...music, name: 'HiAgent Seedance 背景音乐', model: 'actual-service-id', external: true },
  ]
  assert.deepEqual(providerPresentation(text, providers), { name: 'OpenAI 兼容文本接口', model: '模型/服务 ID：chat-max' })
  assert.deepEqual(providerPresentation(music, providers), { name: 'HiAgent 背景音乐接口', model: '模型/服务 ID：actual-service-id' })
  providers[0].model = 'new-selected-model'
  assert.equal(providerPresentation(text, providers).model, '模型/服务 ID：new-selected-model')
})

test('capability isolates shared IDs; absent metadata is not guessed from an old ID', () => {
  const row = { capability: 'image-generation', provider_id: 'shared' }
  const providers = [{ ...row, capability: 'ai-video-generation', name: 'hiagent-seedance', model: 'video' }]
  assert.deepEqual(providerPresentation(row, providers), { name: '服务信息待确认', model: '' })
  assert.equal(providerPresentation(row, [{ ...row, name: 'custom-adapter', model: 'custom-model', external: true }]).name, 'custom-adapter')
  assert.equal(providerPresentation(row, [{ ...row, name: 'local', model: null, external: false }]).model, '本地组件，无需模型')
})
