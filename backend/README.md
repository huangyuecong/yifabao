# AI 个性化内容智能体后端

本目录只包含个性化内容业务：文案、图片、视频、素材和知识库。不包含会议纪要、声纹或说话人分离服务。

- `services/content_service`：内容 API（8002）
- `services/media_relay`：可选媒体中继
- `packages/agent_core`：本服务使用的共享基础包

临时知识库、外部文本模型、任务级外发授权见 [`docs/content-provider-integration.md`](docs/content-provider-integration.md)。

## 首次安装

```powershell
Set-Location "<项目>/backend"
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

把 `.env.example` 复制为 `.env`（已从原项目拆出内容相关配置时可直接使用现有 `.env`）。

## 本地启动

```powershell
.\scripts\run-dev.ps1
```

停止：

```powershell
.\scripts\stop-dev.ps1
```

健康检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8002/health -UseBasicParsing
```

OpenAPI：http://127.0.0.1:8002/docs

## 数据库迁移

```powershell
.\scripts\migrate.ps1
.\scripts\migrate.ps1 -Action downgrade -Revision -1
```

## 验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\scripts\check.ps1
.\.venv\Scripts\python.exe scripts\export_openapi.py
```

`check.ps1` 会依次跑 Ruff、mypy、pytest（含覆盖率门槛）和 pip-audit。提交前钩子见 `.pre-commit-config.yaml`。
