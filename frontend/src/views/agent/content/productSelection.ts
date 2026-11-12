import type { LibraryItem } from '@/api/agent/content'

function identityIds(item: LibraryItem | undefined) {
  if (!item) {
    return []
  }
  return [item.id, item.resource_entry_id]
    .map(value => String(value || '').trim())
    .filter(Boolean)
}

function resourceByAnyId(resources: LibraryItem[], id: string) {
  return resources.find(item => item.id === id || item.resource_entry_id === id)
}

/**
 * Replace the product portion of a task knowledge selection atomically.
 * Explicit non-product knowledge remains selected. Related images and documents
 * are resolved from the newly selected product by the service, so they must not
 * be removed here: the user may have selected one of them independently.
 */
export function replaceProductKnowledgeSelection(
  resources: LibraryItem[],
  knowledgeResourceIds: string[],
  previousProductId: string | null | undefined,
  nextProductId: string | null | undefined,
) {
  const previousProduct = previousProductId ? resourceByAnyId(resources, previousProductId) : undefined
  const staleIds = new Set([
    String(previousProductId || '').trim(),
    ...identityIds(previousProduct),
    ...resources
      .filter(item => item.resource_kind === 'product'
        || ['product', '产品资料'].includes(item.category)
        || item.metadata?.resource_kind === 'product')
      .flatMap(identityIds),
  ].filter(Boolean))
  const next = knowledgeResourceIds.filter(id => !staleIds.has(id))
  const normalizedNextProductId = String(nextProductId || '').trim()
  if (normalizedNextProductId && !next.includes(normalizedNextProductId)) {
    next.push(normalizedNextProductId)
  }
  return [...new Set(next)]
}
