"""Offline integration tests: python -m unittest discover -s services/media_relay."""

import dataclasses
import hashlib
import http.client
import json
import logging
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from service import MAX_CONNECTIONS, MAX_UPLOAD_BYTES, RelayConfig, RelayStore, create_server

VIDEO = (
    struct.pack(">I", 24)
    + b"ftypisom"
    + b"\x00\x00\x00\x00"
    + b"isommp42"
    + struct.pack(">I", 1032)
    + b"mdat"
    + bytes(range(256)) * 4
)


class RelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = 1800000000
        self.config = RelayConfig(
            data_dir=Path(self.directory.name),
            public_base_url="https://203.0.113.10",
            upload_token="test-token-" + "a" * 48,
            signing_secret=b"b" * 64,
        )
        self.server = create_server(self.config, port=0, clock=lambda: self.now)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}
        )
        self.thread.start()
        self.auth = {"Authorization": "Bearer " + self.config.upload_token}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, body=VIDEO, content_type="video/mp4"):
        status, _, response = self.request(
            "POST", "/v1/objects", body, {**self.auth, "Content-Type": content_type}
        )
        self.assertEqual(status, 201, response)
        result = json.loads(response)
        split = urlsplit(result["url"])
        self.assertEqual(split.scheme, "https")
        self.assertEqual(split.netloc, "203.0.113.10")
        self.assertEqual(result["expires_at"], self.now + 1800)
        self.assertEqual(split.path, "/v1/media/" + result["cleanup_token"])
        return split.path + "?" + split.query, result

    def raw_request(self, headers, body=b""):
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        connection.settimeout(2)
        try:
            connection.sendall(headers.encode("ascii") + b"\r\n\r\n" + body)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def rejected_upload(self, size=len(VIDEO), content_type="video/mp4"):
        """An admission error must arrive without transmitting the rejected body."""
        return self.raw_request(
            "POST /v1/objects HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
            + self.config.upload_token
            + "\r\nContent-Type: "
            + content_type
            + "\r\nContent-Length: "
            + str(size)
        )

    def test_health_is_public_and_identifies_api(self):
        status, headers, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body), {"status": "ok", "service": "yifabao-media-relay", "api_version": 1}
        )
        self.assertEqual(headers["Connection"], "close")

    def test_authentication_rejected_before_waiting_for_body(self):
        for authorization in ("", "Authorization: Bearer wrong\r\n"):
            status, headers, _ = self.raw_request(
                "POST /v1/objects HTTP/1.1\r\nHost: localhost\r\n"
                + authorization
                + "Content-Length: 100000\r\nContent-Type: video/mp4"
            )
            self.assertEqual(status, 401)
            self.assertEqual(headers["Connection"], "close")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.assertEqual(self.request("POST", "/v1/cleanup")[0], 401)
        self.assertEqual(self.request("DELETE", "/v1/objects/" + "a" * 32)[0], 401)

    def test_expect_continue_validates_auth_and_length_first(self):
        status, _, _ = self.raw_request(
            "POST /v1/objects HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\n"
            "Content-Type: video/mp4\r\nContent-Length: 1000"
        )
        self.assertEqual(status, 401)
        status, _, _ = self.raw_request(
            "POST /v1/objects HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\n"
            + "Authorization: Bearer "
            + self.config.upload_token
            + "\r\n"
            + "Content-Type: video/mp4\r\nContent-Length: "
            + str(MAX_UPLOAD_BYTES + 1)
        )
        self.assertEqual(status, 413)

    def test_happy_upload_download_head_ranges_and_idempotent_delete(self):
        path, result = self.upload()
        status, headers, body = self.request("GET", path)
        self.assertEqual((status, body), (200, VIDEO))
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        status, headers, body = self.request("HEAD", path)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(VIDEO))
        cases = [
            ("bytes=8-19", 8, 19),
            ("bytes=12-", 12, len(VIDEO) - 1),
            ("bytes=-7", len(VIDEO) - 7, len(VIDEO) - 1),
            ("bytes=0-99999", 0, len(VIDEO) - 1),
            ("bytes=-99999", 0, len(VIDEO) - 1),
        ]
        for byte_range, start, end in cases:
            with self.subTest(byte_range=byte_range):
                status, headers, body = self.request("GET", path, headers={"Range": byte_range})
                self.assertEqual((status, body), (206, VIDEO[start : end + 1]))
                self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{len(VIDEO)}")
        status, headers, body = self.request("HEAD", path, headers={"Range": "bytes=8-19"})
        self.assertEqual((status, body, headers["Content-Length"]), (206, b"", "12"))
        delete_path = "/v1/objects/" + result["cleanup_token"]
        self.assertEqual(self.request("DELETE", delete_path, headers=self.auth)[0], 204)
        self.assertEqual(self.request("DELETE", delete_path, headers=self.auth)[0], 204)
        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_bad_ranges_return_416_and_total_length(self):
        path, _ = self.upload()
        for byte_range in (
            "bytes=99999-",
            "bytes=20-10",
            "bytes=-0",
            "bytes=-",
            "bytes=0-1,4-8",
            "bytes=invalid",
            "items=0-1",
            "bytes=" + "9" * 100 + "-",
        ):
            with self.subTest(byte_range=byte_range):
                status, headers, _ = self.request("GET", path, headers={"Range": byte_range})
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], f"bytes */{len(VIDEO)}")

    def test_signed_link_expiry_and_tampering(self):
        path, _ = self.upload()
        self.assertEqual(self.request("GET", path.replace("signature=", "signature=0"))[0], 403)
        self.assertEqual(self.request("GET", path + "&expires=1800000000")[0], 403)
        self.assertEqual(self.request("GET", path + "&unapproved=1")[0], 403)
        self.assertEqual(self.request("GET", path.split("?")[0])[0], 403)
        self.now += 1800
        self.assertEqual(self.request("GET", path)[0], 403)

    def test_authenticated_cleanup_respects_24_hour_retention(self):
        path, _ = self.upload()
        status, _, body = self.request("POST", "/v1/cleanup", headers=self.auth)
        self.assertEqual((status, json.loads(body)), (200, {"deleted": 0}))
        self.now += 86400
        status, _, body = self.request("POST", "/v1/cleanup", headers=self.auth)
        self.assertEqual((status, json.loads(body)), (200, {"deleted": 1}))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_content_length_missing_duplicate_oversize_and_transfer_encoding(self):
        prefix = (
            "POST /v1/objects HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
            + self.config.upload_token
            + "\r\nContent-Type: video/mp4"
        )
        for suffix, expected in [
            ("", 411),
            ("\r\nContent-Length: -1", 400),
            ("\r\nContent-Length: 50\r\nContent-Length: 50", 400),
            ("\r\nContent-Length: 50, 50", 400),
            ("\r\nContent-Length: " + str(MAX_UPLOAD_BYTES + 1), 413),
            ("\r\nContent-Length: 50\r\nTransfer-Encoding: chunked", 400),
        ]:
            with self.subTest(suffix=suffix):
                self.assertEqual(self.raw_request(prefix + suffix)[0], expected)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_invalid_video_does_not_leave_files(self):
        self.assertEqual(self.rejected_upload(content_type="text/plain")[0], 415)
        self.assertEqual(self.rejected_upload(size=5)[0], 415)
        for video, media_type in [
            (b"not a video" * 100, "video/mp4"),
            (b"\xff\xff\xff\xff" + VIDEO[4:], "video/quicktime"),
        ]:
            self.assertEqual(
                self.request(
                    "POST", "/v1/objects", video, {**self.auth, "Content-Type": media_type}
                )[0],
                415,
            )
            self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        path, _ = self.upload(content_type="video/quicktime")
        self.assertEqual(self.request("GET", path)[1]["Content-Type"], "video/quicktime")

    def test_interrupted_body_is_rejected_and_removed(self):
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        try:
            headers = (
                "POST /v1/objects HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                + self.config.upload_token
                + "\r\nContent-Type: video/mp4\r\nContent-Length: "
                + str(len(VIDEO) + 100)
                + "\r\n\r\n"
            )
            connection.sendall(headers.encode("ascii") + VIDEO)
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_traversal_absolute_urls_and_directory_listing_are_rejected(self):
        for path in (
            "/",
            "/v1/media",
            "/v1/media/../service.py",
            "/v1/media/%2e%2e%2fservice.py",
            "/v1/objects/../../service.py",
            "/v1/media/" + "A" * 32,
            "https://evil.example/v1/media/" + "a" * 32,
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_capacity_refuses_upload_and_delete_frees_space(self):
        self.server.store.config = dataclasses.replace(
            self.config, max_storage_bytes=len(VIDEO) + 1
        )
        _, result = self.upload()
        self.assertEqual(self.rejected_upload()[0], 507)
        self.request("DELETE", "/v1/objects/" + result["cleanup_token"], headers=self.auth)
        self.upload()

    def test_low_disk_space_refuses_upload_without_artifacts(self):
        fake_usage = type("DiskUsage", (), {"free": 0})()
        with patch("service.shutil.disk_usage", return_value=fake_usage):
            self.assertEqual(self.rejected_upload()[0], 507)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_only_four_uploads_can_be_in_flight(self):
        connections = []
        try:
            for _ in range(4):
                connection = socket.create_connection(
                    ("127.0.0.1", self.server.server_port), timeout=2
                )
                headers = (
                    "POST /v1/objects HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                    + self.config.upload_token
                    + "\r\nContent-Type: video/mp4\r\nContent-Length: "
                    + str(len(VIDEO))
                    + "\r\n\r\n"
                )
                connection.sendall(headers.encode("ascii"))
                connections.append(connection)
            deadline = time.monotonic() + 2
            while len(self.server.store._active) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.server.store._active), 4)
            self.assertEqual(self.rejected_upload()[0], 503)
            for connection in connections:
                connection.sendall(VIDEO)
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 201)
                response.read()
        finally:
            for connection in connections:
                connection.close()

    def test_restart_retains_valid_objects_and_removes_orphans(self):
        _, result = self.upload()
        directory = Path(self.directory.name)
        for index, suffix in enumerate((".media", ".upload", ".json.tmp", ".json")):
            (directory / (str(index) * 32 + suffix)).write_bytes(b"orphan")
        fresh_store = RelayStore(self.config, clock=lambda: self.now)
        self.assertEqual(set(fresh_store._records), {result["cleanup_token"]})
        self.assertEqual(len(list(directory.iterdir())), 2)
        record, source = fresh_store.open_download(
            result["cleanup_token"],
            self.now + 1800,
            fresh_store.signature(result["cleanup_token"], self.now + 1800),
        )
        with source:
            self.assertEqual(hashlib.sha256(source.read()).digest(), hashlib.sha256(VIDEO).digest())

    def test_request_logs_do_not_contain_bearer_or_signed_url(self):
        logger = logging.getLogger("media_relay")
        previous_disable = logging.root.manager.disable
        try:
            # Other application imports can disable existing loggers globally.
            # Exercise the service's real log calls, then restore that state.
            logging.disable(logging.NOTSET)
            with (
                patch.object(logger, "disabled", False),
                self.assertLogs(logger, level="INFO") as captured,
            ):
                path, _ = self.upload()
                self.request("GET", path)
        finally:
            logging.disable(previous_disable)
        log = "\n".join(captured.output)
        self.assertIn("request status=201", log)
        self.assertIn("request status=200", log)
        self.assertNotIn(self.config.upload_token, log)
        self.assertNotIn("signature", log)
        self.assertNotIn(path, log)

    def test_total_handler_concurrency_is_bounded(self):
        for _ in range(MAX_CONNECTIONS):
            self.assertTrue(self.server._request_slots.acquire(blocking=False))
        try:
            # The accept loop sheds excess connections before reading a request.
            # Reading its response before sending avoids OS-specific resets when
            # a server closes a socket with unread inbound application bytes.
            with socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=2
            ) as connection:
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual((response.status, response.read()), (503, b""))
                self.assertEqual(response.getheader("Retry-After"), "5")
        finally:
            for _ in range(MAX_CONNECTIONS):
                self.server._request_slots.release()
        self.assertEqual(self.request("GET", "/healthz")[0], 200)


