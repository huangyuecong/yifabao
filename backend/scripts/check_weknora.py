"""Run the read-only platform preflight using server-side configuration."""

import sys
from pathlib import Path

CONTENT_SRC = Path(__file__).resolve().parents[1] / "services" / "content_service" / "src"
if str(CONTENT_SRC) not in sys.path:
    sys.path.insert(0, str(CONTENT_SRC))

from content_service.weknora_diagnostics import main

if __name__ == "__main__":
    raise SystemExit(main())
