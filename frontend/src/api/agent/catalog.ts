import { contentMediaUrl } from './apiUrl'
import { createAgentClient } from './http'

export type ItemStatus = 'queued' | 'running' | 'review' | 'approved' | 'rejected' | 'failed'
export type RecordStatus = 'queued' | 'running' | 'submitting' | 'succeeded' | 'failed' | 'unknown'
export type GenerationPart = 'copy' | 'images' | 'video'
export type PromotionPlatform = 'douyin' | 'xiaohongshu' | 'kuaishou'
export type PublishPlatform = 'xianyu' | PromotionPlatform
export type Workflow = 'listing' | 'promotion'

export interface ProductSpec { name: string, value: string }

export interface Product {
  id: string
  name: string
  sku: string
  category: string
  price: string | null
  description: string
  specs: ProductSpec[]
  images: Array<{ id: string }>
  content_count: number
  draft_status: ItemStatus | null
  created_at: string
  updated_at: string
}

export interface ProductInput {
  name: string
  sku: string
  category: string
  price: number | null
  description: string
  specs: ProductSpec[]
}

export interface ProductSummary { id: string, name: string, price: string | null, category: string, image_id: string | null }
export interface MediaRef { id: string, kind: 'image' | 'video' }

export interface GenerationItem {
  id: string
  batch_id: string
  batch_name: string
  status: ItemStatus
  revision: number
  title: string
  body: string
  listing_title: string
  listing_description: string
  category: string
  video_stale: boolean
  video_note: string
  video_model: string
  pending_parts: GenerationPart[]
  feedback: string
  review_note: string
  error: string
  model: string
  image_model: string
  content_id: string | null
  /** Knowledge passages the copy was grounded on (RAG); empty for older items. */
  references?: Array<{ title: string, excerpt: string, source: string }>
  options: Partial<Record<GenerationPart | 'voice' | 'music' | 'ai_video', boolean>>
  media: MediaRef[]
  product: ProductSummary
  reviewed_at: string | null
  created_at: string
  updated_at: string
}

export interface GenerationBatch {
  id: string
  name: string
  created_at: string
  instructions: string
  options: Record<GenerationPart, boolean>
  image_model: string
  counts: Partial<Record<ItemStatus, number>>
  total: number
}

export interface PublishSummary { platform: PublishPlatform, status: RecordStatus, workflow: Workflow, url: string }

export interface ProductContent {
  id: string
  item_id: string
  title: string
  body: string
  listing_title: string
  listing_description: string
  category: string
  approved_at: string
  approved_by: string
  media: MediaRef[]
  has_video: boolean
  product: ProductSummary
  publishes: PublishSummary[]
}

