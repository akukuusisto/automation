#!/usr/bin/env python3
"""OpenHands Cloud keepalive entrypoint (parts assembled at import time)."""
from pathlib import Path
import sys

_dir = Path(__file__).resolve().parent
_parts = sorted(_dir.glob("_keepalive_part*.py"))
if not _parts:
    print("keepalive parts missing", file=sys.stderr)
    sys.exit(1)
_code = "".join(p.read_text(encoding="utf-8") for p in _parts)
exec(compile(_code, str(_dir / "openhands-keepalive.py"), "exec"), globals())
