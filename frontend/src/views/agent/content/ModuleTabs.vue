<script setup lang="ts">
import type { Workflow } from '@/api/agent/catalog'
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

/* 商品上架 and 内容宣传 are each one module with two tabs: the publishing page and its records. */
const props = defineProps<{ workflow: Workflow }>()
const route = useRoute()
const router = useRouter()

const MODULES: Record<Workflow, { title: string, tabs: Array<{ label: string, path: string }> }> = {
  listing: {
    title: '商品上架',
    tabs: [
      { label: '闲鱼上架', path: '/agent/content/listing' },
      { label: '上架记录', path: '/agent/content/listing/records' },
    ],
  },
  promotion: {
    title: '内容宣传',
    tabs: [
      { label: '平台发布', path: '/agent/content/promotion' },
      { label: '发布历史', path: '/agent/content/promotion/records' },
    ],
  },
}
const module = computed(() => MODULES[props.workflow])
</script>

<template>
  <div class="module-tabs">
    <span class="module-name">{{ module.title }}</span>
    <nav role="tablist" :aria-label="module.title">
      <button
        v-for="tab in module.tabs"
        :key="tab.path"
        type="button"
        role="tab"
        :aria-selected="route.path === tab.path"
        :class="{ active: route.path === tab.path }"
        @click="route.path !== tab.path && router.push(tab.path)"
      >
        {{ tab.label }}
      </button>
    </nav>
    <div class="module-actions"><slot /></div>
  </div>
</template>

<style scoped>
.module-tabs {
  display: flex;
  align-items: flex-end;
  gap: 20px;
  min-height: 44px;
  border-bottom: 1px solid var(--xi-semantic-color-border-subtle);
}
.module-name {
  padding-bottom: 10px;
  color: var(--xi-semantic-color-text-primary);
  font-size: 20px;
  font-weight: 600;
  line-height: 28px;
}
nav {
  display: flex;
  gap: 4px;
}
button {
  position: relative;
  height: 40px;
  padding: 0 14px;
  color: var(--xi-semantic-color-text-secondary);
  font: inherit;
  font-size: 14px;
  background: none;
  border: 0;
  cursor: pointer;
}
button:hover {
  color: var(--xi-semantic-color-text-primary);
}
button.active {
  color: var(--xi-semantic-color-action-primary-default);
  font-weight: 600;
}
button.active::after {
  position: absolute;
  right: 10px;
  bottom: -1px;
  left: 10px;
  height: 2px;
  content: '';
  background: var(--xi-semantic-color-action-primary-default);
  border-radius: 1px;
}
button:focus-visible {
  outline: 2px solid var(--xi-semantic-color-action-primary-default);
  outline-offset: -2px;
}
.module-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  padding-bottom: 8px;
}
@media (width < 700px) {
  .module-tabs {
    flex-wrap: wrap;
    gap: 4px 12px;
  }
  .module-name {
    width: 100%;
    padding-bottom: 0;
  }
  .module-actions {
    padding-bottom: 4px;
  }
}
</style>
