#!/usr/bin/env python3
"""Write OpenAPI schema for the frontend Orval generator."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

backend_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_root))

os.environ.setdefault("JWT_SECRET", "export-openapi-local-dev-secret-32chars")

from app.main import app  # noqa: E402


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    output = repo_root / "frontend" / "openapi.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
