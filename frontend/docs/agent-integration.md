# AI 个性化内容智能体前端接入说明

## 项目边界

本仓库是在雅酿 `@gzxj/create-app` 后台管理系统脚手架上增加的内容智能体前端。只接入内容服务，不包含会议纪要、声纹或说话人分离。

| 模块 | 前端路由 | 默认代理前缀 | 代理目标配置 |
|---|---|---|---|
| AI 个性化内容智能体 | `/agent/content` | `/content-agent-api` | `VITE_CONTENT_PROXY_TARGET` |

公司原有接口继续使用 `/admin-api`，本次接入没有修改其请求封装。

## 本地启动

先启动内容后端（内容 API 8002）：

```powershell
Set-Location '<项目>/backend'
.\scripts\run-dev.ps1
```

再打开另一个 PowerShell，启动前端：

```powershell
Set-Location '<项目>/frontend'
pnpm install --store-dir .pnpm-store --frozen-lockfile --prefer-offline
.\node_modules\.bin\vite.cmd --mode dev --host 127.0.0.1 --port 8200 --strictPort
```

日常仅使用项目根目录的 `AI个性化内容智能体启动器.exe`；`launcher` 目录保留其内部脚本，不再提供重复启动入口。

本地开发模式会直接进入内容工作台，不请求公司统一登录。此行为由 `.env.dev` 中的 `VITE_AGENT_DEV_MODE=true` 控制。

页面地址：http://127.0.0.1:8200/content/admin/agent/content

## 生产接入要求

- 正式环境必须保持 `VITE_AGENT_DEV_MODE=false`。
- 浏览器不得伪造 `X-User-*` 开发身份头；生产身份由超级入口或可信网关写入。
- `VITE_CONTENT_API_URL` 配置到内容服务网关地址；未配置时使用相对代理前缀。
- 企业资料库、素材库和正式模型接入通过后端 Provider 完成，前端业务流程与字段保持不变。
