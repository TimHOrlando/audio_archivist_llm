"""
Release ledger — what the watch has already told Tim about.

A scheduled watch that repeats itself is worse than no watch. Report
GarciaLive Vol 23 every week and he stops reading; then a genuinely new
release arrives and he misses it. The watch fails exactly when it matters.

So the ledger records what was reported, and a release is suppressed on later
runs UNLESS something about it actually changed:

  - verdict changed      — 'uncertain' resolved, or he acquired it
  - release date changed — a slipped or newly-announced date
  - status changed       — pre-order opened, or it finally shipped

Entries are keyed by normalized title, so "Dave's Picks, Vol. 59" and
"Daves Picks Volume 59" are one release, not two.

Storage is a single JSON file written atomically. No database, because this
runs unattended on a media server and a corrupted ledger that silences the
watch would be the worst possible failure.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path

from . import config
from .config import PROJECT_ROOT
from .matching import normalize_title, similarity

LEDGER_PATH = Path(
    os.getenv("LEMIEUX_LEDGER", str(PROJECT_ROOT / "release_ledger.json"))
)

# A release is kept in the ledger so we know what was announced, but only
# releases actually held in the library are suppressed from future reports.
# If a release remains not_held, it is re-reported every sweep until it is
# acquired (or its verdict changes to held).
SUPPRESS_ONLY_HELD = (
    os.getenv("LEMIEUX_LEDGER_SUPPRESS_ONLY_HELD", "1").lower() not in ("0", "false", "no")
)

_SCHEMA_VERSION = 1

# A release is cited with or without its artist depending on the source:
#   "Jerry Garcia Band - Live at The Warfield"
#   "Live at The Warfield"
# Keyed naively those are two entries, and the watch reports the same release
# twice — the exact repetition the ledger exists to prevent. Artist prefixes
# are stripped before keying.
_EXTRA_ARTIST_PREFIXES = (
    "Jerry Garcia Band",
    "Jerry Garcia",
    "Grateful Dead",
    "Dead & Company",
    "Dead and Company",
    "Bob Weir",
    "Phil Lesh and Friends",
    "Phil Lesh",
    "JGB",
)

# Last-resort match for titles that survive prefix stripping but are plainly
# the same release. Deliberately high: merging two distinct releases would
# silence a real one, which is worse than reporting a near-duplicate once.
_LEDGER_MATCH_THRESHOLD = 0.90


def _artist_prefixes() -> list[str]:
    """Configured watch artists plus known aliases, longest first."""
    names = {*config.WATCH_ARTISTS, *_EXTRA_ARTIST_PREFIXES}
    normalized = {normalize_title(name) for name in names if name.strip()}
    return sorted((n for n in normalized if n), key=len, reverse=True)


def ledger_key(title: str) -> str:
    """Normalized key with any leading artist name removed."""
    key = normalize_title(title)
    if not key:
        return ""

    for prefix in _artist_prefixes():
        if key == prefix:
            # The title is JUST the artist name — nothing to strip.
            break
        if key.startswith(prefix + " "):
            stripped = key[len(prefix) + 1 :].strip()
            if stripped:  # never strip a title down to nothing
                return stripped
            break

    return key


def _find(releases: dict, title: str) -> tuple[str | None, dict | None]:
    """Locate an existing entry for `title`: exact key, then close match."""
    key = ledger_key(title)
    if not key:
        return None, None

    if key in releases:
        return key, releases[key]

    for existing_key, record in releases.items():
        if similarity(key, existing_key) >= _LEDGER_MATCH_THRESHOLD:
            return existing_key, record

    return None, None


class LedgerError(Exception):
    """Raised when the ledger cannot be read or written."""


def _today() -> str:
    return _dt.date.today().isoformat()


def load() -> dict:
    """Read the ledger. A missing file is an empty ledger, not an error."""
    if not LEDGER_PATH.is_file():
        return {"version": _SCHEMA_VERSION, "releases": {}}

    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(
            f"Could not read ledger at {LEDGER_PATH}: {exc}. "
            f"Move it aside to start fresh — do NOT let the watch run against "
            f"a ledger it cannot parse, or it will re-report everything."
        ) from exc

    if not isinstance(data, dict) or "releases" not in data:
        raise LedgerError(f"Ledger at {LEDGER_PATH} is not in the expected format.")

    return data


def save(data: dict) -> None:
    """Write the ledger atomically: temp file, then replace."""
    data["version"] = _SCHEMA_VERSION
    data["updated"] = _today()

    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=LEDGER_PATH.parent,
            prefix=".ledger-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            temp_name = handle.name
        os.replace(temp_name, LEDGER_PATH)
    except OSError as exc:
        raise LedgerError(f"Could not write ledger to {LEDGER_PATH}: {exc}") from exc


# Statuses meaning "not out yet". A date change only matters for these.
_FORTHCOMING = {"pre_order", "preorder", "announced", "upcoming", "pending"}


def _changed(prior: dict, verdict: str | None, release_date: str | None,
             status: str | None) -> str | None:
    """What materially changed since the last report, if anything.

    Release dates are deliberately NOT a trigger for already-released titles.
    They are extracted from web pages by a model, and one release legitimately
    carries several: announcement date, street date, streaming date. Treating
    every variation as a change re-reports the same record every run — which
    is what happened to the Warfield box, flipping between 2025-07-25 and
    2025-08-15 across two sweeps an hour apart.

    For something not yet released, a moved date IS news: a slipped pre-order
    is worth knowing about.
    """
    if verdict and prior.get("verdict") and verdict != prior["verdict"]:
        return f"verdict changed: {prior['verdict']} -> {verdict}"

    if status and prior.get("status") and status != prior["status"]:
        return f"status changed: {prior['status']} -> {status}"

    if release_date and prior.get("release_date") and release_date != prior["release_date"]:
        effective = (status or prior.get("status") or "").lower()
        if effective in _FORTHCOMING:
            return (
                f"release date moved: {prior['release_date']} -> {release_date} "
                f"(still {effective})"
            )
        # Already out — record the new date, but do not re-report over it.

    return None


def check(
    title: str,
    verdict: str | None = None,
    release_date: str | None = None,
    status: str | None = None,
) -> dict:
    """Should this release appear in the current report?

    With SUPPRESS_ONLY_HELD (default), a release is re-reported every sweep
    until its Plex verdict becomes 'held'. Once it is held, further reports
    are suppressed. This keeps Tim notified about genuine gaps until they are
    filled, rather than silencing them after one mention.
    """
    if not title or not title.strip():
        raise LedgerError("A title is required.")

    if not ledger_key(title):
        raise LedgerError(f"Title {title!r} normalizes to nothing.")

    releases = load()["releases"]
    _, prior = _find(releases, title)

    if prior is None:
        return {
            "title": title,
            "should_report": True,
            "reason": "Not previously reported — this is new.",
            "previously_reported": False,
        }

    if verdict == "held":
        return {
            "title": title,
            "should_report": False,
            "reason": "Held in the library; no further reminders needed.",
            "previously_reported": True,
            "first_reported": prior.get("first_reported"),
            "times_reported": prior.get("times_reported", 1),
        }

    if not SUPPRESS_ONLY_HELD:
        # Legacy behavior: only report when something materially changed.
        change = _changed(prior, verdict, release_date, status)
        if change:
            return {
                "title": title,
                "should_report": True,
                "reason": f"Previously reported, but {change}.",
                "previously_reported": True,
                "first_reported": prior.get("first_reported"),
                "prior": {
                    "verdict": prior.get("verdict"),
                    "release_date": prior.get("release_date"),
                    "status": prior.get("status"),
                },
            }

        return {
            "title": title,
            "should_report": False,
            "reason": (
                f"Already reported on {prior.get('last_reported', 'an earlier run')} "
                f"with nothing changed since. Leave it out of this report."
            ),
            "previously_reported": True,
            "first_reported": prior.get("first_reported"),
            "times_reported": prior.get("times_reported", 1),
        }

    # Default behavior: re-report not_held/uncertain until they become held.
    return {
        "title": title,
        "should_report": True,
        "reason": "Still not held in the library; re-reporting until acquired.",
        "previously_reported": True,
        "first_reported": prior.get("first_reported"),
        "times_reported": prior.get("times_reported", 1),
    }


def commit(entries: list[dict]) -> dict:
    """Record the releases that actually appeared in this report.

    Called once, after the report is written — never before. Recording a
    release the run then failed to report would silence it permanently.
    """
    if not isinstance(entries, list):
        raise LedgerError("'entries' must be a list of release objects.")

    data = load()
    releases = data["releases"]
    today = _today()
    recorded, skipped = [], []

    for entry in entries:
        if not isinstance(entry, dict):
            skipped.append({"entry": repr(entry)[:80], "why": "not an object"})
            continue

        title = (entry.get("title") or "").strip()
        if not ledger_key(title):
            skipped.append({"entry": repr(entry)[:80], "why": "no usable title"})
            continue

        # Reuse an existing entry's key when this is the same release under a
        # different phrasing, so commits merge rather than duplicate.
        existing_key, prior = _find(releases, title)
        key = existing_key or ledger_key(title)
        prior = prior or {}

        merged = {
            "title": title,
            "verdict": entry.get("verdict") or prior.get("verdict"),
            "release_date": entry.get("release_date") or prior.get("release_date"),
            "status": entry.get("status") or prior.get("status"),
            "url": entry.get("url") or prior.get("url"),
            "first_reported": prior.get("first_reported", today),
            "last_reported": today,
            "times_reported": prior.get("times_reported", 0) + 1,
        }
        releases[key] = merged
        recorded.append(merged)

    save(data)

    return {
        "recorded": recorded,
        "recorded_count": len(recorded),
        "skipped": skipped,
        "ledger_path": str(LEDGER_PATH),
        "total_tracked": len(releases),
    }


def entries() -> dict:
    """Everything the ledger knows, newest first."""
    releases = load()["releases"]
    rows = sorted(
        releases.values(),
        key=lambda row: row.get("last_reported", ""),
        reverse=True,
    )
    return {
        "ledger_path": str(LEDGER_PATH),
        "exists": LEDGER_PATH.is_file(),
        "total_tracked": len(rows),
        "releases": rows,
    }


def release_ledger(
    operation: str,
    title: str | None = None,
    verdict: str | None = None,
    release_date: str | None = None,
    status: str | None = None,
    entries_list: list | None = None,
) -> dict:
    """Entry point registered as the `release_ledger` tool."""
    if operation == "check":
        if not title:
            raise LedgerError("'title' is required for operation 'check'.")
        return check(title, verdict, release_date, status)
    if operation == "commit":
        return commit(entries_list or [])
    if operation == "list":
        return entries()

    raise LedgerError(
        f"Unknown operation '{operation}'. Valid: check, commit, list"
    )
