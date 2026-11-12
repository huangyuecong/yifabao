import type { AxiosProgressEvent } from 'axios'
import { contentMediaUrl } from './apiUrl'
import { agentFetchInit, createAgentClient } from './http'

export type ChatModelKind = 'chat' | 'image' | 'video'
export type ChatProviderSource = 'text' | 'image' | 'video'
export type ChatOutput = 'auto' | 'text' | 'doc' | 'docx' | 'pdf' | 'image' | 'video' | 'agent'

export interface ChatModel {
  id: string
  name: string
  model: string
  kind: ChatModelKind
  provider_source: ChatProviderSource
  supports_image_edit: boolean
  configured?: boolean
  provider?: string
  enabled: boolean
  supports_images: boolean
  supports_video: boolean
}

export interface ChatModelCatalog {
  models: ChatModel[]
  max_attachment_bytes: number
  supported_extensions: string[]
  message?: string
  document_formats?: ('doc' | 'docx' | 'pdf')[]
  capabilities?: {
    chat: boolean
    image: boolean
    video: boolean
    image_edit: boolean
    vision: boolean
    video_read: boolean
    documents: boolean
  }
}

export interface ChatModelOption {
  id: string
  name: string
  provider_source?: ChatProviderSource
  kind?: ChatModelKind
}

export interface ChatModelConfiguration {
  revision: number
  models: ChatModel[]
  provider: string
  configured: boolean
  available_models: ChatModelOption[]
  message?: string
}

export interface ChatAttachment {
  id: string
  name: string
  mime_type: string
  size: number
  url: string
  kind: 'document' | 'image' | 'video'
  status: 'ready'
  text_preview?: string
  frame_count?: number
}

export type ChatTool = 'answer_in_chat' | 'create_document' | 'generate_image' | 'generate_video' | 'operate_workbench'

/** One tool call made by the operations-assistant Agent. */
export interface AgentStep {
  index: number
  tool: string
  label: string
  args: string
  thought: string
  writes: boolean
  status: 'running' | 'done' | 'error'
  summary?: string
}

/** A follow-up the Agent hands back to the user, e.g. confirming a publish on its page. */
export interface AgentAction {
  type: 'open_publish' | 'open_page'
  path: string
  label: string
}

export interface AgentTrace {
  plan: string[]
  steps: AgentStep[]
  actions: AgentAction[]
}

/** How the tool for a question was chosen: by the model's function call, or by the rule fallback. */
export interface ChatRouting {
  method: 'function_calling' | 'rules'
  tool: ChatTool
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  model_id: string
  created_at: string
  status: 'completed' | 'failed' | 'pending'
  error?: string
  attachments: ChatAttachment[]
  artifacts?: ChatArtifact[]
  output?: ChatOutput
  reference_artifact_id?: string
  routing?: ChatRouting
  agent?: AgentTrace
}

export type ChatStreamEvent
  = | { type: 'stage', label: string }
    | { type: 'plan', items: string[] }
    | { type: 'step', step: AgentStep }
    | { type: 'delta', text: string }
    | { type: 'reset' }
    | { type: 'end', live: boolean }

export interface ChatArtifact {
  id: string
  name: string
  kind: 'doc' | 'docx' | 'pdf' | 'image' | 'video'
  mime_type: string
  size: number
  url: string
  status: 'ready' | 'completed' | 'pending' | 'failed'
  error?: string
  text_preview?: string
}

export interface ChatMessageRequest {
  model_id?: string
  content: string
  attachment_ids: string[]
  output: ChatOutput
  reference_artifact_id?: string
}

export interface ChatConversationSummary {
  id: string
  title: string
  model_id: string
  created_at: string
  updated_at: string
  message_count: number
}

export interface ChatConversation extends ChatConversationSummary {
  messages: ChatMessage[]
  busy?: boolean
}

const apiBase = import.meta.env.VITE_CONTENT_API_URL || '/content/api'
const { request } = createAgentClient(apiBase)
const base = '/api/v1/chat'
const configurationHeaders = { 'X-Content-Config-Request': '1' }
const conversationUrl = (id: string) => `${base}/conversations/${encodeURIComponent(id)}`

