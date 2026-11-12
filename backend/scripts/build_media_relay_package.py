"""Build a small, source-only Baota media relay package; never include secrets."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "preflight.sh",
    "install-service.sh",
    "install-certificate.sh",
    "nginx-http-acme.conf.template",
    "nginx-https.conf.template",
    "README.zh-CN.md",
)


def build(output: Path) -> None:
    content = {
        name: (ROOT / "deployment/media-relay" / name).read_text(encoding="utf-8-sig")
        for name in FILES
    }
    content["server.py"] = (ROOT / "services/media_relay/service.py").read_text(
        encoding="utf-8-sig"
    )
    payload = {name: text.replace("\r\n", "\n").encode("utf-8") for name, text in content.items()}
    payload["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(payload.items())
    ).encode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            info = ZipInfo("yifabao-media-relay/" + name)
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)
    with ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(payload):
            raise RuntimeError("Package integrity check failed")
        for name, data in payload.items():
            if archive.read("yifabao-media-relay/" + name) != data:
                raise RuntimeError("Packaged file verification failed")
    print(f"Package: {output.resolve()}")
    print(f"Files: {len(payload)}; no credentials or user media included.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)
