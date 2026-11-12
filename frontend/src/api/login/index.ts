import type { RegisterVO, UserLoginVO } from './types'
import request from '@/config/axios'
import { getRefreshToken } from '@/utils/auth'

export interface SmsCodeVO {
  mobile: string
  scene: number
}

export interface SmsLoginVO {
  mobile: string
  code: string
}

// 登录
export const login = (data: UserLoginVO) => {
  return request.post({ url: '/system/auth/login', data })
}

// 获取key
export function getPublicKey() {
  return request.get({ url: '/system/auth/public-key', params: {} })
}

export const quickBILoginout = () => {
  return request.put({ url: `/system/quick-bi/logout` })
}

// 登录
export const wxlogin = (code, applicationId) => {
  return request.post({ url: '/system/auth/gzxj-login', data: { code, applicationId } })
}
// OA登录
export const oalogin = (code, applicationId = '3018') => {
  return request.post({ url: '/system/auth/oa-login', data: { code, applicationId } })
}
// 获取扫码登录二维码地址
export const getAuthorizeUrl = (redirectUri?: string) => {
  const callback = redirectUri || `${import.meta.env.VITE_BASE_URL}${import.meta.env.VITE_BASE_PATH}/#/login?type=30&agentid=${import.meta.env.VITE_AGENT_ID}`
  return request.get({ url: '/system/auth/social-auth-redirect', params: { type: 30, redirectUri: callback } })
}
// 刷新访问令牌
export const refreshToken = () => {
  return request.post({ url: `/system/auth/refresh-token?refreshToken=${getRefreshToken()}` })
}
// 注册
export const register = (data: RegisterVO) => {
  return request.post({ url: '/system/auth/register', data })
}

// 使用租户名，获得租户编号
export const getTenantIdByName = (name: string) => {
  return request.get({ url: `/system/tenant/get-id-by-name?name=${name}` })
}

// 使用租户域名，获得租户信息
export const getTenantByWebsite = (website: string) => {
  return request.get({ url: `/system/tenant/get-by-website?website=${website}` })
}

// 登出
export const loginOut = () => {
  return request.post({ url: '/system/auth/logout' })
}

// 获取用户权限信息
export const getInfo = () => {
  return request.get({ url: '/system/auth/get-permission-info' })
}

// 获取登录验证码
export const sendSmsCode = (data: SmsCodeVO) => {
  return request.post({ url: '/system/auth/send-sms-code', data })
}

// 短信验证码登录
export const smsLogin = (data: SmsLoginVO) => {
  return request.post({ url: '/system/auth/sms-login', data })
}

// 社交快捷登录，使用 code 授权码
export function socialLogin(type: string, code: string, state: string) {
  return request.post({
    url: '/system/auth/social-login',
    data: {
      type,
      code,
      state,
    },
  })
}

// 社交授权的跳转
export const socialAuthRedirect = (type: number, redirectUri: string) => {
  return request.get({
    url: `/system/auth/social-auth-redirect?type=${type}&redirectUri=${redirectUri}`,
  })
}
// 获取验证图片以及 token
export const getCode = (data) => {
  return request.postOriginal({ url: 'system/captcha/get', data })
}

// 滑动或者点选验证
export const reqCheck = (data) => {
  return request.postOriginal({ url: 'system/captcha/check', data })
}
