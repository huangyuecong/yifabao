export type ID = string | number
export type IDS = string[] | number[]

export interface BaseEntity {
  createBy?: any
  createDept?: any
  createTime?: string
  updateBy?: any
  updateTime?: any
}

/**
 * 分页查询参数
 * @param pageNo 当前页
 * @param pageSize 每页大小
 * @param orderByColumn 排序字段
 * @param isAsc 是否升序
 */
export interface PageQuery {
  pageNo?: number
  pageSize?: number
  orderByColumn?: string
  isAsc?: boolean
}

export interface GroupVO {
  /**
   *
   */
  id: string | number

  /**
   * 分组名称
   */
  name: string
}

export interface GroupForm extends BaseEntity {
  /**
   *
   */
  id?: string | number

  /**
   * 分组名称
   */
  name?: string
}

export interface GroupQuery extends PageQuery {
  /**
   * 分组名称
   */
  name?: string

  /**
   * 日期范围参数
   */
  params?: any
}
