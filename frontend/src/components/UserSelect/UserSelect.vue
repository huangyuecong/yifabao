<script lang="ts" setup>
import * as DeptApi from '@/api/system/dept'
import * as UserApi from '@/api/system/user'

const deptList = ref<any[]>([])
const dataReady = ref(false)
// 懒加载
const load = async (node, resolve) => {
  // 人员
  if (node.data.userCode) {
    resolve([])
    return
  }
  // 部门的顶层节点
  if (node.level === 0) {
    const result: Array<any> = []
    deptList.value.forEach((element) => {
      if (Number(element.parentId) === 0) {
        result.push(element)
      }
    })
    resolve(result)
  }
  else {
    // 部门的叶节点
    if (!deptList.value.some(element => element.parentId === node.data.id)) {
    // 调接口，返回数据
      const deptId = node.data.id
      const data = await UserApi.getUserByDept(deptId)
      resolve(data.map((item) => {
        return { ...item, name: item.nickname }
      }))
    }
    // 部门的非叶节点
    else {
      const result: Array<any> = []
      deptList.value.forEach((element) => {
        if (element.parentId === node.data.id) {
          result.push(element)
        }
      })
      resolve(result)
    }
  }
}
onBeforeMount(async () => {
  const res = await DeptApi.getSimpleDeptList()
  deptList.value = []
  deptList.value.push(...res)
  dataReady.value = true
})
</script>

<template>
  <el-tree-select
    v-if="dataReady"
    multiple
    placeholder="请选择抄送人"
    lazy
    :load="load"
    :props="{ label: 'name',
              value: 'id',
              isLeaf(data){
                if (data.username){ return true }
                else { return false }
              } }"
  />
</template>
