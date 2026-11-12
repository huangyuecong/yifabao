# @yifabao/platform-auth

共享前端平台认证客户端。

主入口 `@yifabao/platform-auth` 负责浏览器凭据传输、登录态、Entry/管理接口和统一请求错误；
Vue 项目可通过同一个包的 `@yifabao/platform-auth/vue` 子入口使用共享 Session 组合函数。

浏览器不保存访问令牌，仅携带 HttpOnly Session Cookie。Agent 业务 API 不属于本包。
