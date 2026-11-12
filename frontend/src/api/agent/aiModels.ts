import { createAgentClient } from './http'

export interface ModelConfigField {
  key: string
  label: string
  kind: 'text' | 'url' | 'secret' | 'list' | 'integers' | 'models'
  required: boolean
}
export interface ModelAdapter {
  value: string
  label: string
  fields: ModelConfigField[]
  defaults: Record<string, string | string[] | number[] | null>
}
export interface ModelConfiguration {
  capability: string
  label: string
  provider: string
  values: Record<string, string | string[] | number[] | null>
  configured_secrets: string[]
  can_rollback: boolean
  adapters: ModelAdapter[]
}
export interface ModelConfigurationDocument {
  revision: number
  configurations: ModelConfiguration[]
  history: { revision: number, capability: string, action: string, actor: string, name: string, at: string, fields: string[] }[]
}
export interface ModelConfigurationPayload {
  provider: string
  values: Record<string, string>
  expected_revision: number
}
export interface ModelOption { id: string, name: string }

const { request } = createAgentClient(import.meta.env.VITE_CONTENT_API_URL || '/content/api')
const headers = { 'X-Content-Config-Request': '1' }
export const aiModelsApi = {
  access: () => request<{ can_manage: boolean, authentication_ready: boolean, message: string }>({ url: '/api/v1/ai-models/access' }),
  get: () => request<ModelConfigurationDocument>({ url: '/api/v1/ai-models' }),
  save: (capability: string, data: ModelConfigurationPayload) => request<ModelConfigurationDocument>({ url: `/api/v1/ai-models/${capability}`, method: 'PUT', headers, data }),
  models: (capability: string, data: ModelConfigurationPayload) => request<{ models: ModelOption[], message: string }>({ url: `/api/v1/ai-models/${capability}/models`, method: 'POST', headers, data }),
  rollback: (capability: string, revision: number) => request<ModelConfigurationDocument>({ url: `/api/v1/ai-models/${capability}/rollback`, method: 'POST', headers, data: { expected_revision: revision } }),
}
