"""Verify a private relay connection, then optionally persist local settings.

No provider generation is called. The probe uses a synthetic MP4 header, never
an existing user video. TLS validation stays enabled on every request.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

PROBE = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"relay-connectivity-probe"
PREFIX = "CONTENT_PROVIDER_MEDIA_RELAY_"


def secure_file(path: Path) -> None:
    if os.name == "nt":
        output = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
        sid = next(csv.reader(output.strip().splitlines()))[1]
        if not re.fullmatch(r"S-1-[\d-]+", sid):
            raise ValueError("无法确定当前 Windows 用户权限")
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        path.chmod(0o600)


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    secure_file(path)
    path.write_text(text, encoding="utf-8")


def load_connection(path: Path) -> tuple[str, str, int]:
    if path.stat().st_size > 8192:
        raise ValueError("连接配置文件过大")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    base = str(data.get("base_url", "")).rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.path
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("base_url 必须是无路径、无凭据的 HTTPS 地址")
    token = data.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("连接令牌格式无效")
    ttl = data.get("url_ttl_seconds", 1800)
    if type(ttl) is not int or not 60 <= ttl <= 3600:
        raise ValueError("URL 有效期必须在 60～3600 秒")
    return base, token, ttl


def probe_connection(base: str, token: str) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    # Disable environment proxies and redirects so credentials only reach the
    # exact operator-selected origin. Never format httpx exception URLs.
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        health = client.get(base + "/healthz")
        if health.status_code != 200 or health.json().get("service") != "yifabao-media-relay":
            raise ValueError("HTTPS 地址没有返回正确的中转服务健康状态")
        uploaded = client.post(
            base + "/v1/objects",
            content=PROBE,
            headers={**auth, "Content-Type": "video/mp4"},
        )
        if uploaded.status_code != 201:
            raise ValueError(f"中转上传验证失败（HTTP {uploaded.status_code}）")
        data = uploaded.json()
        object_id = data.get("cleanup_token", "")
        if not isinstance(object_id, str) or not re.fullmatch(r"[a-f0-9]{32}", object_id):
            raise ValueError("中转返回了无效的对象标识")
        try:
            media_url = data.get("url", "")
            target = urlsplit(media_url)
            origin = urlsplit(base)
            if (
                target.scheme != "https"
                or target.netloc != origin.netloc
                or target.path != f"/v1/media/{object_id}"
                or target.fragment
                or target.username
                or target.password
            ):
                raise ValueError("中转返回了不属于当前服务的媒体地址")
            query = parse_qs(target.query, strict_parsing=True)
            if set(query) != {"expires", "signature"}:
                raise ValueError("中转签名地址格式无效")
            head = client.head(media_url)
            if head.status_code != 200 or int(head.headers.get("content-length", "-1")) != len(
                PROBE
            ):
                raise ValueError("HTTPS HEAD 验证失败")
            ranged = client.get(media_url, headers={"Range": "bytes=0-1"})
            if ranged.status_code != 206 or ranged.content != PROBE[:2]:
                raise ValueError("HTTPS Range 分段读取验证失败")
            unsigned = client.get(base + target.path)
            if unsigned.status_code not in (401, 403, 404):
                raise ValueError("中转服务未拒绝无签名读取")
        finally:
            removed = client.delete(base + f"/v1/objects/{object_id}", headers=auth)
            if removed.status_code not in (204, 404):
                raise ValueError("探测对象清理失败，请检查中转服务")
        print("通过：可信 HTTPS、鉴权上传、签名 HEAD/Range、拒绝无签名读取、清理。")
        print("此验证未调用火山生成；模型侧抓取仍需一次真实短视频引用验收。")


def save_settings(env_file: Path, base: str, token: str, ttl: int) -> None:
    env_file = env_file.resolve(strict=True)
    original = env_file.read_text(encoding="utf-8-sig")
    for line in original.splitlines():
        match = re.match(r"\s*(CONTENT_PROVIDER_MEDIA_TOS_[A-Z_]+)\s*=\s*(.*)", line)
        if (
            match
            and match.group(1)
            in {
                "CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT",
                "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY",
                "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY_FILE",
                "CONTENT_PROVIDER_MEDIA_TOS_BUCKET",
            }
            and match.group(2).strip().strip("\"'")
        ):
            raise ValueError("当前已存在 TOS 配置，请先明确使用哪一种中转方式")
    secrets_dir = env_file.parent / ".data" / "secrets"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    token_file = secrets_dir / f"media-relay-token-{stamp}"
    backup = env_file.parent / ".data" / "config-backups" / f"env-before-relay-{stamp}"
    temp = backup.with_name(f"env-relay-{stamp}.tmp")
    lines = [
        line
        for line in original.splitlines()
        if not re.match(r"\s*CONTENT_PROVIDER_MEDIA_RELAY_[A-Z_]+\s*=", line)
    ]
    lines.extend(
        [
            "",
            "# Private HTTPS reference-video relay",
            f"{PREFIX}BASE_URL={base}",
            f"{PREFIX}TOKEN_FILE={token_file.as_posix()}",
            f"{PREFIX}URL_TTL_SECONDS={ttl}",
        ]
    )
    write_private(backup, original)
    write_private(token_file, token)
    write_private(temp, "\n".join(lines) + "\n")
    os.replace(temp, env_file)
    secure_file(env_file)
    print(f"已写入本地配置：{env_file}")
    print(f"原配置备份：{backup}")
    print("请在没有进行中的生成作业时重启内容服务，再点击页面“重新检测”。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("connection", type=Path, help="从服务器安全下载的 client-connection.json")
    parser.add_argument("--apply", action="store_true", help="验证通过后写入 .env；默认仅验证")
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env"
    )
    args = parser.parse_args()
    try:
        base, token, ttl = load_connection(args.connection)
        probe_connection(base, token)
        if args.apply:
            save_settings(args.env_file, base, token, ttl)
    except (ValueError, KeyError, OSError, httpx.HTTPError, subprocess.SubprocessError):
        # Exception strings can include bearer URLs or OS command data.
        print(
            "连接验证或配置写入失败；未输出凭据。请检查地址、证书、端口、令牌与服务日志。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
