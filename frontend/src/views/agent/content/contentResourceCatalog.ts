import type { LibraryItem, LibraryResourceKind } from '@/api/agent/content'

const categoryLabels: Record<string, string> = {
  enterprise: '企业资料',
  brand: '品牌资料',
  culture: '品牌文化',
  product: '产品资料',
  product_image: '产品图片',
  brand_image: '品牌图片',
  brand_logo: '品牌标识',
  logo: '品牌标识',
  image: '图片素材',
  video: '视频素材',
  audio: '音频素材',
  material: '宣传素材',
  visual: '视觉素材',
  spec: '内容规范',
  specification: '内容规范',
}

const resourceKindLabels: Record<string, string> = {
  document: '文档',
  product: '产品资料',
  logo: 'Logo',
  image: '图片',
  video: '视频',
  audio: '音频',
  spec: '内容规范',
}

export function resourceKindLabel(value: string) {
  return resourceKindLabels[value] || '其他资料'
}

export function metadataText(item: LibraryItem, ...keys: string[]) {
  for (const key of keys) {
    const value = item.metadata?.[key]
    if (typeof value === 'string' && value.trim()) {
      return value.trim()
    }
  }
  return ''
}

export function metadataIds(item: LibraryItem, key: string) {
  const value = item.metadata?.[key]
  return Array.isArray(value) ? value.filter(item => typeof item === 'string').map(String) : []
}

export function categoryLabel(value: string, item?: LibraryItem) {
  const serviceLabel = item
    ? metadataText(item, 'category_label', 'category_name', 'classification_name')
    : ''
  if (serviceLabel) {
    return serviceLabel
  }
  const normalized = value.trim()
  if (!normalized) {
    return '未分类'
  }
  if (categoryLabels[normalized]) {
    return categoryLabels[normalized]
  }
  return normalized
}

export function resourceSummary(item: LibraryItem) {
  const firstLine = item.text_content
    .split(/\r?\n/u)
    .map(value => value.trim())
    .find(Boolean)
  return firstLine || item.notes || '暂无内容摘要'
}

export function resourceSearchText(item: LibraryItem) {
  return [
    item.name,
    item.category,
    categoryLabel(item.category, item),
    item.text_content,
    item.notes,
    ...(item.tags ?? []),
    metadataText(item, 'series_name', 'product_name', 'display_name'),
  ]
    .join(' ')
    .toLocaleLowerCase('zh-CN')
}

export function findParentResource(item: LibraryItem, resources: LibraryItem[]) {
  if (!item.parent_id) {
    return undefined
  }
  return resources.find(
    candidate => candidate.id === item.parent_id || candidate.resource_entry_id === item.parent_id,
  )
}

export function productGroupLabel(item: LibraryItem, resources: LibraryItem[]) {
  const seriesName = metadataText(item, 'series_name', 'series_label')
  if (seriesName) {
    return seriesName
  }
  const seriesFact = item.facts?.find(
    fact => typeof fact === 'string' && fact.startsWith('产品系列：'),
  )
  if (typeof seriesFact === 'string') {
    const factValue = seriesFact.replace(/^产品系列：/u, '').trim()
    if (factValue) {
      return factValue
    }
  }
  const seriesTag = item.tags?.find(tag => tag.endsWith('系列'))
  if (seriesTag) {
    return seriesTag
  }
  return findParentResource(item, resources)?.name || categoryLabel(item.category, item)
}

export function relatedMedia(resource: LibraryItem, resources: LibraryItem[]) {
  if (['image', 'logo', 'video', 'audio'].includes(resourceMediaKind(resource))) {
    return [resource]
  }

  const mediaItemIds = new Set(metadataIds(resource, 'media_library_item_ids'))
  const mediaEntryIds = new Set(metadataIds(resource, 'media_entry_ids'))
  const candidateId = metadataText(resource, 'candidate_id', 'external_id')
  const productEntryIds = new Set(
    [resource.id, resource.resource_entry_id].filter((value): value is string => Boolean(value)),
  )

  return resources.filter((item) => {
    if (!['image', 'logo', 'video', 'audio'].includes(item.resource_kind)) {
      return false
    }
    if (
      mediaItemIds.has(item.id)
      || (item.resource_entry_id && mediaEntryIds.has(item.resource_entry_id))
    ) {
      return true
    }
    if (item.parent_id && productEntryIds.has(item.parent_id)) {
      return true
    }
    const relatedIds = metadataIds(item, 'related_product_ids')
    return Boolean(candidateId && relatedIds.includes(candidateId))
  })
}

export function previewResource(resource: LibraryItem, resources: LibraryItem[]) {
  if (['image', 'logo'].includes(resourceMediaKind(resource))) {
    return resource
  }
  return relatedMedia(resource, resources).find(item =>
    ['image', 'logo'].includes(resourceMediaKind(item)),
  )
}

export function isProductResource(item: LibraryItem | undefined): boolean {
  return Boolean(item && (item.resource_kind === 'product'
    || ['product', '产品资料'].includes(item.category)
    || item.metadata?.resource_kind === 'product'))
}

export function resourceMediaKind(item: LibraryItem | undefined) {
  if (!item) {
    return ''
  }
  const mime = metadataText(item, 'media_type')
  return mime.startsWith('image/') ? 'image' : item.resource_kind
}

export function categoryForResourceKind(kind: LibraryResourceKind) {
  return kind === 'document' ? 'enterprise' : kind
}
