#!/usr/bin/env python3
"""Validate reviewed hosted MCP contracts without contacting external services."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.mcp_compatibility import validate_sources  # noqa: E402

if __name__ == "__main__":
    validate_sources(ROOT)
    print("Hosted MCP source contracts are valid and covered.")
