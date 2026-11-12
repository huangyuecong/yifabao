"""Read-only, redacted WeKnora preflight. Never writes to the remote platform."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence

from .weknora import WeKnoraClient, WeKnoraError


def configured_client() -> WeKnoraClient:
    # Load server settings without importing main, starting workers or reading
    # model credentials from the platform. No secrets are accepted on argv.
    from .config import load_settings

    settings = load_settings()
    return WeKnoraClient(
        settings.weknora_base_url or "",
        settings.weknora_api_key or "",
        settings.weknora_knowledge_base_id or "",
        tenant_id=settings.weknora_tenant_id,
        timeout_seconds=settings.weknora_timeout_seconds,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], WeKnoraClient] = configured_client,
) -> int:
    parser = argparse.ArgumentParser(
        description="只读检查 WeKnora 专用 API Key、目标文档库读取权限和 Embedding 配置"
    )
    parser.parse_args(argv)
    try:
        result = client_factory().check_knowledge_base()
    except WeKnoraError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": exc.code,
                    "message": exc.safe_message,
                    "http_status": exc.status_code,
                },
                ensure_ascii=False,
            )
        )
        return 1
    except (ValueError, OSError):
        # Configuration/secret-file errors may carry paths or malformed values.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "configuration_invalid",
                    "message": "请检查服务端 WeKnora 地址、API Key/密钥文件、知识库 ID 和空间配置",
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
