import request from '@/config/axios'

// 查询用户管理列表
export const getPersonPage = (params) => {
  return request.get({ url: '/system/personnel/page', params })
}

// 查询用户详情
export const getPerson = (id) => {
  return request.get({ url: `/system/personnel/get/${id}` })
}

// 新增用户
export const createPerson = (data) => {
  return request.post({ url: '/system/personnel/create', data })
}

// 修改用户
export const updatePerson = (data) => {
  return request.put({ url: '/system/personnel/update', data })
}

// 删除用户
export const deletePerson = (id) => {
  return request.delete({ url: `/system/personnel/delete/${id}` })
}

// 导出用户
export const importPerson = (file) => {
  return request.post({ url: '/system/personnel/import-excel', data: { file } })
}

export const importPersonTemplate = () => {
  return request.get({ url: '/system/personnel/get-import-template' })
}
