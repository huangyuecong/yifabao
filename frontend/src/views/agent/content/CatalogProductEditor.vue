<script setup lang="ts">
import type { Product, ProductSpec } from '@/api/agent/catalog'
import { ArrowLeft, ArrowRight, Close, Plus } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { catalogApi, catalogMedia } from '@/api/agent/catalog'
import { friendlyContentError } from './contentWorkflow'

const props = defineProps<{ product: Product | null }>()
const emit = defineEmits<{ saved: [product: Product] }>()
const open = defineModel<boolean>({ required: true })

const form = reactive({ name: '', sku: '', category: '', price: undefined as number | undefined, description: '', specs: [] as ProductSpec[] })
const images = ref<Array<{ id: string }>>([])
const pending = ref<Array<{ file: File, url: string }>>([])
const saving = ref(false)
const picker = ref<HTMLInputElement>()
const productId = ref('')
const isNew = computed(() => !productId.value)
const imageCount = computed(() => images.value.length + pending.value.length)

function reset() {
  const product = props.product
  productId.value = product?.id ?? ''
  form.name = product?.name ?? ''
  form.sku = product?.sku ?? ''
  form.category = product?.category ?? ''
  form.price = product?.price == null ? undefined : Number(product.price)
  form.description = product?.description ?? ''
  form.specs = product?.specs.map(spec => ({ ...spec })) ?? [{ name: '规格', value: '' }]
  images.value = product?.images ?? []
  pending.value.forEach(item => URL.revokeObjectURL(item.url))
  pending.value = []
}

watch(open, (value) => {
  if (value) {
    reset()
  }
})

function pickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  const room = 9 - imageCount.value
  if (files.length > room) {
    ElMessage.warning(`每个商品最多 9 张图片，本次只添加前 ${Math.max(room, 0)} 张`)
  }
  pending.value.push(...files.slice(0, Math.max(room, 0)).map(file => ({ file, url: URL.createObjectURL(file) })))
}

function removePending(index: number) {
  URL.revokeObjectURL(pending.value[index].url)
  pending.value.splice(index, 1)
}

async function removeImage(id: string) {
  try {
    const product = await catalogApi.deleteImage(productId.value, id)
    images.value = product.images
    emit('saved', product)
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '删除图片失败'))
  }
}

async function moveImage(index: number, offset: number) {
  const order = images.value.map(image => image.id)
  const [moved] = order.splice(index, 1)
  order.splice(index + offset, 0, moved)
  try {
    const product = await catalogApi.orderImages(productId.value, order)
    images.value = product.images
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '调整顺序失败'))
  }
}

