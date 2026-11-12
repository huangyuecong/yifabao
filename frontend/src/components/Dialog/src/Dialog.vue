<script lang="ts" setup>
import { isNumber } from '@/utils/is'
import { propTypes } from '@/utils/propTypes'

defineOptions({ name: 'Dialog' })

const props = defineProps({
  showAction: propTypes.bool.def(true),
  modelValue: propTypes.bool.def(false),
  title: propTypes.string.def('Dialog'),
  fullscreen: propTypes.bool.def(false),
  width: propTypes.oneOfType([String, Number]).def('40%'),
  scroll: propTypes.bool.def(false), // 是否开启滚动条。如果是的话，按照 maxHeight 设置最大高度
  maxHeight: propTypes.oneOfType([String, Number]).def('400px'),
  draggable: propTypes.bool.def(true),
  destroyOnClose: propTypes.bool.def(false),
})

const emit = defineEmits(['cancel', 'confirm', 'update:modelValue'])

const slots = useSlots()

const getBindValue = computed(() => {
  const delArr: string[] = ['fullscreen', 'title', 'maxHeight', 'appendToBody']
  const attrs = useAttrs()
  const obj = { ...attrs, ...props }
  for (const key in obj) {
    if (delArr.includes(key)) {
      delete obj[key]
    }
  }
  return obj
})

const isFullscreen = ref(false)

const toggleFull = () => {
  isFullscreen.value = !unref(isFullscreen)
}

const dialogHeight = ref(isNumber(props.maxHeight) ? `${props.maxHeight}px` : props.maxHeight)

const dialogVisible = ref(false)

watch(
  () => props.modelValue,
  (val) => {
    dialogVisible.value = val
  },

)
watch(
  () => unref(dialogVisible),
  (val) => {
    emit('update:modelValue', val)
  },

)

const handleCancel = () => {
  emit('cancel')
  dialogVisible.value = false
}
const handleConfirm = () => {
  emit('confirm')
}
watch(
  () => isFullscreen.value,
  async (val: boolean) => {
    await nextTick()
    if (val) {
      const windowHeight = document.documentElement.offsetHeight
      dialogHeight.value = `${windowHeight - 55 - 60 - (slots.footer ? 63 : 0)}px`
    }
    else {
      dialogHeight.value = isNumber(props.maxHeight) ? `${props.maxHeight}px` : props.maxHeight
    }
  },
  {
    immediate: true,
  },
)

const dialogStyle = computed(() => {
  return {
    height: unref(dialogHeight),
  }
})
</script>

<template>
  <ElDialog
    v-bind="getBindValue"
    v-model="dialogVisible"
    :close-on-click-modal="true"
    :fullscreen="isFullscreen"
    :width="width"
    :destroy-on-close="props.destroyOnClose"
    lock-scroll
    :draggable="props.draggable"
    class="com-dialog"
    :show-close="false"
  >
    <template #header="{ close }">
      <div class="relative h-54px flex items-center justify-between pl-15px pr-15px">
        <slot name="title">
          {{ title }}
        </slot>
        <div class="absolute right-15px top-[50%] h-54px flex translate-y-[-50%] items-center justify-between">
          <Icon
            v-if="fullscreen"
            class="is-hover mr-10px cursor-pointer"
            :icon="isFullscreen ? 'radix-icons:exit-full-screen' : 'radix-icons:enter-full-screen'"
            color="var(--el-color-info)"
            hover-color="var(--el-color-primary)"
            @click="toggleFull"
          />
          <Icon
            class="is-hover cursor-pointer"
            icon="ep:close"
            hover-color="var(--el-color-primary)"
            color="#fff"
            @click="close"
          />
        </div>
      </div>
    </template>

    <ElScrollbar v-if="scroll" :style="dialogStyle">
      <slot />
    </ElScrollbar>
    <slot v-else />
    <template #footer>
      <slot name="footer">
        <div v-if="props.showAction" class="mb-16px mr-28px">
          <el-button @click="handleCancel">取消</el-button>
          <el-button type="primary" @click="handleConfirm">确认</el-button>
        </div>
      </slot>
    </template>
  </ElDialog>
</template>

<style lang="scss">
.com-dialog {
  .#{$elNamespace}-overlay-dialog {
    display: flex;
    justify-content: center;
    align-items: center;
  }

  padding: 0 !important;
  border-radius: 6px;

  .#{$elNamespace}-dialog {
    margin: 0 !important;

    &__header {
      height: 54px;
      padding: 0;
      padding-left: 20px;
      margin-right: 0 !important;
      border-bottom: 1px solid var(--el-border-color);
      background-color: var(--el-color-primary);
      color: #fff;
      font-weight: 500;
      font-size: 18px;
      border-radius: 6px;
      border-bottom-left-radius: 0;

      border-bottom-right-radius: 0;
    }

    &__body {
      padding: 15px !important;
    }

    &__footer {
      padding-bottom: 8px;
    }

    &__headerbtn {
      top: 0;
    }
  }
}
</style>
