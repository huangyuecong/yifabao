# AI 个性化内容智能体

独立项目，只包含个性化内容业务：文案、图片、视频、素材和知识库。不包含会议纪要、声纹或说话人分离服务。

- `backend/`：内容 FastAPI 服务
- `frontend/`：只保留内容智能体页面的 Vue 前端脚手架
- `launcher/`：独立启动器

达梦数据库与 WeKnora 知识平台支持显式配置接入，默认保留本地模式。连接配置项见 `backend/.env.example`。

| 组件 | 端口 |
|---|---|
| 前端 | 8200 |
| 内容 API | 8002 |

## 首次运行

在 `backend` 创建 Python 3.12 虚拟环境并安装 `requirements-dev.txt`。前端使用 Element Plus 和本地 Vue 组件，不需要公司私有组件仓库。在 `frontend` 依次执行：

```powershell
pnpm install --store-dir ../.pnpm-store --frozen-lockfile --prefer-offline --ignore-scripts --registry=https://registry.npmjs.org
pnpm rebuild esbuild @swc/core vue-demi
```

上述安装方式适用于不含 `.git` 的项目副本：跳过开发 Git hooks 等安装脚本，再构建前端需要的本地依赖。

日常只需双击根目录的 `AI个性化内容智能体启动器.exe`。重复的 EXE、CMD 和中文启动 PS1 入口已移除。

`launcher` 中的脚本是该 EXE 的内部依赖，不是需要另外打开的启动器，不能删除。重新编译使用 `launcher/build.ps1`，只生成根目录这一个 EXE。

启动前会检查依赖、Vite 配置和端口归属；缺失依赖时直接显示失败，不自动安装，也不会先启动一半服务。关闭启动器窗口不会停止服务，可使用窗口中的“停止服务”。

页面：http://127.0.0.1:8200/content/

本机工作台无需登录，也不显示企业品牌或账号菜单。在“模型与服务”中点击“修改模型”，即可设置文本、图片、视频、配音和背景音乐服务的接口地址、模型与密钥；保存后用于后续生成。已有密钥不会回显，保持原接口且密钥留空时继续使用原配置。

本机启动使用 `frontend/.env.dev` 的 `VITE_CONTENT_AUTH_MODE=standalone`，并在 `backend/.env` 设置 `CONTENT_STANDALONE_MODE=true`、`CONTENT_IDENTITY_MODE=development`。前后端只监听本机回环地址，沿用 `dev-user` 的原有任务和资料。修改运行模式后需通过启动器停止并重新启动服务。

主流程：「商品库」维护商品（名称、价格、1–9 张图片、参数）→ 勾选一批商品「批量生成」，可选标题和文案、宣传图片、宣传视频 →「生成与审核」由管理员通过、驳回、修改或按意见重新生成 → 通过后写入「内容库」→「商品上架」（闲鱼）和「内容宣传」（抖音、小红书、快手）两个独立工作流从内容库选取内容执行 → 每次执行都写入「发布记录」，提交后结果不明的记录标为「待核查」，不会自动重试。数据表见 `catalog_models.py`（products、product_images、generation_batches、generation_items、product_contents、publish_records 等）。「内容宣传」选好内容后会按抖音、小红书、快手发布页的样子预览标题、正文和视频（点击画面可播放），并标红超出平台字数限制的部分，超限内容无法提交。已有闲鱼商品的上下架仍在「商品上架 → 已有商品上下架」。

现已增加安卓 App 执行结构，宣传平台目录包含抖音、小红书、快手，商品平台从闲鱼开始扩展。新建工作流可选安卓设备，旧浏览器配置继续兼容。当前安卓适配器均待实现，未连接模拟器时可保存规则、生成草稿，实际发布会被拦截。

AI 对话的回复通过 SSE 逐段显示，生成图片、视频、文档时显示当前阶段；系统先让对话模型通过 Function Calling 在“文字回答、生成文档、生成图片、生成视频”四个工具中选择一个，模型不支持或调用失败时改用规则匹配，回复上会标出所用方式。两项能力可分别用 `backend/.env` 的 `CONTENT_CHAT_STREAMING`、`CONTENT_CHAT_TOOL_ROUTING` 关闭。

## 开源声明

前端宿主外壳（项目脚手架、构建配置、布局与通用组件）基于 MIT 协议的开源项目 [yudao-ui-admin-vue3](https://gitee.com/yudaocode/yudao-ui-admin-vue3)（芋道源码，其本身源自 [vue-element-plus-admin](https://github.com/kailong321200875/vue-element-plus-admin)），原版权声明保留在 [frontend/LICENSE](frontend/LICENSE)。本作品的业务功能为自行开发：前端位于 `frontend/src/views/agent/` 与 `frontend/src/api/agent/`，后端内容服务位于 `backend/services/content_service/`。第三方依赖的许可证见 [frontend/dependency-licenses.txt](frontend/dependency-licenses.txt) 及 `backend/requirements*.txt`。
