<template>
    <Dialog
      v-model="dialogVisible"
      :max-height="maxHeight"
      :scroll="true"
      :width="600"
      :title="title"
      @confirm="handleConfirm"
      @cancel="reset"
    >
      <div v-if="!disabled" class="upload-file">
        <el-upload
          ref="uploadRef"
          v-model:file-list="fileList"
          :action="uploadUrl"
          :auto-upload="autoUpload"
          :before-upload="beforeUpload"
          :disabled="disabled"
          :drag="drag"
          :http-request="httpRequest"
          :limit="props.limit"
          :multiple="props.limit > 1"
          :on-error="excelUploadError"
          :on-exceed="handleExceed"
          :on-preview="handlePreview"
          :on-remove="handleRemove"
          :on-success="handleFileSuccess"
          :show-file-list="true"
          class="upload-file-uploader"
          name="file"
        >
          <img src="/src/assets/imgs/upload.png" fit="fill" class="h-44px w-48px" />
          <div>
            <span>将文件拖到此区域，或</span>
            <el-button type="primary" text plain> 点击上传 </el-button>
          </div>
          <slot name="more"></slot>
          <template v-if="isShowTip" #tip>
            <div style="font-size: 8px">
              大小不超过 <b style="color: #f56c6c">{{ fileSize }}MB</b>
            </div>
            <div style="font-size: 8px">
              格式为 <b style="color: #f56c6c">{{ fileType.join('/') }}</b> 的文件
            </div>
          </template>
          <template #file="row">
            <div class="flex items-center justify-between mt-30px">
              <span class="font-size-16px color-#31373D fw-500">{{ row.file.name }}</span>
  
              <div>
                <img src="/src/assets/imgs/success.webp" fit="fill" class="h-16px h-16px mr-8px" />
  
                <img
                  src="/src/assets/imgs/delete.webp"
                  fit="fill"
                  class="h-16px h-16px mr-8px"
                  @click="handleRemove(row.file)"
                />
              </div>
              <!-- <div class="ml-10px">
                <el-link :href="row.file.url" :underline="false" download target="_blank" type="primary">
                  下载
                </el-link>
              </div>
              <div class="ml-10px">
                <el-button link type="danger" @click="handleRemove(row.file)"> 删除</el-button>
              </div> -->
            </div>
          </template>
        </el-upload>
      </div>
      <!-- 上传操作禁用时 -->
      <div v-if="disabled" class="upload-file">
        <div v-for="(file, index) in fileList" :key="index" class="flex items-center file-list-item">
          <span>{{ file.name }}</span>
          <div class="ml-10px">
            <el-link :href="file.url" :underline="false" download target="_blank" type="primary">
              下载
            </el-link>
          </div>
        </div>
      </div>
    </Dialog>
  </template>
  <script lang="ts" setup>
  import { propTypes } from '@/utils/propTypes'
  import type { UploadInstance, UploadProps, UploadRawFile, UploadUserFile } from 'element-plus'
  import { UploadRequestOptions } from 'element-plus/es/components/upload/src/upload'
  import * as FileApi from '@/api/infra/file'
  import { useUpload } from '@/components/UploadFile/src/useUpload'
  import { UploadFile } from 'element-plus/es/components/upload/src/upload'
  
  defineOptions({ name: 'UploadFile' })
  
  const message = useMessage() // 消息弹窗
  const emit = defineEmits(['update:modelValue', 'update:visible', 'confirm', 'success'])
  
  interface File {
    name: string
    url: string
  }
  const props = defineProps({
    visible: propTypes.bool.def(false), // 是否显示提示
    modelValue: {
      type: Array<File>,
      default: () => []
    },
    title: propTypes.string.def('文件上传'),
    fileType: propTypes.array.def(['doc', 'xls', 'ppt', 'txt', 'pdf']), // 文件类型, 例如['png', 'jpg', 'jpeg']
    fileSize: propTypes.number.def(5), // 大小限制(MB)
    limit: propTypes.number.def(5), // 数量限制
    maxHeight: propTypes.number.def(300),
    autoUpload: propTypes.bool.def(true), // 自动上传
    drag: propTypes.bool.def(true), // 拖拽上传
    isShowTip: propTypes.bool.def(true), // 是否显示提示
    disabled: propTypes.bool.def(false), // 是否禁用上传组件 ==> 非必传（默认为 false）
    uploadApi: propTypes.func,
    uploadParam: propTypes.object
  })
  
  // ========== 上传相关 ==========
  const uploadRef = ref<UploadInstance>()
  const uploadList = ref<UploadUserFile[]>([])
  const fileList = ref<UploadUserFile[]>([])
  const uploadNumber = ref<number>(0)
  const dialogVisible = computed({
    get: () => props.visible,
    set: (value) => {
  
      emit('update:visible', value)
      if(!value) {
         reset()
      }
    }
  })
  
  let { uploadUrl, httpRequest } = useUpload()
  
  // 文件上传之前判断
  const beforeUpload: UploadProps['beforeUpload'] = (file: UploadRawFile) => {
    if (fileList.value.length > props.limit) {
      message.error(`上传文件数量不能超过${props.limit}个!`)
      return false
    }
    let fileExtension = ''
  
    if (file.name.lastIndexOf('.') > -1) {
      fileExtension = file.name.slice(file.name.lastIndexOf('.') + 1)
    }
    const isImg = props.fileType.some((type: string) => {
      if (file.type.indexOf(type) > -1) return true
      return !!(fileExtension && fileExtension.indexOf(type) > -1)
    })
    const isLimit = file.size < props.fileSize * 1024 * 1024
    if (!isImg) {
      message.error(`文件格式不正确, 请上传${props.fileType.join('/')}格式!`)
      return false
    }
    if (!isLimit) {
      message.error(`上传文件大小不能超过${props.fileSize}MB!`)
      return false
    }
    message.success('正在上传文件，请稍候...')
    uploadNumber.value++
  }
  // 手动上传
  const handleUpload = () => {
    uploadRef.value!.submit()
  }
  // 处理上传的文件发生变化
  // const handleFileChange = (uploadFile: UploadFile): void => {
  //   uploadRef.value.data.path = uploadFile.name
  // }
  // 文件上传成功
  const handleFileSuccess: UploadProps['onSuccess'] = (res: any): void => {
    message.success('上传成功')
    // 删除自身
    const index = fileList.value.findIndex((item) => item.response?.data === res.data)
    fileList.value.splice(index, 1)
  
    uploadList.value.push({ name: res.name, url: res.data })
    if (uploadList.value.length == uploadNumber.value) {
      fileList.value.push(...uploadList.value)
      uploadList.value = []
      uploadNumber.value = 0
      emitUpdateModelValue()
    }
    dialogVisible.value = false
    emit('success')
  }
  
  const handleConfirm = async () => {
    await handleUpload()
  
    emit(
      'confirm',
      props.modelValue.map((item) => item.url)
    )
  }
  
  const reset = () => {
    fileList.value = []
    emitUpdateModelValue()
  }
  // 文件数超出提示
  const handleExceed: UploadProps['onExceed'] = (): void => {
    message.error(`上传文件数量不能超过${props.limit}个!`)
  }
  // 上传错误提示
  const excelUploadError: UploadProps['onError'] = (): void => {
    // message.error('导入数据失败，请您重新上传！')
  }
  // 删除上传文件
  const handleRemove = (file: UploadFile) => {
    const index = fileList.value.map((f) => f.name).indexOf(file.name)
    if (index > -1) {
      fileList.value.splice(index, 1)
      emitUpdateModelValue()
    }
  }
  const handlePreview: UploadProps['onPreview'] = (uploadFile) => {
    console.log(uploadFile)
  }
  
  // 监听模型绑定值变动
  watch(
    () => props.modelValue,
    (val) => {
      if (!val) {
        fileList.value = [] // fix：处理掉缓存，表单重置后上传组件的内容并没有重置
        return
      }
  
      fileList.value = val.map((item) => {
        return {
          url: item.url,
          name: item.name
        }
      })
    },
    { immediate: true, deep: true }
  )
  
  //监听上传参数
  watch(
    () => props.uploadParam,
    (val) => {
      httpRequest = async (options: UploadRequestOptions) => {
        const uploadApi = props.uploadApi ? props.uploadApi : FileApi.updateFile
        return new Promise((resolve, reject) => {
          uploadApi({ file: options.file, ...val })
            .then((res) => {
              if (res.code === 200) {
                res.name = options.file.name
                resolve(res)
              } else {
                reject(res)
              }
            })
            .catch((error) => {
              reject(error)
            })
        })
      }
    }
  )
  // 发送文件链接列表更新
  const emitUpdateModelValue = () => {
    // 情况1：数组结果
  
    let result = fileList.value.map((file) => {
      return {
        name: file.name,
        url: file.url
      }
    })
    emit('update:modelValue', result)
  }
  </script>
  <style lang="scss" scoped>
  .upload-file-uploader {
    margin-bottom: 5px;
  }
  
  :deep(.upload-file-list .el-upload-list__item) {
    position: relative;
    margin-bottom: 10px;
    line-height: 2;
    border: 1px solid #e4e7ed;
  }
  
  :deep(.el-upload-list__item-file-name) {
    max-width: 250px;
  }
  
  :deep(.upload-file-list .ele-upload-list__item-content) {
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: inherit;
  }
  
  :deep(.ele-upload-list__item-content-action .el-link) {
    margin-right: 10px;
  }
  
  .file-list-item {
    border: 1px dashed var(--el-border-color-darker);
    border-radius: 8px;
  }
  </style>
  