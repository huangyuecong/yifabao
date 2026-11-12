import type { GroupForm, GroupQuery, GroupVO, ID, IDS } from './model'
import request from '@/config/axios'

// 查询人员分组列表
export const groupList = async (params?: GroupQuery): Promise<GroupVO[]> => {
  return await request.get({ url: '/system/group/list', params })
}

// 导出人员分组列表
export const groupExport = async (params?: GroupQuery): Promise<Blob> => {
  return await request.post({
    url: '/system/group/export',
    params,
    responseType: 'blob',
  })
}

// 查询人员分组详情
export const getGroupUser = async (id: ID): Promise<GroupVO> => {
  return await request.get({ url: `/system/group/getUser`, params: { id } })
}

// 新增人员分组
export const groupAdd = async (data: GroupForm) => {
  return await request.post({ url: '/system/group', data })
}

// 更新人员分组
export const groupUpdate = async (data: GroupForm) => {
  return await request.put({ url: '/system/group', data })
}

// 删除人员分组
export const groupRemove = async (id: ID | IDS) => {
  return await request.delete({ url: `/system/group/${id}` })
}
