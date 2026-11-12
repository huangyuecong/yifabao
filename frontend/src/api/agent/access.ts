import { createAgentClient } from './http'

export type ContentPermission = 'models.manage' | 'library.manage' | 'rules.manage' | 'audit.read'
export interface ContentAccess {
  authentication_ready: boolean
  subject: string
  display_name: string
  department: string
  is_project_admin: boolean
  permissions: string[]
  can_manage_users: boolean
  message: string
}
export interface ContentAccessUser {
  id: string
  subject: string
  display_name: string
  department: string
  is_project_admin: boolean
  permissions: string[]
  revision: number
  last_seen_at: string | null
}
export interface ContentAccessUpdate {
  expected_revision: number
  is_project_admin: boolean
  permissions: string[]
}

const { request } = createAgentClient(import.meta.env.VITE_CONTENT_API_URL || '/content/api')
export const contentAccessApi = {
  me: () => request<ContentAccess>({ url: '/api/v1/access/me' }),
  users: (params: { keyword: string, page: number, page_size: number }) => request<{ items: ContentAccessUser[], total: number, page: number, page_size: number }>({ url: '/api/v1/access/users', params }),
  update: (id: string, data: ContentAccessUpdate) => request<ContentAccessUser>({ url: `/api/v1/access/users/${encodeURIComponent(id)}`, method: 'PUT', data, headers: { 'X-Content-Access-Request': '1' } }),
}
