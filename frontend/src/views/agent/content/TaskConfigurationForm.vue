<script setup lang="ts">
import type { ContentLength, LibraryItem } from '@/api/agent/content'
import { computed } from 'vue'
import ContentKnowledgePicker from './ContentKnowledgePicker.vue'
import ContentKnowledgeSelectionList from './ContentKnowledgeSelectionList.vue'
import ContentProductPicker from './ContentProductPicker.vue'
import { replaceProductKnowledgeSelection } from './productSelection'

export interface TaskFormValue {
  name: string
  topic: string
  product: string
  product_resource_id: string
  knowledge_resource_ids: string[]
  keywords: string
  audience: string
  content_type: string
  style: string
  length: ContentLength
  requirements: string
}

const props = withDefaults(
  defineProps<{
    modelValue: TaskFormValue
    contentTypeOptions: string[]
    styleOptions: string[]
    resources: LibraryItem[]
    showLength?: boolean
    showRequirements?: boolean
  }>(),
  {
    showLength: true,
    showRequirements: true,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: TaskFormValue]
  'resourcesRefreshed': [value: LibraryItem[]]
}>()

function update<K extends keyof TaskFormValue>(key: K, value: TaskFormValue[K]) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}

function removeKnowledgeResource(id: string) {
  update(
    'knowledge_resource_ids',
    props.modelValue.knowledge_resource_ids.filter(value => value !== id),
  )
}

function updateProduct(name: string, resourceId: string) {
  const nextKnowledgeResourceIds = replaceProductKnowledgeSelection(
    props.resources,
    props.modelValue.knowledge_resource_ids,
    props.modelValue.product_resource_id,
    resourceId,
  )
  // Product name, canonical id and its implicit knowledge selection form one
  // transaction. Emitting them separately would spread the same stale prop
  // snapshot twice and could leave the old product name/references behind.
  emit('update:modelValue', {
    ...props.modelValue,
    product: name,
    product_resource_id: resourceId,
    knowledge_resource_ids: nextKnowledgeResourceIds,
  })
}

const lengthOptions: Array<{ label: string, value: ContentLength }> = [
  { label: '短', value: '短' },
  { label: '中', value: '中' },
  { label: '长', value: '长' },
]
const knowledgeSelectionLimit = 20
const selectedKnowledgeCount = computed(
  () => new Set(props.modelValue.knowledge_resource_ids).size,
)
</script>

<template>
  <ElForm label-position="top">
    <div class="task-form-grid">
      <ElFormItem label="任务名称" required>
        <ElInput
          :model-value="modelValue.name"
          @update:model-value="update('name', String($event))"
        />
      </ElFormItem>
      <ElFormItem label="内容类型">
        <ElSelect
          :model-value="modelValue.content_type"
          filterable
          allow-create
          @update:model-value="update('content_type', String($event))"
        >
          <ElOption
            v-for="item in contentTypeOptions"
            :key="item"
            :label="item"
            :value="item"
          />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="表达风格">
        <ElSelect
          :model-value="modelValue.style"
          filterable
          allow-create
          @update:model-value="update('style', String($event))"
        >
          <ElOption
            v-for="item in styleOptions"
            :key="item"
            :label="item"
            :value="item"
          />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="主题" required>
        <ElInput
          :model-value="modelValue.topic"
          placeholder="可填写创作主题，智能标题将结合该主题生成"
          @update:model-value="update('topic', String($event))"
        />
      </ElFormItem>
      <ElFormItem label="产品名称（可选）">
        <ContentProductPicker
          :show-preview="false"
          :model-value="modelValue.product"
          :product-resource-id="modelValue.product_resource_id"
          :resources="resources"
          @change="updateProduct"
        />
      </ElFormItem>
      <ElFormItem label="目标人群" required>
        <ElInput
          :model-value="modelValue.audience"
          @update:model-value="update('audience', String($event))"
        />
      </ElFormItem>
      <ElFormItem label="关键词">
        <ElInput
          :model-value="modelValue.keywords"
          placeholder="多个关键词用逗号分隔"
          @update:model-value="update('keywords', String($event))"
        />
      </ElFormItem>
      <ElFormItem v-if="showLength" label="内容长度">
        <ElSelect
          :model-value="modelValue.length"
          @update:model-value="update('length', $event as ContentLength)"
        >
          <ElOption
            v-for="option in lengthOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </ElSelect>
      </ElFormItem>
      <ElFormItem class="knowledge-field">
        <template #label>
          引用知识库资料与素材（可多选，已选 {{ selectedKnowledgeCount }} / {{ knowledgeSelectionLimit }} 项）
        </template>
        <ContentKnowledgePicker
          :model-value="modelValue.knowledge_resource_ids"
          :resources="resources"
          :max-selection="knowledgeSelectionLimit"
          :show-selection-summary="false"
          description="可同时选择多个产品资料、企业文化、品牌资料、Logo、图片和内容规范；生成时将优先参考所选内容。"
          @resources-refreshed="emit('resourcesRefreshed', $event)"
          @update:model-value="update('knowledge_resource_ids', $event)"
        />
      </ElFormItem>
      <ContentKnowledgeSelectionList
        v-if="modelValue.knowledge_resource_ids.length"
        class="selected-knowledge-row"
        :model-value="modelValue.knowledge_resource_ids"
        :resources="resources"
        @remove="removeKnowledgeResource"
      />
      <ElFormItem v-if="showRequirements" label="要求" class="full-field">
        <ElInput
          :model-value="modelValue.requirements"
          type="textarea"
          :rows="5"
          placeholder="可粘贴已有标题/文案，或直接输入需求"
          @update:model-value="update('requirements', String($event))"
        />
      </ElFormItem>
    </div>
  </ElForm>
</template>

<style scoped>
.knowledge-field {
  grid-column: 3;
}

.selected-knowledge-row {
  grid-column: 1 / -1;
  margin-bottom: var(--xi-component-content-gap-md);
}
</style>
