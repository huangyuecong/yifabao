# API 约定

## 服务边界

| 服务 | 开发端口 | API 前缀 | 数据目录 |
|---|---:|---|---|
| 会议纪要助手 | 8001 | `/api/v1` | `.data/meeting` |
| AI 个性化内容 | 8002 | `/api/v1` | `.data/content` |

两个服务不得通过数据库、对象目录、队列或内部 Python 模块读取对方业务数据。公共能力只能依赖版本化的 `yifabao-agent-core` 包。

## 身份与追踪

开发身份使用请求头 `X-User-Id`、`X-User-Name`、`X-Department`、`X-Roles`，值使用 ASCII 稳定标识；中文姓名和部门名称由身份对象的业务字段返回。生产环境必须由可信网关或超级入口身份适配器写入，不能接受浏览器任意伪造。

服务接受或生成 `X-Request-Id` 和 `X-Trace-Id`，并在响应头返回；后台任务另有稳定的 Job ID。

## 错误格式

错误响应使用 `application/problem+json`，包含 `type`、`title`、`status`、`detail`、`instance`、`request_id` 和 `trace_id`。参数错误额外包含 `errors`。

## 内容版本作废

- `DELETE /api/v1/artifacts/{artifact_id}` 只允许作废尚未确认的草稿，实际执行 `draft -> discarded` 软状态转换，不删除数据库记录或对象文件。
- 重复作废是幂等操作；响应中的 `already_discarded` 表示该版本此前是否已经作废。
- 已确认版本返回 `409 Conflict`，作废版本的编辑、改写、合规检测、确认、下载和显式来源引用同样返回 `409 Conflict`。
- 普通版本列表、任务导出、分镜自动文案来源、字幕自动配音来源和视频合成自动素材选择都会忽略作废版本。
- 作废操作写入 `artifact.discarded` 审计事件；作废记录仍参与后续版本号计算，保证版本号单调递增且不复用。

## 模型边界

- 会议模型只能使用本地 Provider，会议数据禁止进入外部模型通道。
- 内容模型默认使用确定性 Mock；未来外部 Provider 必须先产生管理员批准的任务级外发授权。
- Mock 与正式 Provider 返回相同业务数据结构，前端不感知供应商差异。
