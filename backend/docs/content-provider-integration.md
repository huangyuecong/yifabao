# 个性化内容智能体模型与知识库接入

## 当前边界

内容智能体运行时不绑定具体模型厂商、知识库目录或企业接口。文本、图片、配音、配乐、字幕、AI 视频、
确定性视频合成、资料目录与检索均通过内容服务自己的 Port 接入；会议纪要助手不引用这些 Port，
也不读取内容服务的数据库、目录或配置。

当前提供以下实现：

- `mock` 文本 Provider：离线、确定性，用于普通自动化测试；
- `openai_compatible` 文本 Provider：用于其他兼容协议的文本服务；
- `volcengine_ark` 文本、图片和 AI 视频 Provider：默认共用
  `CONTENT_ARK_API_KEY`（或 `CONTENT_ARK_API_KEY_FILE`）；各 Provider 仍可用
  自己的 Key 配置覆盖，便于权限隔离和密钥轮换；
- `hiagent` 图片和 AI 视频 Provider：使用 HiAgent V4 网关签名，图片默认
  `d9k56d4mgqo5afvrboqg`，视频优先 `d9k53bcmgqo5afvrbok0`（Pro 未授权或创建失败时回退
  `d9k55hkmgqo5afvrboo0`）；能力范围由工作区授权模型和工作流参数校验共同约束；
- `volcengine_tts_api_key` 配音 Provider：使用新版豆包语音 API Key，默认接入 TTS 2.0；
- `volcengine_tts` 旧版配音 Provider：仅为已有 AppID、Access Token 部署保留；
- `mock` / `minimax` 配乐 Provider：统一实现 `MusicGenerationPort`，生成纯音乐并返回可立即持久化的
  MP3/WAV bytes；MiniMax 实现使用 Music 2.6，不向浏览器暴露 Key、临时 URL 或十六进制原文；
- 本地字幕对齐：优先使用 TTS 字级时间戳，无时间戳时明确标记为 `estimated`；
- 本地 FFmpeg 合成：负责镜头、转场、字幕、配音、BGM、Logo、片头片尾和画幅；
- 本地 FFmpeg 时间线渲染：用真正 `xfade` 编译顺序镜头，支持裁剪/拆分、逐镜头源音轨、
  voice/music/sfx 的偏移/裁剪/音量/淡入淡出/ducking、Logo 区间与字幕烧录；
- `library_db` 知识 Provider：使用内容服务自身维护的企业资料；
- `weknora` 知识 Provider：正式资料后台同步至 WeKnora，由平台的 Embedding 模型建立向量索引；按本地权限、当前版本和显式所选资料限定检索范围。API 契约见 [适配说明](weknora-adapter-contract.md)。
- `jsonl` 知识 Provider：仅在显式指定目录时读取固定测试知识库。

## HiAgent 网关网络与代理

