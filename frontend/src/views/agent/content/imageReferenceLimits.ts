import type { LibraryItem } from '@/api/agent/content'
import { isProductResource, relatedMedia, resourceMediaKind } from './contentResourceCatalog'

export interface ImageReferenceSelection {
  knowledgeResourceIds: string[]
  libraryAssetIds: string[]
  artifactIds: string[]
}

export function libraryResourceCanonicalId(item: LibraryItem) {
  return String(item.resource_entry_id || item.id).trim()
}

function resourceByAnyId(resources: LibraryItem[], id: string) {
  return resources.find(item => item.id === id || item.resource_entry_id === id)
}

function isVisualResource(item: LibraryItem | undefined): item is LibraryItem {
  return Boolean(item && ['image', 'logo'].includes(resourceMediaKind(item)))
}

export function productVisualReferenceResources(
  resources: LibraryItem[],
  knowledgeResourceIds: string[],
) {
  const items = new Map<string, LibraryItem>()
  for (const id of knowledgeResourceIds) {
    const product = resourceByAnyId(resources, id)
    if (!product || !isProductResource(product)) {
      continue
    }
    for (const item of relatedMedia(product, resources)) {
      if (isVisualResource(item)) {
        items.set(libraryResourceCanonicalId(item), item)
      }
    }
  }
  return [...items.values()]
}

export function imageReferenceCanonicalIds(
  resources: LibraryItem[],
  selection: ImageReferenceSelection,
) {
  const ids = new Set<string>()
  for (const id of selection.knowledgeResourceIds) {
    const item = resourceByAnyId(resources, id)
    if (isVisualResource(item)) {
      ids.add(`library:${libraryResourceCanonicalId(item)}`)
    }
  }
  for (const item of productVisualReferenceResources(resources, selection.knowledgeResourceIds)) {
    ids.add(`library:${libraryResourceCanonicalId(item)}`)
  }
  for (const id of selection.libraryAssetIds) {
    const item = resourceByAnyId(resources, id)
    if (isVisualResource(item)) {
      ids.add(`library:${libraryResourceCanonicalId(item)}`)
    }
  }
  for (const id of selection.artifactIds) {
    const normalized = String(id).trim()
    if (normalized) {
      ids.add(`artifact:${normalized}`)
    }
  }
  return ids
}

export function imageReferenceCount(
  resources: LibraryItem[],
  selection: ImageReferenceSelection,
) {
  return imageReferenceCanonicalIds(resources, selection).size
}
