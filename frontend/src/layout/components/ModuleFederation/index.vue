<script lang="ts" setup>
import { ContentWrap } from '@/components/ContentWrap'
import { useModuleFederation } from '@/store/modules/module-federation'
import { defineAsyncComponent, onMounted, ref, shallowRef } from '@/vue-export'
import { loadRemote, registerRemotes } from '@module-federation/enhanced/runtime'
import { to } from 'await-to-js'
import Loading from './Loading.vue'

const route = useRoute()

// console.log('[build in time] route:', toRaw(route))

const MFComponent = shallowRef()
const isLoading = ref(true)
const errorMessage = ref('')

const moduleFederationStore = useModuleFederation()

onMounted(async () => {
  isLoading.value = true
  let origin = route.meta.origin as string ?? ''
  let pathname = origin

  if (origin.startsWith('/')) {
    // 相对于当前网站的url,非外部
    origin = location.origin
  }
  else {
    const url = new URL(origin)
    origin = url.origin
    pathname = url.pathname
  }
  const originBase = route.query._debug_ ? `http://localhost:${route.query._debug_}` : origin

  const originUrl = `${originBase}${pathname}`

  // console.log(originUrl)

  if (!moduleFederationStore.modules.get(originUrl)?.loaded) {
    // 获取清单文件
    const [err, manifestJson] = await to(fetch(originUrl).then((res: Response) => res.json()))
    if (err) {
      // console.log('模块清单请求错误', err)
      errorMessage.value = '模块加载失败'
      isLoading.value = false
      return
    }
    // 是否考虑移入onMounted
    registerRemotes([
      {
        name: manifestJson.name as string,
        entry: originUrl,
      },
    ], { force: true })
    moduleFederationStore.modules.set(originUrl, {
      name: manifestJson.name,
      loaded: true,
      origin: originUrl,
    })
  }

  const mf = moduleFederationStore.modules.get(originUrl)

  if (!mf) {
    // console.log('模块清单请求错误', originUrl)
    errorMessage.value = '模块加载失败'
    isLoading.value = false
    return
  }

  MFComponent.value = defineAsyncComponent({
    loader: () => loadRemote(`${mf.name}${(route.meta.pathname as string).trim()}`),
    loadingComponent: Loading,
  })
  isLoading.value = false
})
</script>

<template>
  <ContentWrap>
    <div v-if="MFComponent && !isLoading" class="min-h-300px">
      <component :is="MFComponent" />
    </div>
    <div v-if="isLoading">
      <Loading />
    </div>
    <div v-if="errorMessage.length && !isLoading" class="text-center">{{ errorMessage }}</div>
  </ContentWrap>
</template>