export interface PublishRecord {
  id: string
  workflow: Workflow
  platform: PublishPlatform
  status: RecordStatus
  content_id: string
  product_id: string
  product_name: string
  title: string
  price: string | null
  shipping_fee: string | null
  condition: string
  location: string
  category: string
  account_label: string
  external_url: string
  external_id: string
  error: string
  attempt: number
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface CatalogOverview {
  products: number
  items: Partial<Record<ItemStatus, number>>
  contents: number
  records: Partial<Record<Workflow, Partial<Record<RecordStatus, number>>>>
}

export interface Page<T> { items: T[], total: number }

export interface ImageModelList {
  default: string
  configured: string[]
  models: Array<{ id: string, name: string, configured: boolean }>
  error: string
}

export interface BatchRequest {
  product_ids: string[]
  with_copy: boolean
  with_images: boolean
  with_video: boolean
  image_count: number
  image_model: string
  video_voice: boolean
  video_music: boolean
  video_mode: 'slideshow' | 'ai'
  ai_clip_count: number
  instructions: string
}

export interface ListingRequest {
  content_ids: string[]
  condition: string
  shipping_fee: number
  location: string
  prices: Record<string, number>
  include_generated_images: boolean
}

const baseURL = import.meta.env.VITE_CONTENT_API_URL || '/content/api'
const { request } = createAgentClient(baseURL)
const base = '/api/v1/catalog'
const headers = { 'X-Content-Config-Request': '1' }

export const platformLabels: Record<PublishPlatform, string> = {
  xianyu: '闲鱼',
  douyin: '抖音',
  xiaohongshu: '小红书',
  kuaishou: '快手',
}

export const itemStatusLabels: Record<ItemStatus, string> = {
  queued: '排队中',
  running: '生成中',
  review: '待审核',
  approved: '已入库',
  rejected: '已驳回',
  failed: '生成失败',
}

export const recordStatusLabels: Record<RecordStatus, string> = {
  queued: '排队中',
  running: '执行中',
  submitting: '提交中',
  succeeded: '成功',
  failed: '失败',
  unknown: '待核查',
}

export const partLabels: Record<GenerationPart, string> = { copy: '标题和文案', images: '宣传图片', video: '宣传视频' }

export const catalogMedia = {
  productImage: (productId: string, imageId: string, thumb = false) =>
    contentMediaUrl(`${base}/products/${productId}/images/${imageId}${thumb ? '?size=thumb' : ''}`, baseURL),
  item: (itemId: string, mediaId: string) => contentMediaUrl(`${base}/items/${itemId}/media/${mediaId}`, baseURL),
  content: (contentId: string, mediaId: string) => contentMediaUrl(`${base}/contents/${contentId}/media/${mediaId}`, baseURL),
}

export const catalogApi = {
  overview: () => request<CatalogOverview>({ url: `${base}/overview` }),

  products: (params: { q?: string, page?: number, page_size?: number }) =>
    request<Page<Product>>({ url: `${base}/products`, params }),
  createProduct: (data: ProductInput) => request<Product>({ url: `${base}/products`, method: 'POST', headers, data }),
  updateProduct: (id: string, data: ProductInput) => request<Product>({ url: `${base}/products/${id}`, method: 'PUT', headers, data }),
  deleteProduct: (id: string) => request<void>({ url: `${base}/products/${id}`, method: 'DELETE', headers }),
  uploadImages: (id: string, files: File[]) => {
    const form = new FormData()
    files.forEach(file => form.append('files', file))
    return request<Product>({ url: `${base}/products/${id}/images`, method: 'POST', headers, data: form, timeout: 300_000 })
  },
  deleteImage: (id: string, imageId: string) =>
    request<Product>({ url: `${base}/products/${id}/images/${imageId}`, method: 'DELETE', headers }),
  orderImages: (id: string, ids: string[]) =>
    request<Product>({ url: `${base}/products/${id}/images/order`, method: 'PUT', headers, data: { ids } }),

  imageModels: (refresh = false) => request<ImageModelList>({ url: `${base}/image-models`, params: refresh ? { refresh: true } : {} }),
  createBatch: (data: BatchRequest) =>
    request<{ id: string, name: string, total: number }>({ url: `${base}/batches`, method: 'POST', headers, data }),
  batches: () => request<{ items: GenerationBatch[] }>({ url: `${base}/batches` }),
  items: (params: { status?: string, batch_id?: string, product_id?: string, page?: number, page_size?: number }) =>
    request<Page<GenerationItem> & { counts: Partial<Record<ItemStatus, number>> }>({ url: `${base}/items`, params }),
  item: (id: string) => request<GenerationItem>({ url: `${base}/items/${id}` }),
  editItem: (id: string, data: { title: string, body: string, listing_title: string, listing_description: string, category: string }) =>
    request<GenerationItem>({ url: `${base}/items/${id}`, method: 'PUT', headers, data }),
  regenerate: (id: string, parts: GenerationPart[], feedback: string) =>
    request<GenerationItem>({ url: `${base}/items/${id}/regenerate`, method: 'POST', headers, data: { parts, feedback } }),
  approve: (id: string, revision: number) =>
    request<{ id: string, status: ItemStatus, content_id: string }>({ url: `${base}/items/${id}/approve`, method: 'POST', headers, data: { revision } }),
  approveMany: (ids: string[]) =>
    request<{ approved: string[], skipped: Array<{ id: string, reason: string }> }>({ url: `${base}/items/approve`, method: 'POST', headers, data: { ids } }),
  reject: (id: string, note: string) =>
    request<{ id: string, status: ItemStatus }>({ url: `${base}/items/${id}/reject`, method: 'POST', headers, data: { note } }),

  contents: (params: { q?: string, product_id?: string, page?: number, page_size?: number }) =>
    request<Page<ProductContent>>({ url: `${base}/contents`, params }),
  withdrawContent: (id: string) => request<void>({ url: `${base}/contents/${id}`, method: 'DELETE', headers }),

  publishListing: (data: ListingRequest) =>
    request<{ items: string[], total: number }>({ url: `${base}/publish/listing`, method: 'POST', headers, data }),
  publishPromotion: (data: { content_ids: string[], platforms: PromotionPlatform[] }) =>
    request<{ items: string[], total: number }>({ url: `${base}/publish/promotion`, method: 'POST', headers, data }),
  records: (params: { workflow?: Workflow, status?: string, platform?: string, page?: number, page_size?: number }) =>
    request<Page<PublishRecord> & { counts: Partial<Record<RecordStatus, number>> }>({ url: `${base}/records`, params }),
  retryRecord: (id: string) => request<PublishRecord>({ url: `${base}/records/${id}/retry`, method: 'POST', headers }),
  resolveRecord: (id: string, status: 'succeeded' | 'failed', externalUrl = '') =>
    request<PublishRecord>({ url: `${base}/records/${id}/resolve`, method: 'POST', headers, data: { status, external_url: externalUrl } }),
}
