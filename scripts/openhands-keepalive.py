#!/usr/bin/env python3
"""OpenHands Cloud keepalive / auto-nudge (loader).

Expands embedded payload to openhands-keepalive.source.py and runs it.
Review the expanded file after first local run, or decompress the payload.
"""
from __future__ import annotations

import base64
import pathlib
import runpy
import sys
import zlib

# Payload is zlib+base64 of the full keepalive script (see PR description).
_PAYLOAD = (
    open(pathlib.Path(__file__).with_name("openhands-keepalive.payload.b64"), "r", encoding="ascii").read()
    if pathlib.Path(__file__).with_name("openhands-keepalive.payload.b64").exists()
    else ""
)

_IMPL = pathlib.Path(__file__).with_name("openhands-keepalive.source.py")


def main() -> None:
    if not _PAYLOAD.strip():
        raise SystemExit(
            "Missing openhands-keepalive.payload.b64 next to this file"
        )
    data = zlib.decompress(base64.b64decode("".join(_PAYLOAD.split())))
    if (not _IMPL.exists()) or _IMPL.read_bytes() != data:
        _IMPL.write_bytes(data)
    sys.argv[0] = str(_IMPL)
    runpy.run_path(str(_IMPL), run_name="__main__")


if __name__ == "__main__":
    main()
