<script setup lang="ts">
import { nextTick } from 'vue'

/* The workbench's one pagination bar (the style first used on 历史任务): total, page size,
 * pages and a jump box, right-aligned under a divider. `compact` drops the size picker
 * and jump box for narrow columns. */
withDefaults(defineProps<{ total: number, pageSizes?: number[], compact?: boolean }>(), {
  pageSizes: () => [10, 20, 50, 100],
  compact: false,
})
const emit = defineEmits<{ change: [] }>()
const page = defineModel<number>('page', { required: true })
const pageSize = defineModel<number>('pageSize', { required: true })

// A size change can also move the current page; report both as one change.
let pending = false
function notify() {
  if (pending) {
    return
  }
  pending = true
  void nextTick(() => {
    pending = false
    emit('change')
  })
}

function changeSize() {
  page.value = 1
  notify()
}
</script>

<template>
  <div v-if="total > 0" class="wb-pagination">
    <ElPagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      background
      :total="total"
      :page-sizes="pageSizes"
      :pager-count="compact ? 5 : 7"
      :layout="compact ? 'total, prev, pager, next' : 'total, sizes, prev, pager, next, jumper'"
      @current-change="notify"
      @size-change="changeSize"
    />
  </div>
</template>

<style scoped>
.wb-pagination {
  display: flex;
  justify-content: flex-end;
  padding: 12px 16px;
  border-top: 1px solid var(--xi-semantic-color-border-subtle);
}
.wb-pagination :deep(.el-pagination) {
  flex-wrap: wrap;
  justify-content: flex-end;
  row-gap: 8px;
}
</style>
