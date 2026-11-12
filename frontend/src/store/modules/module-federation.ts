import { defineStore } from 'pinia'
import { ref } from '@/vue-export'

interface MF {
  name: string | null | undefined
  origin: string
  loaded: boolean
}

export const useModuleFederation = defineStore('module-federation', () => {
  const modules = ref(new Map<string, MF>())

  return { modules }
})
