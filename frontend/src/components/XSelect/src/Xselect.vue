<script setup lang="ts">
import type { ComponentSize } from 'element-plus'
import request from '@/config/axios'
import { computed, ref, watch } from '@/vue-export'

// 定义接口类型
interface OptionItem {
  [key: string]: any
  disabled?: boolean
}

interface Schema {
  label: string
  value: string
}

interface Props {
  modelValue?: string | number | string[] | number[]
  api?: string // 接口地址
  fetchData?: () => Schema[]
  schema?: Schema // 字段映射配置
  params?: Record<string, any> // 额外请求参数
  placeholder?: string
  clearable?: boolean
  multiple?: boolean
  disabled?: boolean
  size?: ComponentSize
  filterable?: boolean
  remote?: boolean // 是否启用远程搜索
  reserveKeyword?: boolean
  emptyText?: string
  immediate?: boolean // 是否立即加载数据
  skipLoadData?: () => boolean
  keywordName?: string
}

interface Emits {
  (e: 'update:modelValue', value: string | number | string[] | number[] | undefined): void
  (e: 'change', value: string | number | string[] | number[] | undefined): void
  (e: 'loadSuccess', data: OptionItem[]): void
  (e: 'loadError', error: Error): void
  (e: 'changeOption', value: OptionItem): void
}

// 定义 props 和 emits
const props = withDefaults(defineProps<Props>(), {
  schema: () => ({ label: 'label', value: 'value' }),
  placeholder: '请选择',
  clearable: true,
  multiple: false,
  disabled: false,
  size: 'default',
  filterable: false,
  remote: true,
  reserveKeyword: false,
  emptyText: '暂无数据',
  immediate: true,
  keywordName: 'name',
  params: () => ({}),
})

const emit = defineEmits<Emits>()

// 响应式数据
const loading = ref(false)
const options = ref<OptionItem[]>([])
const searchKeyword = ref('')

// 计算属性
const modelValue = computed({
  get: () => props.modelValue,
  set: value => emit('update:modelValue', value),
})

// 请求数据的方法
const fetchData = async (keyword?: string) => {
  if (typeof props.skipLoadData === 'function' && props.skipLoadData()) {
    return
  }

  if (typeof props.fetchData === 'function') {
    loading.value = true
    try {
      const data = await props.fetchData()
      options.value = data
      emit('loadSuccess', data)
    }
    catch (error) {
      console.error('AsyncSelect fetch error:', error)
      options.value = []
      emit('loadError', error as Error)
    }
    finally {
      loading.value = false
    }
    return
  }

  if (!props.api) {
    console.warn('AsyncSelect: api prop is required')
    return
  }

  loading.value = true

  try {
    // 构建请求参数
    const requestParams = {
      ...(typeof props.params === 'function' ? props.params(modelValue.value, options.value) : props.params),
    }

    if (keyword) {
      requestParams[props.keywordName] = keyword
    }

    const data = await request.get<OptionItem[]>({
      url: props.api,
      params: requestParams,
    })

    options.value = data
    emit('loadSuccess', data)
  }
  catch (error) {
    console.error('AsyncSelect fetch error:', error)
    options.value = []
    emit('loadError', error as Error)
  }
  finally {
    loading.value = false
  }
}

// 远程搜索处理
const handleRemoteSearch = (query: string) => {
  searchKeyword.value = query
  console.log(query)
  if (query !== '') {
    fetchData(query)
  }
  else if (props.immediate) {
    // 清空搜索时重新加载全部数据
    fetchData()
  }
  else {
    options.value = []
  }
}

// 选择变化处理
const handleChange = (value: string | number | string[] | number[] | undefined) => {
  emit('change', value)
  const option = options.value.find(i => i[props.schema.value] === value)
  if (option) {
    emit('changeOption', { ...option })
  }
}

// 下拉框显示/隐藏处理
const handleVisibleChange = (visible: boolean) => {
  if (visible && !props.remote && options.value.length === 0) {
    fetchData()
  }
}

// 监听 params 变化
watch(() => props.params, () => {
  if (props.api) {
    fetchData()
  }
}, { deep: true })

// 暴露方法给父组件
const refresh = () => {
  if (props.api) {
    fetchData()
  }
}

const clearOptions = () => {
  options.value = []
}

defineExpose({
  refresh,
  clearOptions,
  fetchData,
})
</script>

<template>
  <ElSelect
    v-model="modelValue"
    :loading="loading"
    :placeholder="placeholder"
    :clearable="clearable"
    :multiple="multiple"
    :disabled="disabled"
    :size="size"
    :filterable="filterable"
    :remote="remote"
    :remote-method="handleRemoteSearch"
    :reserve-keyword="reserveKeyword"
    v-bind="$attrs"
    @change="handleChange"
    @visible-change="handleVisibleChange"
  >
    <ElOption
      v-for="item in options"
      :key="item[schema.value]"
      :label="item[schema.label]"
      :value="item[schema.value]"
      :disabled="item.disabled"
    />

    <!-- 空数据提示 -->
    <template v-if="!loading && options.length === 0">
      <ElOption
        value=""
        :label="emptyText"
        disabled
      />
    </template>
  </ElSelect>
</template>
