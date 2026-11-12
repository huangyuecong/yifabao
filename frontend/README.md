# AI 个性化内容智能体前端

只接入个性化内容智能体，访问内容服务 `8002`。不包含会议纪要页面。

## 开源声明

前端宿主外壳（项目脚手架、构建配置、布局与通用组件）基于 MIT 协议的开源项目 [yudao-ui-admin-vue3](https://gitee.com/yudaocode/yudao-ui-admin-vue3)（芋道源码，其本身源自 [vue-element-plus-admin](https://github.com/kailong321200875/vue-element-plus-admin)），原版权声明保留在 [LICENSE](LICENSE)。第三方依赖的许可证见 [dependency-licenses.txt](dependency-licenses.txt)。

本作品的业务功能均为自行开发，代码位于：

- `src/views/agent/`：工作台、商品库、生成与审核、内容库、商品上架、内容宣传、发布记录、AI 对话、资料与素材、模型与服务、视频编辑器等全部页面与组件；
- `src/api/agent/`：内容服务接口层（类型由后端 OpenAPI 契约生成）；
- `scripts/*.test.mjs`：智能体页面与接口的测试脚本。

- 页面：`/admin/agent/content`
- 开发地址：http://127.0.0.1:8200/content/admin/agent/content

## 本地开发

首次安装依赖：

```powershell
pnpm install --store-dir .pnpm-store --frozen-lockfile --prefer-offline
```

日常启动：

```powershell
.\node_modules\.bin\vite.cmd --mode dev --host 127.0.0.1 --port 8200 --strictPort
```

请先按后端 README 启动 `8002`，或直接运行项目启动器。

## 提交前验证

```powershell
pnpm run quality:agent
pnpm run build:dev
```

`quality:agent` 会阻止内容客户端跨到会议服务，也会阻止智能体代码直接依赖公司原有业务 API。