class ConfigurationTests(unittest.TestCase):
    def test_separate_https_port_is_supported_and_repr_omits_secrets(self):
        config = RelayConfig(
            data_dir=Path("unused"),
            public_base_url="https://203.0.113.10:8443",
            upload_token="a" * 32,
            signing_secret=b"b" * 32,
        )
        self.assertEqual(config.public_base_url, "https://203.0.113.10:8443")
        self.assertNotIn("a" * 32, repr(config))
        self.assertNotIn("b" * 32, repr(config))

    def test_invalid_origins_credentials_and_ttls_are_rejected(self):
        defaults = {
            "data_dir": Path("unused"),
            "public_base_url": "https://203.0.113.10",
            "upload_token": "a" * 32,
            "signing_secret": b"b" * 32,
        }
        for values in [
            {"public_base_url": "http://203.0.113.10"},
            {"public_base_url": "https://203.0.113.10/path"},
            {"public_base_url": "https://user:pass@203.0.113.10"},
            {"public_base_url": "https://203.0.113.10?token=secret"},
            {"upload_token": "too-short"},
            {"signing_secret": b"too-short"},
            {"url_ttl_seconds": 59},
            {"url_ttl_seconds": 3601},
            {"object_ttl_seconds": 60},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                RelayConfig(**{**defaults, **values})


if __name__ == "__main__":
    unittest.main()