async function save() {
  if (!form.name.trim()) {
    ElMessage.warning('请填写商品名称')
    return
  }
  const specs = form.specs.map(spec => ({ name: spec.name.trim(), value: spec.value.trim() })).filter(spec => spec.name && spec.value)
  const data = {
    name: form.name.trim(),
    sku: form.sku.trim(),
    category: form.category.trim(),
    price: form.price ?? null,
    description: form.description.trim(),
    specs,
  }
  saving.value = true
  try {
    let product = isNew.value ? await catalogApi.createProduct(data) : await catalogApi.updateProduct(productId.value, data)
    productId.value = product.id
    if (pending.value.length) {
      product = await catalogApi.uploadImages(product.id, pending.value.map(item => item.file))
      pending.value.forEach(item => URL.revokeObjectURL(item.url))
      pending.value = []
    }
    images.value = product.images
    emit('saved', product)
    ElMessage.success('已保存')
    open.value = false
  }
  catch (error) {
    ElMessage.error(friendlyContentError(error, '保存失败'))
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <ElDrawer
    v-model="open"
    :title="isNew ? '新增商品' : '编辑商品'"
    size="560px"
    class="catalog-product-editor"
  >
    <ElForm label-position="top" @submit.prevent>
      <ElFormItem label="商品名称" required>
        <ElInput v-model="form.name" maxlength="120" placeholder="例如：臻品雅酿 500ml" />
      </ElFormItem>
      <div class="editor-row">
        <ElFormItem label="售价（元）">
          <ElInputNumber
            v-model="form.price"
            :min="0"
            :precision="2"
            :step="10"
            controls-position="right"
            placeholder="未定价"
          />
        </ElFormItem>
        <ElFormItem label="分类">
          <ElInput v-model="form.category" maxlength="64" placeholder="上架时用作闲鱼分类，如：白酒" />
        </ElFormItem>
        <ElFormItem label="货号">
          <ElInput v-model="form.sku" maxlength="64" placeholder="选填" />
        </ElFormItem>
      </div>

      <ElFormItem label="参数">
        <div class="spec-list">
          <div v-for="(spec, index) in form.specs" :key="index" class="spec-row">
            <ElInput v-model="spec.name" maxlength="40" placeholder="参数名" />
            <ElInput v-model="spec.value" maxlength="200" placeholder="参数值" />
            <ElButton
              :icon="Close"
              text
              aria-label="删除参数"
              @click="form.specs.splice(index, 1)"
            />
          </div>
          <ElButton
            :icon="Plus"
            text
            type="primary"
            :disabled="form.specs.length >= 30"
            @click="form.specs.push({ name: '', value: '' })"
          >
            添加参数
          </ElButton>
        </div>
      </ElFormItem>

      <ElFormItem :label="`图片（${imageCount}/9）`">
        <div class="image-list">
          <div v-for="(image, index) in images" :key="image.id" class="image-cell">
            <img :src="catalogMedia.productImage(productId, image.id, true)" alt="">
            <span v-if="index === 0" class="image-main">主图</span>
            <div class="image-actions">
              <button
                type="button"
                :disabled="index === 0"
                aria-label="前移"
                @click="moveImage(index, -1)"
              >
                <ElIcon><ArrowLeft /></ElIcon>
              </button>
              <button
                type="button"
                :disabled="index === images.length - 1"
                aria-label="后移"
                @click="moveImage(index, 1)"
              >
                <ElIcon><ArrowRight /></ElIcon>
              </button>
              <button type="button" aria-label="删除" @click="removeImage(image.id)"><ElIcon><Close /></ElIcon></button>
            </div>
          </div>
          <div v-for="(item, index) in pending" :key="item.url" class="image-cell is-pending">
            <img :src="item.url" alt="">
            <span class="image-main">待上传</span>
            <div class="image-actions">
              <button type="button" aria-label="移除" @click="removePending(index)"><ElIcon><Close /></ElIcon></button>
            </div>
          </div>
          <button
            v-if="imageCount < 9"
            type="button"
            class="image-add"
            @click="picker?.click()"
          >
            <ElIcon><Plus /></ElIcon><span>添加图片</span>
          </button>
          <input
            ref="picker"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            hidden
            @change="pickFiles"
          >
        </div>
      </ElFormItem>

      <ElFormItem label="描述">
        <ElInput
          v-model="form.description"
          type="textarea"
          :rows="4"
          maxlength="4000"
          show-word-limit
          placeholder="选填。真实的商品信息，生成文案时会作为依据"
        />
      </ElFormItem>
    </ElForm>
    <template #footer>
      <ElButton @click="open = false">取消</ElButton>
      <ElButton type="primary" :loading="saving" @click="save">保存</ElButton>
    </template>
  </ElDrawer>
</template>

<style scoped>
.editor-row {
  display: grid;
  grid-template-columns: 150px 1fr 1fr;
  gap: 12px;
}
.editor-row :deep(.el-input-number) {
  width: 100%;
}
.spec-list {
  display: grid;
  width: 100%;
  gap: 8px;
}
.spec-row {
  display: grid;
  grid-template-columns: 140px 1fr 32px;
  gap: 8px;
}
.spec-list > .el-button {
  justify-self: start;
  padding-left: 0;
}
.image-list {
  display: grid;
  width: 100%;
  grid-template-columns: repeat(auto-fill, 96px);
  gap: 8px;
}
.image-cell,
.image-add {
  position: relative;
  width: 96px;
  height: 96px;
  overflow: hidden;
  border: 1px solid var(--xi-semantic-color-border-subtle);
  border-radius: 4px;
}
.image-cell img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.image-cell.is-pending {
  border-style: dashed;
}
.image-main {
  position: absolute;
  top: 4px;
  left: 4px;
  padding: 0 4px;
  color: #fff;
  font-size: 11px;
  line-height: 16px;
  background: rgb(0 0 0 / 55%);
  border-radius: 2px;
}
.image-actions {
  position: absolute;
  right: 0;
  bottom: 0;
  left: 0;
  display: none;
  justify-content: center;
  gap: 2px;
  padding: 3px;
  background: rgb(0 0 0 / 55%);
}
.image-cell:hover .image-actions,
.image-cell:focus-within .image-actions {
  display: flex;
}
.image-actions button {
  display: grid;
  width: 24px;
  height: 22px;
  place-items: center;
  color: #fff;
  background: none;
  border: 0;
  cursor: pointer;
}
.image-actions button:disabled {
  opacity: 0.35;
  cursor: default;
}
.image-add {
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 4px;
  color: var(--xi-semantic-color-text-tertiary);
  font: inherit;
  font-size: 12px;
  background: var(--xi-semantic-color-background-page);
  border-style: dashed;
  cursor: pointer;
}
.image-add:hover {
  color: var(--xi-semantic-color-action-primary-default);
  border-color: var(--xi-semantic-color-action-primary-default);
}
@media (width < 600px) {
  .editor-row {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>