export const chatApi = {
  models: () => request<ChatModelCatalog>({ url: `${base}/models` }),
  adminModels: () => request<ChatModelConfiguration>({ url: `${base}/models/admin` }),
  saveModels: (data: { expected_revision: number, models: ChatModel[] }) => request<ChatModelConfiguration>({
    url: `${base}/models/admin`,
    method: 'PUT',
    headers: configurationHeaders,
    data,
  }),
  conversations: (offset = 0) => request<{ items: ChatConversationSummary[], total: number }>({
    url: `${base}/conversations`,
    params: { offset, limit: 100 },
  }),
  createConversation: (data: { model_id?: string, title?: string }) => request<ChatConversation>({
    url: `${base}/conversations`,
    method: 'POST',
    data,
  }),
  conversation: (id: string) => request<ChatConversation>({ url: conversationUrl(id) }),
  deleteConversation: (id: string) => request<void>({ url: conversationUrl(id), method: 'DELETE' }),
  send: (id: string, data: ChatMessageRequest) => request<ChatConversation>({
    url: `${conversationUrl(id)}/messages`,
    method: 'POST',
    data,
    timeout: 300_000,
  }),
  retry: (id: string, modelId?: string) => request<ChatConversation>({
    url: `${conversationUrl(id)}/retry`,
    method: 'POST',
    data: { model_id: modelId },
    timeout: 300_000,
  }),
  upload: (file: File, onUploadProgress?: (event: AxiosProgressEvent) => void) => {
    const data = new FormData()
    data.append('file', file)
    return request<ChatAttachment>({ url: `${base}/attachments`, method: 'POST', data, onUploadProgress, timeout: 300_000 })
  },
  attachment: (id: string) => request<Blob>({
    url: `${base}/attachments/${encodeURIComponent(id)}/content`,
    responseType: 'blob',
  }),
  deleteAttachment: (id: string) => request<void>({ url: `${base}/attachments/${encodeURIComponent(id)}`, method: 'DELETE' }),
  artifact: (id: string) => request<Blob>({
    url: `${base}/artifacts/${encodeURIComponent(id)}/content`,
    responseType: 'blob',
  }),
}

function parseStreamEvent(block: string): ChatStreamEvent | undefined {
  let type = ''
  let data = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) {
      type = line.slice(6).trim()
    }
    else if (line.startsWith('data:')) {
      data += line.slice(5).trim()
    }
  }
  if (!type) {
    return undefined
  }
  const value = data ? JSON.parse(data) as Record<string, unknown> : {}
  if (type === 'stage') {
    return { type, label: String(value.label || '') }
  }
  if (type === 'delta') {
    return { type, text: String(value.text || '') }
  }
  if (type === 'plan') {
    return { type, items: Array.isArray(value.items) ? value.items.map(String) : [] }
  }
  if (type === 'step') {
    return { type, step: value as unknown as AgentStep }
  }
  if (type === 'reset') {
    return { type }
  }
  if (type === 'end') {
    return { type, live: Boolean(value.live) }
  }
  return undefined
}

/**
 * Follow a reply in progress over server-sent events. The saved conversation stays
 * authoritative: callers keep polling, and this resolves once the server sends "end".
 */
export async function streamReply(
  conversationId: string,
  messageId: string,
  onEvent: (event: ChatStreamEvent) => void,
  signal: AbortSignal,
) {
  const init = agentFetchInit()
  const url = contentMediaUrl(`${conversationUrl(conversationId)}/messages/${encodeURIComponent(messageId)}/stream`, apiBase)
  const response = await fetch(url, {
    ...init,
    headers: { ...init.headers as Record<string, string>, Accept: 'text/event-stream' },
    signal,
  })
  if (!response.ok || !response.body) {
    throw new Error('回复流暂不可用')
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) {
      return
    }
    buffer += value.replace(/\r\n/g, '\n')
    let index = buffer.indexOf('\n\n')
    while (index >= 0) {
      const event = parseStreamEvent(buffer.slice(0, index))
      buffer = buffer.slice(index + 2)
      if (event) {
        onEvent(event)
        if (event.type === 'end') {
          await reader.cancel().catch(() => {})
          return
        }
      }
      index = buffer.indexOf('\n\n')
    }
  }
}
