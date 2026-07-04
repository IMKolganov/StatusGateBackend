#!/usr/bin/env python3
"""Export OpenAPI schema to frontend/openapi.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
OUTPUT = REPO_ROOT / "frontend" / "openapi.json"

sys.path.insert(0, str(BACKEND_ROOT))

from app.core.openapi import setup_openapi  # noqa: E402
from app.main import app  # noqa: E402


def main() -> int:
    setup_openapi(app)
    schema = app.openapi()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
