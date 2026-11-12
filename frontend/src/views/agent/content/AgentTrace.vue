<script setup lang="ts">
import type { AgentAction, AgentStep } from '@/api/agent/chat'
import { ArrowDown, ArrowRight, CircleCheckFilled, Loading, WarningFilled } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

/* What the operations-assistant Agent planned and did, live or from a saved reply. */
const props = defineProps<{
  plan: string[]
  steps: AgentStep[]
  actions?: AgentAction[]
  live?: boolean
}>()

const router = useRouter()
const open = ref(true)
const finished = computed(() => props.steps.filter(step => step.status !== 'running').length)
const failed = computed(() => props.steps.filter(step => step.status === 'error').length)
const heading = computed(() => {
  if (props.live) {
    if (props.steps.length) {
      return `运营助手执行中 · 第 ${props.steps.length} 步`
    }
    return props.plan.length ? '运营助手已制定计划，准备执行' : '运营助手正在规划'
  }
  return `运营助手 · 执行 ${props.steps.length} 步${failed.value ? `，${failed.value} 步未成功` : ''}`
})

function go(action: AgentAction) {
  void router.push(action.path)
}
</script>

<template>
  <section class="agent-trace" :class="{ live }" aria-label="运营助手执行过程">
    <button
      type="button"
      class="trace-head"
      :aria-expanded="open"
      @click="open = !open"
    >
      <ElIcon :class="{ 'is-loading': live }">
        <Loading v-if="live" />
        <ArrowDown v-else-if="open" />
        <ArrowRight v-else />
      </ElIcon>
      <strong>{{ heading }}</strong>
      <span v-if="!live && steps.length" class="trace-count">{{ finished }}/{{ steps.length }}</span>
    </button>

    <div v-show="open" class="trace-body">
      <div v-if="plan.length" class="trace-plan">
        <span class="trace-caption">计划</span>
        <ol>
          <li v-for="(item, index) in plan" :key="index">{{ item }}</li>
        </ol>
      </div>

      <ol v-if="steps.length" class="trace-steps">
        <li
          v-for="step in steps"
          :key="step.index"
          :class="`is-${step.status}`"
        >
          <span class="step-icon" aria-hidden="true">
            <ElIcon v-if="step.status === 'running'" class="is-loading"><Loading /></ElIcon>
            <ElIcon v-else-if="step.status === 'done'"><CircleCheckFilled /></ElIcon>
            <ElIcon v-else><WarningFilled /></ElIcon>
          </span>
          <div class="step-main">
            <p v-if="step.thought" class="step-thought">{{ step.thought }}</p>
            <p class="step-title">
              <strong>{{ step.label }}</strong>
              <em v-if="step.writes" class="step-tag">执行操作</em>
              <code v-if="step.args">{{ step.args }}</code>
            </p>
            <p v-if="step.summary" class="step-summary">{{ step.summary }}</p>
          </div>
        </li>
      </ol>
    </div>

    <div v-if="actions?.length" class="trace-actions">
      <ElButton
        v-for="action in actions"
        :key="action.path"
        :type="action.type === 'open_publish' ? 'primary' : 'default'"
        size="small"
        @click="go(action)"
      >
        {{ action.label }}
      </ElButton>
    </div>
  </section>
</template>

<style scoped>
.agent-trace {
  margin: 4px 0 10px;
  padding: 10px 12px;
  background: var(--wb-hover);
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 6px;
  font-size: 13px;
}
.agent-trace.live {
  border-color: var(--xi-semantic-color-feedback-info-border);
}
.trace-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 0;
  color: var(--xi-semantic-color-text-primary);
  font: inherit;
  text-align: left;
  background: none;
  border: 0;
  cursor: pointer;
}
.trace-head strong {
  font-weight: 600;
}
.trace-count {
  margin-left: auto;
  color: var(--xi-semantic-color-text-tertiary);
  font-variant-numeric: tabular-nums;
}
.trace-body {
  display: grid;
  gap: 10px;
  margin-top: 10px;
}
.trace-caption {
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
}
.trace-plan ol {
  margin: 4px 0 0;
  padding-left: 20px;
  color: var(--xi-semantic-color-text-secondary);
  line-height: 1.7;
}
.trace-steps {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.trace-steps li {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr);
  gap: 8px;
}
.step-icon {
  padding-top: 2px;
  color: var(--xi-semantic-color-text-tertiary);
}
.is-done .step-icon {
  color: var(--xi-semantic-color-feedback-success-default);
}
.is-error .step-icon {
  color: var(--xi-semantic-color-feedback-danger-default);
}
.step-main p {
  margin: 0;
  line-height: 1.6;
}
.step-thought {
  color: var(--xi-semantic-color-text-tertiary);
}
.step-title {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}
.step-title code {
  overflow: hidden;
  max-width: 100%;
  color: var(--xi-semantic-color-text-tertiary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.step-tag {
  padding: 0 6px;
  color: var(--xi-semantic-color-feedback-warning-text);
  font-size: 12px;
  font-style: normal;
  background: var(--xi-semantic-color-feedback-warning-background-subtle);
  border-radius: 3px;
}
.step-summary {
  color: var(--xi-semantic-color-text-secondary);
}
.is-error .step-summary {
  color: var(--xi-semantic-color-feedback-danger-text);
}
.trace-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.trace-actions .el-button {
  margin: 0;
}
</style>