HiAgent 网关地址由 `HIAGENT_BASE_URL` 配置（例如 `https://hiagent.example.com:30040`）。
模型调用、参考素材上传和流式请求默认直连该地址，不继承 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`
或系统代理。生成结果位于同一主机时，图片、视频下载也使用同一策略；租户若在独立端口提供生成文件，
用 `HIAGENT_OBJECT_STORAGE_PORT` 声明。
这避免本机代理无法连接该平台时出现 Windows `10053`，或生成已经成功、下载却因
`SSL UNEXPECTED_EOF_WHILE_READING` 失败的问题。服务器需要能直接访问该域名的
`30040`（网关）和 `443`（文件下载）端口。

若部署环境确实必须经过代理访问 HiAgent，可显式设置：

```dotenv
HIAGENT_USE_ENV_PROXY=true
HTTPS_PROXY=http://your-proxy-host:port
```

未设置或设为 `false` 时使用直连；设为 `true` 时恢复 Requests 原有的环境/系统代理选择，
包括 `NO_PROXY` 规则。修改部署环境后重启内容服务。此开关仅影响 HiAgent 网关及该平台
域名的文件下载，不修改全局代理配置；其他 AI 服务和其他域名的结果文件下载保持原有
网络设置。下载仍执行原有 HTTPS、文件类型、大小及地址校验。

## 临时 JSONL 知识库测试

从项目根目录设置，不在代码中固定绝对路径：

```powershell
$env:CONTENT_KNOWLEDGE_PROVIDER = "jsonl"
$env:CONTENT_KNOWLEDGE_DIR = (Resolve-Path `
  "tests\fixtures\content_knowledge\yifabao_official").Path
$env:CONTENT_KNOWLEDGE_TOP_K = "4"
```

不设置上述变量时，默认使用 `library_db`，后续可通过现有企业资料维护功能写入甲方资料。

## 火山引擎模型测试

由部署环境设置，不把密钥写入代码。只配置统一 Key 时，服务会自动选择已验证的方舟
地址与默认文本、图片、视频模型；以下模型字段均为可选覆盖项：

```powershell
$env:CONTENT_ARK_API_KEY = "仅在本机临时输入的统一方舟密钥"

$env:CONTENT_LLM_MODEL = "可选：文本 Model ID"
$env:CONTENT_IMAGE_MODEL = "可选：图片 Model ID"
$env:CONTENT_AI_VIDEO_MODEL = "可选：视频 Model ID"
```

若部署需要三类能力使用不同密钥，可分别覆盖 `CONTENT_LLM_API_KEY`、
`CONTENT_IMAGE_API_KEY` 与 `CONTENT_AI_VIDEO_API_KEY`。标准方舟根地址同样允许通过各自的
`*_BASE_URL` 覆盖。

豆包语音是独立于方舟模型 Key 的接入面。新版控制台创建的语音 API Key 只需以下配置；
Provider、地址、Resource ID 和公版音色均有默认值，存在 Key 时自动启用：

```powershell
$env:CONTENT_SPEECH_API_KEY = "新版豆包语音 API Key"
```

默认值为 `https://openspeech.bytedance.com`、`seed-tts-2.0` 和
`zh_female_vv_uranus_bigtts`。`CONTENT_SPEECH_VOICE_TYPE` 用于指定默认音色；
`CONTENT_SPEECH_SUPPORTED_VOICES` 用于追加已开通音色，并与内置公版音色合并去重。服务端请求会开启
供应商字幕事件；返回字级时间戳时直接用于字幕对齐，否则明确回退为本地 `estimated` 时间轴。

仅旧版语音账号才显式设置 `CONTENT_SPEECH_PROVIDER=volcengine_tts`，并同时提供
`CONTENT_SPEECH_APP_ID`、`CONTENT_SPEECH_ACCESS_TOKEN`、Resource ID 与音色。新版 Key
绝不复用 `CONTENT_ARK_API_KEY`，也不会传到前端或写入生成结果。

外部图片、配音或 AI 视频调用失败时会返回明确错误，不会在生产调用中静默换成 Mock。AI 视频成功后
应立即通过 `download_generated_video` 转存到 `AssetStorePort`，避免供应商临时 URL 过期。

## Seedance 工程参考视频的私有 TOS 中转

Seedance 不能读取内容服务的登录态下载地址，也不接受 `data:video`。当工程参考视频需要交给外部模型
读取时，推荐配置私有火山引擎 TOS 中转。内容服务在完成用户权限、外发授权、ProjectAsset 归属、MP4/MOV、
200MB 和 2–15 秒校验后，直接以文件流上传到固定临时前缀，显式使用 private ACL，再把短时预签名 HTTPS
GET URL 只放进本次 Seedance 请求内。URL 不写数据库、provenance、审计或 API 响应；工程源文件也不会移动
或变成公开对象。

推荐生产配置：

```text
CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT=https://tos-<region>.volces.com
CONTENT_PROVIDER_MEDIA_TOS_REGION=<region>
CONTENT_PROVIDER_MEDIA_TOS_BUCKET=<private-bucket>
CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY_FILE=/run/secrets/content-provider-media-tos-ak
CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY_FILE=/run/secrets/content-provider-media-tos-sk
CONTENT_PROVIDER_MEDIA_TOS_KEY_PREFIX=provider-media/reference-videos
CONTENT_PROVIDER_MEDIA_TOS_URL_TTL_SECONDS=900
CONTENT_PROVIDER_MEDIA_TOS_OBJECT_TTL_DAYS=1
```

不提供示例凭据。账号 IAM 权限应限制到指定桶和 `provider-media/reference-videos/` 前缀，仅授予上传、列举、
读取签名和删除所需动作。桶和对象必须保持私有，Bucket Policy 不得匿名放行该前缀。服务会在 submit 异常、
供应商明确终态或用户取消后尽早删除；恢复循环按对象 key 中的 URL 到期时间清理。上传同时设置 1–7 天的
对象级兜底过期。部署仍必须给同一固定前缀配置桶生命周期删除规则（建议 1 天），以清除“上传完成但进程在
记录 cleanup token 前崩溃”的孤儿对象；若桶开启版本控制，还需同步清理非当前版本与删除标记。

TOS 配置是全有或全无；Endpoint 必须是无路径、无凭据的 HTTPS 地址，URL 有效期限定 60–3600 秒。只配一部分、
SDK 缺失、上传/预签名失败或返回非 HTTPS URL 时严格失败，不会退回公开目录，也不会把视频转成 Base64。

## Seedance 工程参考视频的签名 HTTPS 网关（备选）

未配置 TOS 时，也可给内容服务的 `GET/HEAD /api/v1/provider-media/{storage_key}` 配置公网可达的 HTTPS 反向
代理地址。不要把 `.data/content/objects` 或对象目录设成匿名静态目录；内容服务只会签发短时 HMAC SHA-256
URL，签名同时绑定对象 key 与到期时间，网关校验通过后才以只读方式流式返回 MP4/MOV。若 TOS 和网关同时
配置，TOS 是主 transport，能力接口同时列出两个 transport，但保留原布尔字段供前端兼容。

网关配置：

```text
CONTENT_PROVIDER_MEDIA_BASE_URL=https://content.example.cn/api/v1/provider-media
CONTENT_PROVIDER_MEDIA_SIGNING_SECRET_FILE=/run/secrets/content-provider-media-hmac
CONTENT_PROVIDER_MEDIA_URL_TTL_SECONDS=900
```

仓库提供了一份只暴露签名媒体路由的 Nginx 示例：
[`docs/nginx-content-provider-media.conf.example`](nginx-content-provider-media.conf.example)。部署时替换域名、
证书路径和上游服务名；如果沿用示例路径，`CONTENT_PROVIDER_MEDIA_BASE_URL` 必须与公网路径完全一致。
配置后应从内容服务所在网络之外分别用 `HEAD` 和带 `Range: bytes=0-1` 的 `GET` 验证返回 200/206；仅在
本机 curl 成功不能证明 Seedance 能访问该地址。

- 签名密钥至少 32 个随机字节，只保存在服务端；可用
  `python -c "import secrets; print(secrets.token_urlsafe(48))"` 离线生成后写入受限权限的 Secret 文件；
- `CONTENT_PROVIDER_MEDIA_BASE_URL` 必须无账号密码、查询串和片段，并且由负载均衡或反向代理转发到上述
  内容服务路由；公网证书、DNS 与防火墙连通性仍由部署侧提供；
- 代理需保留 `HEAD`、`Range` 与查询参数，且访问日志不应记录完整签名查询串；CDN 不应越过 URL 到期时间
  缓存响应；
- 基址与签名密钥必须同时配置。两者均未配置时网关安全关闭，能力接口返回
  `project_asset_transport_available=false`；配置后返回
  `project_asset_transport=provider-signed-https-gateway` 及实际 URL 有效期；
- URL 是到期前可使用的 bearer credential。不要写入业务日志、审计详情或前端持久状态，模型调用完成后
  只保留普通素材 ID。

仅设置一个变量、使用 HTTP、使用不足 32 字节的密钥或把有效期设到 60–3600 秒之外都会使服务启动失败，
避免出现界面宣称可用而模型实际无法读取、或误将整个对象目录匿名暴露的状态。

## AI 配乐接入

完整创作和快捷视频共用工程的 `generate-music` 接口，只生成无人声背景音乐（BGM），
不是文字朗读，也不承诺风雨、碰杯等独立音效生成；用户上传的音效仍可放入原来的背景音效轨。
未配置真实音乐凭据时，点击会收到中文配置提示，既不会生成模拟音频，也不会禁用后续重试。
只有开发/测试显式设置 `CONTENT_MUSIC_PROVIDER=mock` 才允许模拟输出。

### 火山纯音乐（Imagination）

该接口使用独立 AK/SK 签名，**不能复用 `CONTENT_ARK_API_KEY`**，也不会读取 TOS 或语音服务的密钥。
管理员须先按[音乐服务快速接入说明](https://www.volcengine.com/docs/84992/1404668)
确认账号资格、音乐服务开通和调用授权，再配置服务端密钥文件：

```dotenv
CONTENT_MUSIC_PROVIDER=volcengine
CONTENT_MUSIC_ACCESS_KEY_FILE=/run/secrets/content-music-ak
CONTENT_MUSIC_SECRET_KEY_FILE=/run/secrets/content-music-sk
CONTENT_MUSIC_MODEL=v5.0
CONTENT_MUSIC_BILLING=prepaid
CONTENT_MUSIC_TIMEOUT_SECONDS=300
CONTENT_MUSIC_POLL_INTERVAL_SECONDS=10
```

实现依据[火山官方 AgentKit 音乐示例](https://github.com/bytedance/agentkit-samples/blob/main/skills/byted-music-generate/scripts/music_generate.py)：
GitHub 仅供核对官方接口和签名代码，不是音乐来源；生成请求实际发送至火山 `open.volcengineapi.com`。
`GenBGM`（已有资源包）或显式配置 `postpaid` 的 `GenBGMForTime`（已有按时长服务），
提交 `Text/Version/Duration/EnableInputRewrite`，使用 `QuerySong` 轮询。
代码不会替账号开通服务、购买套餐、切换计费方式，也不会自动重发生成请求。
应用接受 1–120 整数秒；火山原始生成仍保持 30–120 秒。选择不足 30 秒时，先提交 30 秒音乐，
再由本地 FFmpeg 裁成所选长度，并在末尾做最多 0.2 秒淡出。短配乐仍可能按上游 30 秒生成计费，
不能把裁切后的时长当作模型原生时长或计费时长。配置的上游最小时长高于 30 秒时按该下限处理。
输出检查真实解码时长，记录用户目标、上游请求/报告时长、原文件实际时长及裁切信息；
原音频短于目标时明确失败，不补静音、不循环伪造长度。返回文件立即通过 HTTPS 域名白名单下载、
校验并落地。MP3/WAV 格式按实际文件处理，需要时由本地 FFmpeg 转码，不向接口发送未记载的格式字段。
缺密钥、权限不足、额度到期、限流、审核失败和网络超时会返回可理解的中文提示。
新音乐适配器已通过模拟协议测试；实际环境缺专用 AK/SK，不能据此声称真实生成已验通。

### MiniMax（保留现有接入）

MiniMax 配乐 Key 独立于方舟与豆包语音 Key。只需在服务端环境提供：

```powershell
$env:CONTENT_MUSIC_API_KEY = "MiniMax 服务端 API Key"
```

此时空的 `CONTENT_MUSIC_PROVIDER` 会自动选择 `minimax`，默认地址为
`https://api.minimaxi.com`、模型为 `music-2.6`、供应商响应格式为 `hex`。未配置任何音乐 Key 时选择
待配置的火山适配器；点击生成才提示缺少专用密钥。显式选择 `minimax` 但缺 Key 会启动失败，不会静默回退。

`MusicGenerationRequest` 提供 `prompt`、目标 `duration_seconds`、MP3/WAV 以及 hex/url 响应选择；
`MusicGenerationResult.audio` 始终是经过大小和文件签名校验的 bytes。URL 模式只接受 HTTPS，拒绝本机或
非公网 IP，且默认只允许 MiniMax 官方根域；部署可通过 `CONTENT_MUSIC_ALLOWED_DOWNLOAD_HOSTS` 覆盖为
实测供应商 CDN 域名，并在一次调用内完成有界下载。业务层应立即把 bytes 写入 `AssetStorePort`。

MiniMax Music 2.6 接口没有精确时长参数，所以适配器不会伪造供应商字段：请求时长记录为目标值，
实际时长来自供应商元数据，随后由时间线音乐轨的 trim/loop 落实。MiniMax 官方文档还注明，自
2026-08-20 起付费 Music Generation API 不再向新用户开放（已有付费用户可继续使用），接入前需用
[官方 Music Generation 文档](https://platform.minimax.io/docs/api-reference/music-generation)
核对账号资格；无资格部署应配置其他已获授权的真实服务，不应在正式环境用 Mock 代替。

## 时间线 FFmpeg 核心

路由层可通过 `get_timeline_renderer()` 获取 `FfmpegTimelineRenderer`。公开 API 只应接受素材 ID；路由先
通过 `AssetStorePort` 将 ID 解析成服务端 `Path`，再构造 `TimelineRenderRequest`。依赖注入默认把输入和
输出都限制在 `CONTENT_DATA_DIR`，渲染模型没有 raw filter 或 shell 参数字段。

- `TimelineClip`：同一素材使用不同 `source_in_seconds/source_out_seconds` 重复出现，即为一次 split；
  元组顺序即拼接顺序，后一镜头的 `transition` 决定 cut 或 allowlist 内的真正 xfade；
- `source_audio=keep/mute` 与 `source_audio_volume` 按镜头生效；
- `TimelineAudioTrack` 分为 voice/music/sfx，支持时间线 offset、源区间 trim、目标 duration、音量、
  fade 与 music loop；`TimelineDucking` 使用 voice 作为 sidechain 压低指定 music/sfx/source；
- `TimelineLogo` 用 start/end 控制显示区间，`TimelineSubtitle` 仅接受 SRT/ASS/SSA/VTT 和受控样式；
- `build_command()` 可供作业队列做验证/排队，`render()` 执行并返回 `TimelineRenderResult`。

旧的 `VideoComposerPort` / `FfmpegVideoComposerPort` 保留，现有轻量合成调用无需迁移；新剪辑工作台应使用
时间线核心，避免把旧合成器继续扩展为第二套项目模型。

外部 Provider 默认不能接收任务数据。管理员必须按任务创建与
`CONTENT_LLM_PROVIDER_ID` 完全一致的外发授权，并分别明确：

- 是否允许发送任务配置、用户生成指令；
- 是否允许发送合规禁用词；
- 允许检索并发送的知识资料等级（`public / internal / restricted`）；
- 允许继续改写或生成分镜的来源草稿等级。

生成流程会在网络调用前完成授权检查、来源任务隔离和资料等级双重校验。生成草稿和人工修订稿默认
至少按 `internal` 处理；使用受限资料时升级为 `restricted`。知识资料许可与来源草稿许可相互独立，
旧授权不包含新增许可字段时按拒绝处理。

## 正式前端工作台完整链路测试

在设置模型与知识库环境变量的同一个 PowerShell 窗口启动项目，然后打开主系统中的内容智能体工作台。页面会
读取当前文本 Provider，不包含固定厂商名称或模型地址。

1. 创建内容任务，填写主题、产品、关键词、人群、类型、风格、篇幅和附加要求；
2. 外部模型模式下，点击“授权外部模型使用任务信息、公开资料和内部草稿”；
3. 依次测试生成选题、文案、改写、合规检测和分镜；
4. 在接口结果中核对 `generation`、`reference_item_ids`、`references` 和 `compliance`；
5. 下载单个文本结果或任务素材包，确认各版本文件互不覆盖。

正式工作台只授权当前任务的任务上下文、合规词表及所选知识资料和来源草稿等级。若确需检索
内部资料或处理受限草稿，必须通过授权接口在对应范围显式增加等级，不能通过修改知识库路径、人工
改写引用信息或错误的 Provider 返回值绕过。

## 生成结果追溯

选题、文案和分镜结果会记录：

- 稳定 Provider ID 与模型名；
- 提示词版本、结束原因和供应商返回的 token 用量；
- 本次检索或模型声明使用的资料 ID、分类、数据等级、版本和公开来源地址；
- 生成后的敏感词与禁用词检测结果。

日志和业务结果不会记录 API Key、Authorization 请求头或完整外发提示词。

## 后续对接甲方资源

甲方知识库接口确定后，在内容服务内新增 `KnowledgeCatalogPort` / `KnowledgeSearchPort` 适配器即可，
业务生成流程和对外接口无需修改。其他模型也只新增对应 Port 适配器；模型地址、鉴权和模型名继续
使用 `CONTENT_*` 配置注入。Provider 不进入共享 `agent_core`，避免会议纪要助手获得外部内容模型通道。
