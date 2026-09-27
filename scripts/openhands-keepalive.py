#!/usr/bin/env python3
"""OpenHands keepalive entrypoint — assembles split source parts."""
from pathlib import Path
import runpy
import sys

_dir = Path(__file__).resolve().parent
parts = []
for i in range(4):
    parts.append((_dir / f"_keepalive_part{i}.py").read_text(encoding="utf-8"))
src = "".join(parts)
target = _dir / "_keepalive_assembled.py"
target.write_text(src, encoding="utf-8")
sys.argv[0] = str(target)
runpy.run_path(str(target), run_name="__main__")
