"""
fs_inspect — read-only filesystem and FLAC tag inspection.

No write, move, rename, or delete operation exists in this module. Adding one
later means adding a new tool with its own dry-run gate, not extending this.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import config
from ..safety import SafetyError, cap_rows, require_dir, require_file, resolve_in_jail

_SHOW_RE = re.compile(config.SHOW_FOLDER_PATTERN)


def _classify(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in config.ARCHIVAL_FORMATS:
        return "archival"
    if suffix in config.LOSSY_FORMATS:
        return "lossy"
    if suffix in config.AUDIO_FORMATS:
        return "audio-other"
    if suffix in config.LINEAGE_FILES:
        return "lineage"
    return "other"


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


# ------------------------------------------------------------------------------
# Operations
# ------------------------------------------------------------------------------

def _op_list(path: Path) -> dict:
    """Immediate children of a directory, with classification."""
    require_dir(path)
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            stat = child.stat()
            size = stat.st_size if child.is_file() else None
        except OSError:
            size = None
        entries.append(
            {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "kind": None if child.is_dir() else _classify(child),
                "size": _human(size) if size is not None else None,
            }
        )

    entries, truncated = cap_rows(entries)
    return {
        "path": str(path),
        "count": len(entries),
        "truncated": truncated,
        "entries": entries,
    }


def _op_stat(path: Path) -> dict:
    """Recursive rollup of a directory: file counts by kind, total size."""
    require_dir(path)
    counts: dict[str, int] = {}
    total_size = 0
    file_count = 0
    dir_count = 0

    for item in path.rglob("*"):
        try:
            if item.is_dir():
                dir_count += 1
                continue
            file_count += 1
            kind = _classify(item)
            counts[kind] = counts.get(kind, 0) + 1
            total_size += item.stat().st_size
        except OSError:
            continue

    return {
        "path": str(path),
        "directories": dir_count,
        "files": file_count,
        "by_kind": counts,
        "total_size": _human(total_size),
        "total_bytes": total_size,
    }


def _op_tags(path: Path) -> dict:
    """Read FLAC Vorbis comments for every .flac directly inside `path`."""
    try:
        from mutagen.flac import FLAC
    except ImportError as exc:
        raise SafetyError(
            "mutagen is not installed. Run: pip install mutagen"
        ) from exc

    require_dir(path)
    flacs = sorted(p for p in path.iterdir() if p.suffix.lower() == ".flac")
    if not flacs:
        return {"path": str(path), "tracks": [], "note": "No FLAC files in this folder."}

    tracks = []
    for flac_path in flacs:
        try:
            audio = FLAC(flac_path)
        except Exception as exc:  # corrupt/unreadable file is data, not a crash
            tracks.append({"file": flac_path.name, "error": str(exc)})
            continue

        tags = {k.lower(): v[0] for k, v in audio.tags.items()} if audio.tags else {}
        tracks.append(
            {
                "file": flac_path.name,
                "tracknumber": tags.get("tracknumber"),
                "title": tags.get("title"),
                "artist": tags.get("artist"),
                "album": tags.get("album"),
                "date": tags.get("date"),
                "venue": tags.get("venue"),
                "sample_rate": audio.info.sample_rate,
                "bit_depth": getattr(audio.info, "bits_per_sample", None),
                "length_sec": round(audio.info.length, 1),
            }
        )

    tracks, truncated = cap_rows(tracks)
    return {
        "path": str(path),
        "track_count": len(tracks),
        "truncated": truncated,
        "tracks": tracks,
    }


def _op_audit(path: Path) -> dict:
    """Check immediate subfolders against collection standards.

    Reports defects only — naming deviations, lossy files where FLAC is
    expected, folders with no audio, and missing lineage material. Reports
    nothing as "fixed"; this operation cannot change anything.
    """
    require_dir(path)
    findings = []
    checked = 0

    for folder in sorted(p for p in path.iterdir() if p.is_dir()):
        checked += 1
        defects = []

        match = _SHOW_RE.match(folder.name)
        if not match:
            defects.append(
                "folder name does not match 'YYYY-MM-DD Venue, City, ST, USA'"
            )

        kinds: dict[str, int] = {}
        for item in folder.rglob("*"):
            if item.is_file():
                kind = _classify(item)
                kinds[kind] = kinds.get(kind, 0) + 1

        if kinds.get("lossy"):
            defects.append(
                f"{kinds['lossy']} lossy file(s) present where FLAC is the standard"
            )
        if not kinds.get("archival"):
            defects.append("no FLAC files found")
        if not kinds.get("lineage"):
            defects.append("no lineage/provenance files (txt, ffp, md5) present")

        if defects:
            findings.append(
                {
                    "folder": folder.name,
                    "parsed_date": match.group("date") if match else None,
                    "defects": defects,
                }
            )

    findings, truncated = cap_rows(findings)
    return {
        "path": str(path),
        "folders_checked": checked,
        "folders_with_defects": len(findings),
        "truncated": truncated,
        "findings": findings,
    }


def _op_find(path: Path, pattern: str) -> dict:
    """Glob search beneath `path`."""
    require_dir(path)
    if not pattern:
        raise SafetyError("The 'pattern' argument is required for operation 'find'.")

    hits = []
    for item in path.rglob(pattern):
        hits.append(
            {
                "path": str(item),
                "type": "dir" if item.is_dir() else "file",
                "kind": None if item.is_dir() else _classify(item),
            }
        )
        if len(hits) >= config.MAX_RESULT_ROWS * 2:
            break

    hits, truncated = cap_rows(hits)
    return {
        "root": str(path),
        "pattern": pattern,
        "match_count": len(hits),
        "truncated": truncated,
        "matches": hits,
    }


def _op_read_text(path: Path) -> dict:
    """Read a small lineage/provenance text file."""
    require_file(path)
    if path.suffix.lower() not in config.LINEAGE_FILES:
        raise SafetyError(
            f"Only lineage/text files may be read "
            f"({', '.join(sorted(config.LINEAGE_FILES))}). Got '{path.suffix}'."
        )
    if path.stat().st_size > 256_000:
        raise SafetyError(f"File too large to inline ({_human(path.stat().st_size)}).")

    return {
        "path": str(path),
        "content": path.read_text(encoding="utf-8", errors="replace"),
    }


_OPERATIONS = {
    "list": _op_list,
    "stat": _op_stat,
    "tags": _op_tags,
    "audit": _op_audit,
    "read_text": _op_read_text,
}


def fs_inspect(operation: str, path: str, pattern: str | None = None) -> dict:
    """Entry point registered as the `fs_inspect` tool."""
    target = resolve_in_jail(path)

    if operation == "find":
        return _op_find(target, pattern or "")

    handler = _OPERATIONS.get(operation)
    if handler is None:
        valid = ", ".join(sorted([*_OPERATIONS, "find"]))
        raise SafetyError(f"Unknown operation '{operation}'. Valid: {valid}")

    return handler(target)
