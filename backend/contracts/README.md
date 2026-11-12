# AI 个性化内容智能体接口契约

- `content.openapi.json`：AI 个性化内容智能体接口

接口文件由 `scripts/export_openapi.py` 从内容 FastAPI 应用生成。内容产物草稿通过 `DELETE /api/v1/artifacts/{artifact_id}` 作废。该接口是软删除且幂等，已确认版本返回 `409 Conflict`。

更新契约：

```powershell
.\.venv\Scripts\python.exe scripts\export_openapi.py
```
