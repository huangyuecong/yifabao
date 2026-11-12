import type { ContentProviderStatus } from '@/api/agent/content'

// Display the active adapter, not a historical provider ID retained for old tasks.
const adapterNames: Record<string, string> = {
  'openai-compatible-text': 'OpenAI 兼容文本接口',
  'openai-compatible-image': 'OpenAI 兼容图片接口',
  'volcengine-ark-text': '火山方舟文本接口',
  'hiagent-seedream': 'HiAgent 图片接口',
  'hiagent-seedance': 'HiAgent 视频接口',
  'HiAgent Seedance 背景音乐': 'HiAgent 背景音乐接口',
  'Seedance 背景音乐': '火山方舟背景音乐接口',
  'chat-tts': 'Chat TTS 配音接口',
  'volcengine-ark-image': '火山方舟图片接口',
  'volcengine-ark-ai-video': '火山方舟视频接口',
  'volcengine-doubao-speech-tts-2': '豆包配音接口',
  'volcengine-v3-tts': '豆包 V3 配音接口',
  'minimax-music-2.6': 'MiniMax 背景音乐接口',
  'timestamp-aware-subtitle-alignment': '本地字幕时间对齐',
  'ffmpeg-video-composer': '本地视频合成',
  'deterministic-text': '本地模拟文本服务',
  'deterministic-image': '本地模拟图片服务',
  'deterministic-ai-video-task': '本地模拟视频服务',
  'deterministic-speech': '本地模拟配音服务',
  'deterministic-instrumental-music': '本地模拟音乐服务',
}

export function providerPresentation(
  row: { capability: string, provider_id: string },
  providers: ContentProviderStatus[],
) {
  const active = providers.find(item => item.capability === row.capability && item.provider_id === row.provider_id)
  if (!active) {
    return { name: '服务信息待确认', model: '' }
  }
  const name = active.name?.trim() || ''
  const model = active.model?.trim()
  return {
    name: adapterNames[name] || name || '服务信息待确认',
    model: model
      ? `${active.external ? '模型/服务 ID' : '组件'}：${model}`
      : active.external ? '模型由服务端指定' : '本地组件，无需模型',
  }
}
