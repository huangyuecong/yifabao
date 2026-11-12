<script setup lang="ts">
import type { ComplianceHit } from '@/api/agent/content'
import { computed } from 'vue'
import { complianceHitPresentation } from './contentWorkflow'

const props = defineProps<{
  hits: ComplianceHit[]
}>()

const items = computed(() => props.hits.map(hit => ({
  key: `${hit.rule_id}-${hit.field}-${hit.start}`,
  ...complianceHitPresentation(hit),
})))
</script>

<template>
  <ul class="compliance-hit-list">
    <li v-for="item in items" :key="item.key">
      <strong>{{ item.typeLabel }}：{{ item.match }}</strong>
      <small>{{ item.fieldLabel }}</small>
      <p class="compliance-snippet">
        <span>{{ item.prefix }}</span><mark>{{ item.match }}</mark><span>{{ item.suffix }}</span>
      </p>
      <small v-if="item.replacement">建议替换为：{{ item.replacement }}</small>
    </li>
  </ul>
</template>

<style scoped>
.compliance-hit-list {
  display: grid;
  gap: var(--xi-component-content-gap-sm);
  width: 100%;
  padding: 0;
  margin: 0;
  list-style: none;
}

.compliance-hit-list li {
  display: grid;
  gap: var(--xi-component-content-gap-xxs);
  padding: var(--xi-component-content-gap-sm);
  background: var(--xi-semantic-color-feedback-warning-background-subtle);
  border-radius: var(--xi-component-content-radius-control);
}

.compliance-hit-list small {
  color: var(--xi-semantic-color-text-secondary);
}

.compliance-snippet {
  margin: 0;
  line-height: 1.6;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.compliance-snippet mark {
  padding: 0 2px;
  color: var(--xi-semantic-color-feedback-danger-text);
  background: var(--xi-semantic-color-feedback-danger-background-subtle);
  border-radius: 2px;
  font-weight: 600;
}
</style>
