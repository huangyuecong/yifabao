<script setup lang="ts">
import type { LibraryItem } from '@/api/agent/content'
import { computed } from 'vue'
import { categoryLabel, resourceKindLabel } from './contentResourceCatalog'

const props = withDefaults(defineProps<{
  modelValue: string[]
  resources: LibraryItem[]
  preserveUnknown?: boolean
}>(), {
  preserveUnknown: true,
})

const emit = defineEmits<{
  remove: [id: string]
}>()

function resourceId(item: LibraryItem) {
  return item.resource_entry_id || item.id
}

const resourceById = computed(() => new Map(
  props.resources
    .filter(item => item.active !== false)
    .map(item => [resourceId(item), item]),
))
const selectedResources = computed(() => props.modelValue
  .map(id => resourceById.value.get(id))
  .filter((item): item is LibraryItem => Boolean(item)),
)
const unavailableSelectionCount = computed(() => props.preserveUnknown
  ? props.modelValue.filter(id => !resourceById.value.has(id)).length
  : 0,
)
</script>

<template>
  <div class="knowledge-selection-summary">
    <div
      v-if="selectedResources.length"
      class="selected-knowledge-list"
      aria-label="已选知识库资料"
    >
      <article v-for="item in selectedResources" :key="resourceId(item)">
        <div>
          <strong :title="item.name">{{ item.name }}</strong>
          <span>{{ resourceKindLabel(item.resource_kind) }} ·
            {{ categoryLabel(item.category, item) }}</span>
        </div>
        <ElButton link :aria-label="`移除${item.name}`" @click="emit('remove', resourceId(item))">
          移除
        </ElButton>
      </article>
    </div>
    <p v-if="unavailableSelectionCount" class="unavailable-selection">
      另有 {{ unavailableSelectionCount }} 项历史引用当前不可见，保存时将继续保留。
    </p>
  </div>
</template>

<style scoped>
.knowledge-selection-summary,
.selected-knowledge-list {
  display: grid;
  width: 100%;
  gap: var(--xi-component-content-gap-sm);
}

.selected-knowledge-list {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.selected-knowledge-list article {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: var(--xi-component-content-gap-xs);
  padding: var(--xi-component-content-gap-xs) var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-background-brand-subtle);
  border: var(--xi-component-content-border-brand-subtle);
  border-radius: var(--xi-component-content-radius-control);
}

.selected-knowledge-list article > div {
  display: grid;
  min-width: 0;
  gap: var(--xi-component-content-gap-xxs);
}

.selected-knowledge-list strong,
.selected-knowledge-list span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.selected-knowledge-list span,
.unavailable-selection {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: var(--xi-semantic-typography-caption-font-size);
}

.unavailable-selection {
  margin: 0;
}

@media (max-width: 900px) {
  .selected-knowledge-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .selected-knowledge-list {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
