"""
Lemieux — configuration and environment.

Everything the harness is allowed to touch is declared here. If a path is not
in MEDIA_ROOTS, no tool in this package can read it.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load KEY=VALUE pairs from .env into os.environ.

    Hand-rolled rather than a python-dotenv dependency — the format here is
    trivial and one less package is one less supply-chain surface for something
    that holds a Plex token.

    Real environment variables always win: a value already in os.environ is
    never overwritten, so `PLEX_TOKEN=x python -m lemieux.agent` and the
    VS Code envFile both still behave as expected.
    """
    if not path.is_file():
        return

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        # Strip one layer of matching quotes if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


_load_dotenv()

# ------------------------------------------------------------------------------
# Model
# ------------------------------------------------------------------------------

# Point straight at a base model. The persona is injected as a system message
# at runtime (see prompt.py), so no `ollama create` step is required — and
# deriving a model from a :cloud base is what produced "does not support chat".
MODEL = os.getenv("LEMIEUX_MODEL", "kimi-k3:cloud")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Inject the Modelfile's SYSTEM block as a system message. Set to 0/false only
# when MODEL is a model built by `ollama create`, which already carries the
# prompt — injecting again would send it twice.
INJECT_SYSTEM = os.getenv("LEMIEUX_INJECT_SYSTEM", "1").lower() not in (
    "0",
    "false",
    "no",
)

# Max tool-call round trips before the agent loop gives up. Prevents a model
# that gets confused from hammering the filesystem in a loop.
# A release sweep legitimately needs many round trips: inventory, then a few
# searches per artist, then a 'have' check per candidate. 12 was too low — a
# single-artist sweep burned through it before checking holdings on anything.
MAX_TOOL_ITERATIONS = int(os.getenv("LEMIEUX_MAX_ITERATIONS", "30"))

# ------------------------------------------------------------------------------
# Plex
# ------------------------------------------------------------------------------

PLEX_BASEURL = os.getenv("PLEX_BASEURL", "http://192.168.4.254:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN")  # never hardcode; never logged
PLEX_MACHINE_ID = os.getenv(
    "PLEX_MACHINE_ID", "c389bcb3ea42d137fc4be13eb8aa2afccf991c1a"
)
PLEX_TIMEOUT = int(os.getenv("PLEX_TIMEOUT", "30"))

# Hard ceiling on rows returned to the model in one call. Keeps a "list
# everything" request from blowing out the context window.
MAX_RESULT_ROWS = int(os.getenv("LEMIEUX_MAX_ROWS", "250"))

# ------------------------------------------------------------------------------
# Filesystem scope — the path jail
# ------------------------------------------------------------------------------
#
# Only these roots (and their descendants) are readable. Anything resolving
# outside them is refused by safety.resolve_in_jail(), including via symlink.

_DEFAULT_ROOTS = [
    "/disks/wd",
    "/disks/evo",
    "/disks/media_storage",
    "/home/tim/logs",
]


def _load_roots() -> list[Path]:
    """Roots from LEMIEUX_MEDIA_ROOTS (os.pathsep-separated) or the defaults.

    Non-existent roots are kept rather than dropped so that a typo'd or
    unmounted path surfaces as an explicit error instead of silently
    shrinking the jail.
    """
    raw = os.getenv("LEMIEUX_MEDIA_ROOTS")
    entries = raw.split(os.pathsep) if raw else _DEFAULT_ROOTS
    roots: list[Path] = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        # strict=False: tolerate unmounted roots at import time.
        roots.append(Path(entry).expanduser().resolve(strict=False))
    return roots


MEDIA_ROOTS: list[Path] = _load_roots()

# ------------------------------------------------------------------------------
# Collection conventions
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# Release watch
# ------------------------------------------------------------------------------

# Artists whose catalogs are swept for releases not already held.
WATCH_ARTISTS = [
    a.strip()
    for a in os.getenv(
        "LEMIEUX_WATCH_ARTISTS",
        "Grateful Dead|Dead|Further|Dead & Company|The Warlocks|Jerry Garcia|Jerry Garcia Band",
    ).split("|")
    if a.strip()
]

# Plex sections searched when building held inventory. Empty = every music
# section. Default to everything on purpose: a release missing from inventory
# gets reported as a gap Tim already owns, and that false alarm is the failure
# mode that makes a watch worth ignoring.
INVENTORY_SECTIONS = [
    s.strip()
    for s in os.getenv("LEMIEUX_INVENTORY_SECTIONS", "").split("|")
    if s.strip()
]

ARCHIVAL_FORMATS = {".flac"}
LOSSY_FORMATS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
AUDIO_FORMATS = ARCHIVAL_FORMATS | LOSSY_FORMATS | {".wav", ".aiff", ".shn", ".ape"}

# Lineage / provenance material that must never be treated as disposable.
LINEAGE_FILES = {
    ".txt", ".ffp", ".md5", ".st5", ".nfo", ".log", ".cue", ".sbe",
}

# Live show folder standard: "YYYY-MM-DD Venue, City, ST, USA"
#
# Venue is `.+` (greedy, backtracking), NOT `[^,]+`. Venue names legitimately
# contain commas — "Barton Hall, Cornell University" — and a comma-free venue
# field produces a false positive on correctly named folders. The last three
# comma-separated fields are anchored from the right instead.
SHOW_FOLDER_PATTERN = (
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<venue>.+),\s*"
    r"(?P<city>[^,]+),\s*"
    r"(?P<state>[A-Z]{2}),\s*"
    r"(?P<country>[A-Z]{2,3})$"
)


# -----------------------------------------------------------------------------
# Web chat
# -----------------------------------------------------------------------------

WEB_HOST = os.getenv("LEMIEUX_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("LEMIEUX_WEB_PORT", "8000"))
WEB_PASSWORD = os.getenv("LEMIEUX_WEB_PASSWORD")


def config_report() -> dict:
    """Non-secret snapshot of effective config, for startup display."""
    return {
        "dotenv_loaded": (PROJECT_ROOT / ".env").is_file(),
        "model": MODEL,
        "inject_system_prompt": INJECT_SYSTEM,
        "ollama_host": OLLAMA_HOST,
        "plex_baseurl": PLEX_BASEURL,
        "plex_token_present": bool(PLEX_TOKEN),
        "media_roots": [str(p) for p in MEDIA_ROOTS],
        "roots_mounted": {str(p): p.is_dir() for p in MEDIA_ROOTS},
        "max_result_rows": MAX_RESULT_ROWS,
        "web_host": WEB_HOST,
        "web_port": WEB_PORT,
        "web_password_set": bool(WEB_PASSWORD),
    }
