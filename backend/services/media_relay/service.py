#!/usr/bin/env python3
"""Private upload API and expiring video downloads; run behind an HTTPS proxy.

Only Python's standard library is required. The listener is deliberately bound
to loopback; Nginx handles public TLS. Credentials are read from private files.
"""

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

LOGGER = logging.getLogger("media_relay")
OBJECT_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_CONNECTIONS = 24
MIN_FREE_BYTES = 64 * 1024 * 1024
IO_TIMEOUT_SECONDS = 20
UPLOAD_TIMEOUT_SECONDS = 300
CHUNK_BYTES = 64 * 1024
ALLOWED_TYPES = {"video/mp4", "video/quicktime"}


class RelayError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class RelayConfig:
    data_dir: Path
    public_base_url: str
    upload_token: str = field(repr=False)
    signing_secret: bytes = field(repr=False)
    url_ttl_seconds: int = 1800
    object_ttl_seconds: int = 86400
    max_storage_bytes: int = 5368709120
    port: int = 18741

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        try:
            parsed = urlsplit(self.public_base_url)
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError as exc:
            raise ValueError("MEDIA_RELAY_PUBLIC_BASE_URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not valid_port
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in self.public_base_url)
        ):
            raise ValueError("MEDIA_RELAY_PUBLIC_BASE_URL must be an HTTPS origin")
        object.__setattr__(self, "public_base_url", self.public_base_url.rstrip("/"))
        try:
            token_bytes = self.upload_token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Upload token must contain printable ASCII") from exc
        if len(token_bytes) < 32 or any(c <= 32 or c >= 127 for c in token_bytes):
            raise ValueError("Upload token must contain at least 32 printable ASCII bytes")
        if not isinstance(self.signing_secret, bytes) or len(self.signing_secret) < 32:
            raise ValueError("Signing secret must contain at least 32 bytes")
        if not 60 <= self.url_ttl_seconds <= 3600:
            raise ValueError("URL TTL must be between 60 and 3600 seconds")
        if not self.url_ttl_seconds <= self.object_ttl_seconds <= 7 * 86400:
            raise ValueError("Object TTL must cover URL TTL and be at most 7 days")
        if self.max_storage_bytes < 16:
            raise ValueError("Storage capacity must be positive")
        if not 0 <= self.port <= 65535:
            raise ValueError("Invalid listening port")

    @classmethod
    def from_env(cls):
        def credential(name):
            filename = os.environ.get(name)
            if not filename:
                raise ValueError(name + " is required")
            return Path(filename).read_bytes().rstrip(b"\r\n")

        return cls(
            data_dir=Path(os.environ.get("MEDIA_RELAY_DATA_DIR", "/var/lib/yifabao-media-relay")),
            public_base_url=os.environ.get("MEDIA_RELAY_PUBLIC_BASE_URL", ""),
            upload_token=credential("MEDIA_RELAY_UPLOAD_TOKEN_FILE").decode("ascii"),
            signing_secret=credential("MEDIA_RELAY_SIGNING_SECRET_FILE"),
            url_ttl_seconds=int(os.environ.get("MEDIA_RELAY_URL_TTL_SECONDS", "1800")),
            object_ttl_seconds=int(os.environ.get("MEDIA_RELAY_OBJECT_TTL_SECONDS", "86400")),
            max_storage_bytes=int(os.environ.get("MEDIA_RELAY_MAX_STORAGE_BYTES", "5368709120")),
            port=int(os.environ.get("MEDIA_RELAY_PORT", "18741")),
        )


