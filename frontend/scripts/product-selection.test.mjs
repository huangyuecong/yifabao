import assert from 'node:assert/strict'
import { replaceProductKnowledgeSelection } from '../src/views/agent/content/productSelection.ts'

const resources = [
  {
    id: 'product-a-item',
    resource_entry_id: 'product-a',
    resource_kind: 'product',
    metadata: { candidate_id: 'candidate-a' },
  },
  {
    id: 'product-b-item',
    resource_entry_id: 'product-b',
    resource_kind: 'product',
    metadata: { candidate_id: 'candidate-b' },
  },
  {
    id: 'image-a-item',
    resource_entry_id: 'image-a',
    resource_kind: 'image',
    parent_id: 'product-a',
    metadata: {},
  },
  {
    id: 'brief-a-item',
    resource_entry_id: 'brief-a',
    resource_kind: 'document',
    metadata: { related_product_ids: ['candidate-a'] },
  },
  {
    id: 'brand-guide-item',
    resource_entry_id: 'brand-guide',
    resource_kind: 'document',
    metadata: {},
  },
]

assert.deepEqual(
  replaceProductKnowledgeSelection(
    resources,
    ['product-a', 'image-a', 'brief-a', 'brand-guide'],
    'product-a',
    'product-b',
  ),
  ['image-a', 'brief-a', 'brand-guide', 'product-b'],
)
assert.deepEqual(
  replaceProductKnowledgeSelection(resources, ['brand-guide'], null, 'product-b'),
  ['brand-guide', 'product-b'],
)
assert.deepEqual(
  replaceProductKnowledgeSelection(resources, ['product-a', 'brand-guide'], 'product-a', ''),
  ['brand-guide'],
)
assert.deepEqual(
  replaceProductKnowledgeSelection(
    resources,
    ['product-a-item', 'product-b', 'brand-guide'],
    'product-a-item',
    'product-b-item',
  ),
  ['brand-guide', 'product-b-item'],
)

console.info('Product selection replacement unit contract passed.')
