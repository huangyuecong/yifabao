import type { ItemStatus, PublishPlatform, RecordStatus } from '@/api/agent/catalog'
import { platformLabels } from '@/api/agent/catalog'
import { formatDate } from './contentWorkflow'

const itemTone: Record<ItemStatus, string> = {
  queued: '',
  running: 'is-primary',
  review: 'is-warning',
  approved: 'is-success',
  rejected: '',
  failed: 'is-danger',
}

const recordTone: Record<RecordStatus, string> = {
  queued: '',
  running: 'is-primary',
  submitting: 'is-primary',
  succeeded: 'is-success',
  failed: 'is-danger',
  unknown: 'is-warning',
}

export const itemStatusClass = (status: ItemStatus) => itemTone[status] ?? ''
export const recordStatusClass = (status: RecordStatus) => recordTone[status] ?? ''

export function yuan(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  return `¥${Number(value).toFixed(2)}`
}

export function shortTime(value?: string | null) {
  const text = formatDate(value)
  return text.length > 16 ? text.slice(0, 16) : text
}

export const platformInitial: Record<PublishPlatform, string> = { xianyu: '闲', douyin: '抖', xiaohongshu: '红', kuaishou: '快' }
export const platformName = (platform: PublishPlatform) => platformLabels[platform] ?? platform