class RelayStore:
    def __init__(self, config: RelayConfig, clock: Callable = time.time):
        self.config = config
        self.clock = clock
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(4)
        self._active = {}  # id -> reserved upload bytes
        self._records: dict[str, dict] = {}
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._load_records()
        self.purge()

    def _path(self, object_id, suffix):
        if not OBJECT_ID.fullmatch(object_id):
            raise RelayError(404, "Object not found")
        return self.config.data_dir / (object_id + suffix)

    def _load_records(self):
        for path in self.config.data_dir.glob("*.json"):
            object_id = path.stem
            if not OBJECT_ID.fullmatch(object_id) or path.is_symlink():
                continue
            try:
                if path.stat().st_size > 2048:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                media = self._path(object_id, ".media")
                if (
                    record["id"] != object_id
                    or record["content_type"] not in ALLOWED_TYPES
                    or type(record["size"]) is not int
                    or not 16 <= record["size"] <= MAX_UPLOAD_BYTES
                    or type(record["expires_at"]) is not int
                    or type(record["created_at"]) is not int
                    or record["expires_at"] <= record["created_at"]
                    or media.is_symlink()
                    or not media.is_file()
                    or media.stat().st_size != record["size"]
                ):
                    continue
                self._records[object_id] = record
            except (OSError, ValueError, KeyError, TypeError):
                continue

    @staticmethod
    def _unlink(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # An open Windows download can temporarily prevent deletion. It is
            # already inaccessible to new requests and the next sweep retries.
            LOGGER.warning("File cleanup will be retried")

    def _disk_bytes(self):
        total = 0
        for path in self.config.data_dir.glob("*.media"):
            if OBJECT_ID.fullmatch(path.stem) and not path.is_symlink():
                with contextlib.suppress(FileNotFoundError):
                    total += path.stat().st_size
        return total

    def signature(self, object_id, expires):
        message = ("v1\n" + object_id + "\n" + str(expires)).encode("ascii")
        return hmac.new(self.config.signing_secret, message, hashlib.sha256).hexdigest()

    def download_url(self, record):
        expires = min(int(self.clock()) + self.config.url_ttl_seconds, record["expires_at"])
        return {
            "url": (
                self.config.public_base_url
                + "/v1/media/"
                + record["id"]
                + "?expires="
                + str(expires)
                + "&signature="
                + self.signature(record["id"], expires)
            ),
            "cleanup_token": record["id"],
            "expires_at": expires,
        }

    def upload(self, stream, size, content_type, set_timeout):
        if not self._slots.acquire(blocking=False):
            raise RelayError(503, "Upload capacity is busy; retry later")
        object_id = uuid.uuid4().hex
        temporary = self._path(object_id, ".upload")
        media = self._path(object_id, ".media")
        metadata = self._path(object_id, ".json")
        metadata_temp = self._path(object_id, ".json.tmp")
        committed = False
        try:
            with self._lock:
                reserved = sum(self._active.values())
                if self._disk_bytes() + reserved + size > self.config.max_storage_bytes:
                    raise RelayError(507, "Media storage capacity exceeded")
                if shutil.disk_usage(self.config.data_dir).free - reserved - size < MIN_FREE_BYTES:
                    raise RelayError(507, "Insufficient disk space")
                self._active[object_id] = size
            deadline = time.monotonic() + UPLOAD_TIMEOUT_SECONDS
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open(str(temporary), flags, 0o600), "wb") as target:
                remaining = size
                header = b""
                validated = False
                while remaining:
                    seconds_left = deadline - time.monotonic()
                    if seconds_left <= 0:
                        raise RelayError(408, "Upload timed out")
                    set_timeout(min(IO_TIMEOUT_SECONDS, seconds_left))
                    chunk = stream.read1(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise RelayError(400, "Upload ended before Content-Length")
                    if len(header) < 16:
                        header += chunk[: 16 - len(header)]
                    if not validated and len(header) >= 16:
                        box_size = int.from_bytes(header[:4], "big")
                        if header[4:8] != b"ftyp" or box_size < 16 or box_size > size:
                            raise RelayError(
                                415, "Video must begin with a valid MP4 or QuickTime ftyp box"
                            )
                        validated = True
                    target.write(chunk)
                    remaining -= len(chunk)
                if not validated:
                    raise RelayError(415, "Invalid video header")
                target.flush()
                os.fsync(target.fileno())
            now = int(self.clock())
            record = {
                "id": object_id,
                "size": size,
                "content_type": content_type,
                "created_at": now,
                "expires_at": now + self.config.object_ttl_seconds,
            }
            with self._lock:
                with os.fdopen(
                    os.open(str(metadata_temp), flags, 0o600), "w", encoding="utf-8"
                ) as target:
                    json.dump(record, target, separators=(",", ":"))
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, media)
                os.replace(metadata_temp, metadata)
                self._records[object_id] = record
                committed = True
            return self.download_url(record)
        except TimeoutError as exc:
            raise RelayError(408, "Upload timed out") from exc
        finally:
            with self._lock:
                if not committed:
                    for path in (temporary, media, metadata, metadata_temp):
                        self._unlink(path)
                self._active.pop(object_id, None)
            self._slots.release()

    def open_download(self, object_id, expires, signature):
        now = int(self.clock())
        if (
            expires <= now
            or expires > now + self.config.url_ttl_seconds
            or not re.fullmatch(r"[0-9a-f]{64}", signature)
            or not hmac.compare_digest(self.signature(object_id, expires), signature)
        ):
            raise RelayError(403, "Download authorization is invalid or expired")
        with self._lock:
            record = self._records.get(object_id)
            if not record or record["expires_at"] <= now:
                raise RelayError(404, "Object not found")
            path = self._path(object_id, ".media")
            if path.is_symlink():
                raise RelayError(404, "Object not found")
            try:
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                source = os.fdopen(os.open(str(path), flags), "rb")
            except OSError as exc:
                raise RelayError(404, "Object not found") from exc
            if os.fstat(source.fileno()).st_size != record["size"]:
                source.close()
                raise RelayError(404, "Object not found")
            return record.copy(), source

    def delete(self, object_id):
        with self._lock:
            self._records.pop(object_id, None)
            self._unlink(self._path(object_id, ".json"))
            self._unlink(self._path(object_id, ".media"))

    def purge(self):
        with self._lock:
            expired = [
                object_id
                for object_id, record in self._records.items()
                if record["expires_at"] <= int(self.clock())
            ]
            for object_id in expired:
                self.delete(object_id)
            for path in self.config.data_dir.iterdir():
                match = re.fullmatch(r"([0-9a-f]{32})\.(media|json|upload|json\.tmp)", path.name)
                if not match:
                    continue
                object_id, suffix = match.groups()
                if object_id in self._active:
                    continue
                if suffix in ("upload", "json.tmp") or object_id not in self._records:
                    self._unlink(path)
            return len(expired)


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address, config, clock):
        self.config = config
        self.store = RelayStore(config, clock)
        self._stop_cleanup = threading.Event()
        self._request_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, RelayHandler)
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            try:
                request.settimeout(0.5)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\nRetry-After: 5\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def _cleanup_loop(self):
        while not self._stop_cleanup.wait(60):
            try:
                self.store.purge()
            except OSError:
                LOGGER.warning("Scheduled cleanup failed; will retry")

    def server_close(self):
        self._stop_cleanup.set()
        self._cleanup_thread.join(timeout=1)
        super().server_close()

    def handle_error(self, request, client_address):
        # BaseServer's traceback could expose an arbitrary request URL.
        LOGGER.warning("Request connection closed unexpectedly")


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MediaRelay/1"
    sys_version = ""

    def setup(self):
        self.request.settimeout(IO_TIMEOUT_SECONDS)
        super().setup()

    def log_message(self, format, *args):
        pass  # Never log user input, bearer tokens, query strings or signed URLs.

    def log_request(self, code="-", size="-"):
        LOGGER.info("request status=%s", code)

    def _headers(self, status, length, content_type="application/json", extra=None):
        self.close_connection = True
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _json(self, status, payload, extra=None):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._headers(status, len(body), extra=extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authenticate(self):
        values = self.headers.get_all("Authorization", [])
        expected = ("Bearer " + self.server.config.upload_token).encode("ascii")
        candidate = values[0].encode("utf-8") if len(values) == 1 else b""
        if not hmac.compare_digest(
            hashlib.sha256(candidate).digest(), hashlib.sha256(expected).digest()
        ):
            raise RelayError(401, "Upload authorization required")

    def handle_expect_100(self):
        # Validate before inviting an unauthenticated/oversized upload body.
        try:
            if self.command != "POST" or self.path != "/v1/objects":
                raise RelayError(417, "Expectation is unsupported for this request")
            self._authenticate()
            self._upload_headers()
        except RelayError as exc:
            self._json(exc.status, {"error": exc.message})
            return False
        self.send_response_only(100)
        self.end_headers()
        return True

    def _upload_headers(self):
        if self.headers.get_all("Transfer-Encoding"):
            raise RelayError(400, "Transfer-Encoding is not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise RelayError(411, "Content-Length is required")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,12}", lengths[0]):
            raise RelayError(400, "Invalid Content-Length")
        size = int(lengths[0])
        if size > MAX_UPLOAD_BYTES:
            raise RelayError(413, "Video exceeds 200 MiB limit")
        if size < 16:
            raise RelayError(415, "Video is too short")
        types = self.headers.get_all("Content-Type", [])
        content_type = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
        if content_type not in ALLOWED_TYPES:
            raise RelayError(415, "Only MP4 and QuickTime video are supported")
        return size, content_type

    def _dispatch(self):
        if self.path == "/healthz" and self.command in ("GET", "HEAD"):
            self._json(200, {"status": "ok", "service": "yifabao-media-relay", "api_version": 1})
            return
        if self.path == "/v1/objects" and self.command == "POST":
            self._authenticate()
            size, content_type = self._upload_headers()
            result = self.server.store.upload(
                self.rfile, size, content_type, self.connection.settimeout
            )
            self._json(201, result)
            return
        if self.path == "/v1/cleanup" and self.command == "POST":
            self._authenticate()
            self._json(200, {"deleted": self.server.store.purge()})
            return
        delete_match = re.fullmatch(r"/v1/objects/([0-9a-f]{32})", self.path)
        if delete_match and self.command == "DELETE":
            self._authenticate()
            self.server.store.delete(delete_match.group(1))
            self._headers(204, 0)
            return
        split = urlsplit(self.path)
        media_match = re.fullmatch(r"/v1/media/([0-9a-f]{32})", split.path)
        if (
            not split.scheme
            and not split.netloc
            and not split.fragment
            and media_match
            and self.command in ("GET", "HEAD")
        ):
            query = parse_qs(split.query, keep_blank_values=True, max_num_fields=4)
            if (
                set(query) != {"expires", "signature"}
                or any(len(v) != 1 for v in query.values())
                or not re.fullmatch(r"[0-9]{1,12}", query["expires"][0])
            ):
                raise RelayError(403, "Invalid download authorization")
            record, source = self.server.store.open_download(
                media_match.group(1), int(query["expires"][0]), query["signature"][0]
            )
            with source:
                self._download(record, source)
            return
        raise RelayError(404, "Route not found")

    def _download(self, record, source):
        size = record["size"]
        start, end = 0, size - 1
        ranges = self.headers.get_all("Range", [])
        if ranges:
            match = (
                re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", ranges[0]) if len(ranges) == 1 else None
            )
            if (
                not match
                or not any(match.groups())
                or any(len(part) > 12 for part in match.groups())
            ):
                self._json(
                    416,
                    {"error": "Unsatisfiable byte range"},
                    {"Content-Range": "bytes */" + str(size)},
                )
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                start = max(0, size - int(last))
            if start >= size or end < start:
                self._json(
                    416,
                    {"error": "Unsatisfiable byte range"},
                    {"Content-Range": "bytes */" + str(size)},
                )
                return
        extra = {"Accept-Ranges": "bytes", "Content-Disposition": "inline"}
        if ranges:
            extra["Content-Range"] = "bytes " + str(start) + "-" + str(end) + "/" + str(size)
        count = end - start + 1
        self._headers(206 if ranges else 200, count, record["content_type"], extra)
        if self.command == "HEAD":
            return
        source.seek(start)
        while count:
            chunk = source.read(min(CHUNK_BYTES, count))
            if not chunk:
                break
            self.wfile.write(chunk)
            count -= len(chunk)

    def _handle(self):
        self._response_started = False
        try:
            self._dispatch()
        except RelayError as exc:
            self._error(exc.status, exc.message)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except ValueError:
            self._error(400, "Malformed request")
        except OSError:
            LOGGER.warning("Request failed during storage access")
            self._error(503, "Storage is temporarily unavailable")

    def _error(self, status, message):
        # Once streaming has begun, close a failed transfer without appending a
        # second HTTP response to the video. Disconnected uploads also stay quiet.
        if self._response_started:
            self.close_connection = True
            return
        try:
            self._json(status, {"error": message})
        except OSError:
            self.close_connection = True

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_DELETE = _handle


def create_server(config: RelayConfig, host="127.0.0.1", port: int | None = None, clock=time.time):
    if host != "127.0.0.1":
        raise ValueError("Media relay must listen on loopback behind an HTTPS proxy")
    return RelayHTTPServer((host, config.port if port is None else port), config, clock)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = RelayConfig.from_env()
        server = create_server(config)
    except (OSError, ValueError) as exc:
        LOGGER.error("Startup configuration failed: %s", exc)
        return 1

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    LOGGER.info("Media relay listening on loopback port %d", server.server_port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
