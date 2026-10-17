#!/usr/bin/env python3
"""
Update Lemieux source files from GitHub without needing git or SSH.

Requires a GitHub personal access token with "repo" scope, set as
GITHUB_TOKEN in the environment or in .env. The token only needs read access;
it is used to call the GitHub contents API for a private repository.

Usage on a target machine (e.g. hal9003):
    GITHUB_TOKEN=<pat> python scripts/update_from_github.py

Files updated by this script are listed in UPDATE_FILES. The venv is not
modified; only runtime code is refreshed.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

UPDATE_FILES = [
    "lemieux/web.py",
    "lemieux/templates/index.html",
]

REPO = "TimHOrlando/audio_archivist_llm"
BRANCH = "main"
API_BASE = f"https://api.github.com/repos/{REPO}/contents"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"\'')
        if key and key not in os.environ:
            os.environ[key] = value


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Set GITHUB_TOKEN to a PAT with 'repo' scope. "
            "Create one at https://github.com/settings/tokens",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return token


def _fetch_file(token: str, path: str) -> bytes:
    url = f"{API_BASE}/{path}?ref={BRANCH}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "lemieux-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"GitHub API error for {path}: {exc.code} {exc.reason}", file=sys.stderr)
        try:
            print(exc.read().decode("utf-8"), file=sys.stderr)
        except Exception:
            pass
        raise
    content = data.get("content", "")
    if not content:
        raise ValueError(f"No content returned for {path}")
    return base64.b64decode(content.replace("\n", ""))


def run() -> int:
    token = _token()
    root = Path(__file__).resolve().parent.parent
    for rel_path in UPDATE_FILES:
        data = _fetch_file(token, rel_path)
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(f"Updated {rel_path} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
