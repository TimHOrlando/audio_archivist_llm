#!/usr/bin/env python3
"""
Create a one-line deployable bundle of the most recently changed runtime files.

Usage (on the dev/build machine):
    python scripts/make_bundle.py

It prints a single shell command that you paste into the server shell (hal9003).
That command will:
  - create a temporary bundle archive
  - extract the listed files into /opt/lemieux
  - restart the uvicorn web app

Why this exists: hal9003 cannot easily authenticate to GitHub, and scp from this
box is blocked by host-key/SSH issues. A base64-encoded bundle survives any
clipboard-or-paste transfer path.
"""
from __future__ import annotations

import gzip
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RELATIVE_FILES = [
    "lemieux/web.py",
    "lemieux/templates/index.html",
]


def run() -> int:
    files = [PROJECT_ROOT / p for p in RELATIVE_FILES]
    for f in files:
        if not f.is_file():
            print(f"missing: {f}", file=sys.stderr)
            return 1

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "bundle.tar.gz"
        subprocess.run(
            ["tar", "-czf", str(archive), "-C", str(PROJECT_ROOT)]
            + RELATIVE_FILES,
            check=True,
        )
        payload = archive.read_bytes()

    encoded = __import__("base64").b64encode(payload).decode("ascii")

    # Print a single command the user can paste on hal9003.
    cmd = (
        f"cd /opt/lemieux && base64 -d <<'BUNDLE_END' | tar -xzf - && "
        f"source .venv/bin/activate && pkill -f 'uvicorn lemieux.web:app' || true && "
        f"python -m uvicorn lemieux.web:app --host 0.0.0.0 --port 8080\n"
        f"{encoded}\n"
        f"BUNDLE_END"
    )
    print(cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
