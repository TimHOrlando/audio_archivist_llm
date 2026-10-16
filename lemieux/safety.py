"""
Lemieux — enforcement layer.

The system prompt makes the model *want* to behave. This module makes
misbehavior impossible. Every filesystem path reaching a tool passes through
resolve_in_jail() first.
"""

from __future__ import annotations

from pathlib import Path

from . import config


class SafetyError(Exception):
    """Raised when an operation is refused. Returned to the model as an error
    string so it can explain the refusal rather than retrying blindly."""


def resolve_in_jail(raw: str) -> Path:
    """Resolve `raw` to an absolute path and prove it lives inside a media root.

    Resolution happens BEFORE the containment check, so `..` traversal and
    symlinks pointing outside the jail are both caught — a symlink at
    /disks/evo/escape -> /etc resolves to /etc and is refused.

    Raises SafetyError on anything outside the jail.
    """
    if not raw or not str(raw).strip():
        raise SafetyError("Empty path.")

    candidate = Path(str(raw).strip()).expanduser()

    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop
        raise SafetyError(f"Could not resolve path: {exc}") from exc

    for root in config.MEDIA_ROOTS:
        if resolved == root or root in resolved.parents:
            return resolved

    raise SafetyError(
        f"Path '{resolved}' is outside the permitted media roots. "
        f"Allowed roots: {', '.join(str(r) for r in config.MEDIA_ROOTS)}"
    )


def require_dir(path: Path) -> Path:
    """Assert `path` exists and is a directory."""
    if not path.exists():
        raise SafetyError(f"Path does not exist: {path}")
    if not path.is_dir():
        raise SafetyError(f"Not a directory: {path}")
    return path


def require_file(path: Path) -> Path:
    """Assert `path` exists and is a regular file."""
    if not path.exists():
        raise SafetyError(f"Path does not exist: {path}")
    if not path.is_file():
        raise SafetyError(f"Not a file: {path}")
    return path


def cap_rows(rows: list, limit: int | None = None) -> tuple[list, bool]:
    """Truncate `rows` to the configured ceiling.

    Returns (rows, truncated). The caller must surface `truncated` to the model
    so it never reports a partial list as complete.
    """
    limit = limit or config.MAX_RESULT_ROWS
    if len(rows) > limit:
        return rows[:limit], True
    return rows, False


def scrub(text: str) -> str:
    """Remove the Plex token from any string before it reaches the model or a
    log. Tokens leak through URLs in plexapi objects and exception messages."""
    if config.PLEX_TOKEN and config.PLEX_TOKEN in text:
        return text.replace(config.PLEX_TOKEN, "<redacted>")
    return text
