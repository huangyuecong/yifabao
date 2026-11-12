# WeKnora 适配器与 API 契约

本模块接入已有 WeKnora 文档知识库，不创建空间、知识库或模型。解析、Embedding、向量库及可选的摘要模型由 WeKnora 管理。本项目继续维护资料目录、版本、权限、任务引用和审计；支持格式的正式附件由平台保存原件，本地文件作为经校验的缓存。

2026-10-05 首个接入包仅支持文字副本。本轮接入将原件托管与可重建的检索副本分开；下述为两条流程的契约。目标库已有 COS，实际原件下载和预览验证通过，但开发改动及本地测试不等于服务器已部署验收。严禁复用旧版/停用索引清理流程去删除平台唯一原件。

契约核对日期：2026-10-05。来源：

- [官方知识库与知识 API](https://weknora.weixin.qq.com/docs/04-api/02-api-knowledge)
- [官方认证与响应约定](https://weknora.weixin.qq.com/docs/04-api/01-api-overview)
- [官方 SearchParams / SearchResult 类型](https://github.com/Tencent/WeKnora/blob/main/internal/types/search.go)
- [官方 Knowledge 类型](https://github.com/Tencent/WeKnora/blob/main/internal/types/knowledge.go)

这些文档描述当前官方接口；实际部署版本尚未确认。本地模拟契约测试通过不代表真实部署已联通。上线前应在指定测试库验证文档范围过滤、解析状态、来源 metadata 和删除行为。旧版若不支持这些字段或返回结构，适配器拒绝调用结果，不自动改为全库检索。

## HTTP 接口

所有路径使用 `/api/v1` 前缀，`base_url` 接受服务根地址或带 `/api/v1` 的地址。认证仅使用服务端配置的 `X-API-Key`，可选 `X-Tenant-ID` 表示 **WeKnora 空间 ID**。该 ID 不等于项目资料的业务 `tenant_id`。

建议使用绑定到目标空间、限制指定知识库且仅具 `retrieve` / `ingest` 能力的 key。无需超级管理员密码、登录 JWT 或创建空间权限。客户端禁用重定向和环境代理继承，保留 TLS 校验；不把远端响应正文、请求头或 key 写入错误信息。支持显式配置的内网 HTTP 地址，生产网络应按部署边界选择 HTTPS 或可信专网。

| 操作 | 方法与路径 | 请求/响应重点 |
| --- | --- | --- |
| 上传文件 | `POST /knowledge-bases/{kb}/knowledge/file` | multipart `file`、`fileName`、`metadata` JSON 字符串、`channel=api`；返回 Knowledge；重复可返回 409 + 已有 Knowledge |
| 同步本地文本 | 同上传文件 | 文本编码为 UTF-8 Markdown 文件，metadata 保存 `local_source_marker`，用于超时后的精确恢复 |
| 手工知识 | `POST /knowledge-bases/{kb}/knowledge/manual` | `title/content/status=publish/channel=api`；独立提供，但不用于需来源 marker 的默认同步 |
| 读取解析状态 | `GET /knowledge/{id}` | 严格校验 `id/knowledge_base_id/parse_status`；只把 `completed` 且未禁用视为 ready |
| 文档列表 | `GET /knowledge-bases/{kb}/knowledge` | `page/page_size`；`data` 数组及 `total`；仅用于来源恢复，不自动导入整个平台目录 |
| 原件下载 | `GET /knowledge/{id}/download` | 只使用数据库中已验证的原件关联；流式限制大小并核对 SHA-256，不信任重定向或远端文件名 |
| 原件预览 | `GET /knowledge/{id}/preview` | 已验证目标联调部署可读取，但不假设支持 Range；应用通过本地回填继续提供原有预览/播放 |
| 删除文档 | `DELETE /knowledge/{id}` | 异步提交，返回 `data.task_id`；提交成功不代表远端已经物理删除 |
| 范围检索 | `POST /knowledge-bases/{kb}/hybrid-search` | `query_text/knowledge_ids/match_count/skip_context_enrichment=true`；读取 `data[].id/knowledge_id/content/score` |

实际联调服务在无命中时返回 `data: null`，客户端将其视为空列表；缺少 `data` 字段仍视为协议错误。2026-10-05 已使用两份虚构文档验证上传来源 metadata、解析完成状态、正确内容召回，以及只指定其中一份时不会返回另一份。此验证不等于业务服务器已启用同步。

文件上传的来源 marker 必须精确匹配本地 entry/version；上传响应未包含 marker 时再读一次详情。重复文档若属于不同来源，拒绝关联，避免后续误删别人资料。客户端不自动重试写入。来源恢复最多扫描 100 页；遇到多个相同 marker、分页不完整或扫描超限，均明确失败，不猜测归属。

官方 `knowledge_create.go` 会用 multipart `fileName` 的扩展名选择解析器，不能直接传不带扩展名的业务标题。客户端生成“安全标题-稳定来源摘要.md”作为文本文件名，原业务标题保存在字符串 metadata 的 `local_title` 中。WeKnora 还按文件内容 hash 去重；为避免不同本地资源/版本因正文相同而共用远端归属，Markdown 加入稳定来源摘要注释。同一版本重试的字节一致，不同版本的字节不同。

这项追加来源注释的处理只适用于重新生成的 Markdown 检索副本。正式附件必须原字节托管，不能为了去重改写 PDF、图片或其他原件。原件出现 409/远端内容去重时，需要验证本项目连接、目标库、可信归属及实际内容，不得通过覆盖别人 metadata 冒领原件；不能确认归属则保留本地并明确报告，不删除未知远端条目。

2026-10-07 补充重复原件复用：只从本项目同连接、同知识库的已就绪原件记录中，按完整 SHA256 和文件大小查找候选。先核对当前本地文件与上传收据，再核对候选远端归属并实际下载验证字节。成功后保留本条资料自己的 `source_marker`，另外保存 `remote_source_marker`，将多条本地资料关联到同一份已验证的远端原件。旧记录没有该字段值时沿用自己的 `source_marker`。同名、仅远端声称摘要相同、其他连接或未经本项目托管的文件均不能直接复用。

共享只影响原件存储：资料权限、历史版本和独立检索索引仍按各自记录管理；读取、缓存回填和缓存回收使用已验证的远端来源标识。复用不改写远端 metadata，不重新上传原件，不删除远端文件。此前因重复归属被拒绝的记录，只有存在符合条件的本项目已就绪原件时才进入恢复验证，失败仍保留本地文件并报告。

## 独立原件托管

配置为 `CONTENT_WEKNORA_ORIGINALS_ENABLED=false`（默认不开启新原件上传）和 `CONTENT_WEKNORA_ORIGINALS_MAX_FILE_BYTES=52428800`（默认 50 MiB）。关闭新原件托管不应关闭已有托管原件的读取；只要连接配置完整，素材存储仍提供按需回填。

原件和索引至少区分用途、关联记录、恢复标识与清理路径。相同资料版本可以同时关联一条远端原件与一条 Markdown 索引；原件 ID 不加入项目检索 `knowledge_ids`，不会因为平台自动解析了原件而扩大本项目的授权检索范围。

原件映射必须能定位服务连接、知识库、知识条目、本地 storage_key 及其版本，并保留原始文件名、MIME、字节数和 SHA-256。以可信本地记录为依据，不能从客户端提交一个远端 ID 或 URL 就创建托管映射。原件状态与索引 `ready` 分开：远端确实保存且取回字节一致，可以认定原件托管成功；只有解析完成且启用才允许该独立索引参与搜索。

正式附件先保留在本地，上传后取回并核对实际大小、摘要和目标库关联，再保存可用于缓存回填的映射。响应丢失或元数据不完整时先恢复/核实，不盲目上传，也不立即删除本地副本。远端取回失败、校验不符或原件格式不支持时保持本地可用并报告原因。

换版、资料停用和索引重建只撤销/清理独立文字索引，保留原件和旧版本关联。官方删除知识条目会删除原件，故索引清理器不得碰原件托管记录。缓存回收只删除本机副本，不调用远端 DELETE。原件永久删除必须另外定义业务生命周期及引用检查，不隐含在本次迁移里。

### 授权读取与缓存回填

现有资料、任务和工程下载接口先检查本地权限，再让素材存储按可信 storage_key 找到原件托管映射，按需取回。不得新增匿名、只凭 storage_key/远端 ID 读取资料的旁路。已有 Provider 短时签名拉取接口先验签再取文件。私有文件响应使用 `Cache-Control: private, no-store`，浏览器不获得知识平台 Key 或长效匿名对象 URL。

远端响应数据流写入临时文件，限制实际字节数、校验摘要后原子替换最终缓存；失败删除未完成临时文件。当前回填使用进程内锁串行处理和独立临时文件原子替换；多个进程可能重复下载，但不会暴露半文件。跨进程共享运行锁用于排斥离线清理，并非跨进程下载去重锁。保持现有目录边界与符号链接防穿越校验，不能接受远端返回的文件路径。

`AssetStorePort.path_for` 返回的是普通 Path，没有声明消费完成的时间。HTTP FileResponse、流式 generator 和 FFmpeg 都可能在函数返回后继续读取，所以当前回收只在停止应用和相关后台任务时执行。工具逐项核实远端、大小和 SHA-256，无法证明托管完整的文件一律保留。若未来做运行中 TTL 回收，须为所有读取者实现覆盖整个响应/渲染期限的共享锁或租约，清理器只在获得排他锁后执行。

随包提供 `configure-weknora.py` 交互生成经只读验证的私有 overlay，以及 `maintain-original-cache.sh --dry-run|--apply` 进行显式缓存维护；它们随私有部署包提供，不在本仓库中。不在启动或后台定时任务中自动删除原件缓存。

关闭外部检索与停止原件读取是两个动作。回滚为本地检索不能丢弃托管映射或凭据，否则本地缓存已经回收的正式附件无法打开。彻底退出平台前必须完整取回并校验所有唯一远端副本。

### 不能由知识 API 替代的存储

当前官方知识文件接口拒绝视频。`/storage-backends` 是配置管理和连通性检查；`/files` 是只读代理；`/sessions/{id}/attachments` 是临时会话文档；`/artifacts` 没有外部通用上传端点。平台的 COS 凭据在服务端加密，不通过这些接口授权本项目任意写对象。

因此视频、工程临时素材和任意生成/导出产物继续使用现有文件存储，不伪造格式或混入企业知识库。界面应明确提示不支持托管的原件仍保留本机。通用媒体远端存储需要项目专用对象存储授权或平台通用文件写 API；不能因知识原件接入完成就声称所有文件已移出应用服务器。

## 本地映射

`ExternalRef.system` 为 `weknora`，`external_id` 为平台知识条目 ID。`metadata_json` 至少包含：

```json
{
  "connection_id": "配置连接范围的摘要",
  "knowledge_base_id": "目标库ID",
  "version_id": "本地ResourceVersion.id",
  "sync_status": "ready",
  "source_marker": "content-agent:本地entry-id:本地version-id"
}
```

`connection_id` 由规范化服务地址、目标库 ID 和平台空间 ID 计算，不含密钥；更换 key 不丢失映射，改变服务/库/空间则不会误用旧映射。

上述 `ExternalRef.system=weknora` 专用于检索副本。原件的独立记录不能复用此 system 被检索器或清理器选中。原件迁移和缓存回收不改变旧版索引队列的权限过滤逻辑。

`WeKnoraDocument.ready` 只接受 `parse_status=completed`，`enable_status` 为空（兼容未提供此字段的版本）或 `enabled`。`pending/processing/finalizing/failed` 及未知状态均不可检索。单独的同步模块负责持久任务、提交恢复、轮询和删除协调；HTTP 客户端本身不写库。

## 权限与范围

`WeKnoraKnowledgeProvider.search(query)` 自动检索；`search_selected(query, resource_ids, allow_cross_owner=False)` 限定用户勾选的本地资源。

1. 从本地当前版本与 active 资源计算可访问范围；检查用户所有权、public、部门、角色、本地租户及允许发送到平台的数据等级。
2. 仅纳入匹配当前连接、目标知识库、当前版本且 `sync_status=ready` 的 ExternalRef。
3. 显式选中资料若无权限或未完成索引，返回明确错误；显式空数组及空授权目录不产生网络调用。
4. 每次检索都发送非空 `knowledge_ids`，最多 200 个一组；绝不退化为整个库或空间检索。关闭上下文自动补齐，减少平台跨片段扩展。
5. 任一返回项缺失文档 ID、超出本次文档范围或声明错误知识库，整次拒绝。标题、分类、数据等级与版本均使用可信本地数据，不信任平台自报权限。
6. 收到结果后刷新本地资源与映射，再检查 active、权限、当前版本和同步状态；并发停用、撤权或新版本会使结果失效。

内容生成使用上述检索器时，先验证并提炼用户所选资料的本地当前版本。索引尚未完成、没有命中、连接失败、超时、HTTP 429 或 5xx 时，使用经重新校验的当前本地摘要继续生成，不等待知识索引；已有远端命中保留，但缺少命中的所选资料仍用本地摘要补齐，保证每项选择都进入模型上下文。无当前版本内容时仍报错。权限拒绝、并发权限或版本变化、越界结果及异常协议不回退；不会改为全库检索，也不会使用旧索引。

平台默认允许 `public/internal`，并与内容生成本身允许的数据等级取交集。`restricted` 只有服务端明确配置允许时才可送往知识平台。资料存在本地 `tenant_id` 而请求缺少或不匹配业务租户时拒绝，不能用平台空间 ID 代替用户租户。

任务参考图、普通图片/视频/音频素材不会因配置知识平台而自动进入文本索引；是否作为企业资料同步由业务同步层明确决定。

## 验证

部署环境准备好专用 API Key 后，使用服务端现有配置执行只读前置检查：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts/check_weknora.py
```

也可执行 `python -m content_service.weknora_diagnostics`。命令不从参数读取密钥、不登录管理员、不创建任何资源，也不启动应用或同步线程；只请求 `GET /api/v1/knowledge-bases/{已配置的库ID}`，验证凭据及读取权限、文档库类型、Embedding 模型 ID 已配置，输出经过白名单筛选的 JSON。返回码 0 表示这些只读检查通过，1 表示接口/权限/库配置未通过，2 表示本地服务端配置无效。

这一步不声称获得服务器版本，不验证实际 Embedding 推理、写入权限或知识 ID 过滤的运行效果；这些必须在目标版本的测试库做一次受控契约验收。当前公网入口的 `GET /health` 返回前端 HTML，不能用其 HTTP 200 代替真实 API 检查。

`tests/test_content_weknora.py` 使用 httpx MockTransport 和独立 SQLite 数据库验证真实路由/字段、上传与重复归属、状态/删除、来源恢复、重定向及错误脱敏、文档范围、权限/数据等级/版本/连接隔离及并发撤权。测试不读取凭据，也不请求真实平台。

原件接入需要额外覆盖：原字节上传和回读摘要校验；重定向/超大响应/摘要不符失败；超时恢复及不同用途归属；本地权限拒绝后无远端下载；换版/停用只删索引保留原件；无远端验证时禁止回收；并发回填不暴露半文件；回收后缓存缺失可恢复；视频明确本地保留。服务器部署验收还应在停止服务窗口对少量测试文件实际回收并恢复，不能用模拟测试代替此项。

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_content_weknora.py -q
```
