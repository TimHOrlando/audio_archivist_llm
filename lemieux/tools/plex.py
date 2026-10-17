"""
plex_query — read-only Plex library interrogation via plexapi.

Every call in this module is a GET. Nothing here edits metadata, triggers
scans, or deletes. The PlexServer connection is created lazily and cached so a
multi-step conversation doesn't re-handshake on every tool call.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse

from .. import config
from ..safety import SafetyError, cap_rows, scrub

_server = None  # module-level connection cache

# Inventory cache. Building it walks every album in every music section — on
# an 819-album library that is slow, and a sweep calls 'have' once per
# candidate. Without this, a five-candidate sweep enumerated the whole library
# five times. Cached for the process, with a TTL so a long-lived session still
# picks up newly added releases.
_inventory_cache: tuple[float, list[str]] | None = None
INVENTORY_TTL_SECONDS = int(os.getenv("LEMIEUX_INVENTORY_TTL", "600"))

# A lowercase letter butted against an uppercase one inside an otherwise
# space-separated name: "In And Out OfThe Garden" -> a dropped space.
# Deliberately narrow. Filesystem conventions like "daves_picks" do not match,
# so this flags real typos rather than every stylistic difference.
_MISSING_SPACE = re.compile(r"[a-z][A-Z]")


def _name_anomalies(name: str) -> list[str]:
    """Structural defects in a folder or section name.

    Returned as DATA so a finding does not depend on the model choosing to
    mention it. A model can decline to comment on a path; it cannot make a
    populated 'anomalies' field disappear without actively suppressing it.
    """
    findings = []

    if " " in name:  # only title-style names, not snake_case conventions
        for match in _MISSING_SPACE.finditer(name):
            findings.append(
                f"possible missing space at '{match.group(0)}' "
                f"(position {match.start()})"
            )

    if "  " in name:
        findings.append("contains a double space")
    if name != name.strip():
        findings.append("has leading or trailing whitespace")

    return findings


def _connect():
    """Return a cached PlexServer, connecting on first use."""
    global _server
    if _server is not None:
        return _server

    if not config.PLEX_TOKEN:
        raise SafetyError(
            "PLEX_TOKEN is not set. Export it in the environment "
            "(see .env.example); it is never read from a file in the library."
        )

    try:
        from plexapi.server import PlexServer
    except ImportError as exc:
        raise SafetyError("plexapi is not installed. Run: pip install plexapi") from exc

    try:
        _server = PlexServer(
            config.PLEX_BASEURL, config.PLEX_TOKEN, timeout=config.PLEX_TIMEOUT
        )
    except Exception as exc:
        raise SafetyError(
            f"Could not reach Plex at {config.PLEX_BASEURL}: {scrub(str(exc))}"
        ) from exc

    return _server


def _deep_link(rating_key) -> str:
    """Build an app.plex.tv deep link for a rating key."""
    key = urllib.parse.quote(f"/library/metadata/{rating_key}", safe="")
    return (
        f"https://app.plex.tv/desktop/#!/server/"
        f"{config.PLEX_MACHINE_ID}/details?key={key}"
    )


def _album_row(album) -> dict:
    return {
        "rating_key": album.ratingKey,
        "title": album.title,
        "artist": getattr(album, "parentTitle", None),
        "year": getattr(album, "year", None),
        "track_count": len(album.tracks()) if hasattr(album, "tracks") else None,
        "added_at": str(getattr(album, "addedAt", "")) or None,
        "deep_link": _deep_link(album.ratingKey),
    }


# ------------------------------------------------------------------------------
# Operations
# ------------------------------------------------------------------------------

def _op_sections() -> dict:
    """List library sections. Use this to orient first.

    For music ('artist') sections totalSize counts ARTISTS, not releases — a
    Grateful Dead section of 58 volumes reports 1. Reporting that as an item
    count invites the model to treat it as holdings, so album counts are
    fetched separately and the artist count is labeled for what it is.
    """
    plex = _connect()
    rows = []
    defect_count = 0

    for section in plex.library.sections():
        locations = list(getattr(section, "locations", []))
        row = {
            "title": section.title,
            "type": section.type,
            "key": section.key,
            "locations": locations,
        }

        anomalies = {}
        title_findings = _name_anomalies(section.title)
        if title_findings:
            anomalies["title"] = {
                "value": section.title,
                "findings": title_findings,
            }
        for location in locations:
            basename = location.rstrip("/").rsplit("/", 1)[-1]
            location_findings = _name_anomalies(basename)
            if location_findings:
                anomalies.setdefault("folders", []).append(
                    {"value": basename, "path": location, "findings": location_findings}
                )

        if anomalies:
            defect_count += 1
            row["anomalies"] = anomalies
            row["REPORT_VERBATIM"] = (
                "This section has a malformed name. Quote the affected value "
                "EXACTLY as given above, including the defect. Do not correct "
                "it, and do not omit it from your report."
            )

        try:
            total = section.totalSize
        except Exception:
            total = None

        if section.type == "artist":
            row["artist_count"] = total
            try:
                row["album_count"] = len(section.albums())
            except Exception as exc:
                row["album_count"] = None
                row["album_count_error"] = scrub(str(exc))
            row["note"] = (
                "album_count is the holdings figure; artist_count is not."
            )
        else:
            row["item_count"] = total

        rows.append(row)

    result = {
        "server": plex.friendlyName,
        "section_count": len(rows),
        "sections_with_name_defects": defect_count,
        "sections": rows,
        "warning": (
            "Section locations may nest: a parent section and a box-set "
            "section can both index the same files. Do not sum album counts "
            "across sections without checking locations for overlap."
        ),
    }

    if defect_count:
        result["ACTION_REQUIRED"] = (
            f"{defect_count} section(s) have malformed names (see 'anomalies'). "
            "Your report MUST name each affected value verbatim, defect "
            "included. Omitting them, or silently correcting them, reports a "
            "clean library that is not clean."
        )

    return result


def _search_results(targets, query: str, libtype: str) -> list[dict]:
    """Run a Plex search across a list of sections."""
    results = []
    for target in targets:
        try:
            found = target.search(title=query, libtype=libtype)
        except Exception as exc:
            raise SafetyError(f"Search failed: {scrub(str(exc))}") from exc
        for item in found:
            row: dict = {"title": item.title, "section": target.title}
            if libtype == "album":
                row.update(_album_row(item))
            elif libtype == "track":
                row.update({
                    "artist": getattr(item, "grandparentTitle", None),
                    "album_artist": getattr(item, "originalTitle", None) or getattr(item, "parentTitle", None),
                    "album": getattr(item, "parentTitle", None),
                    "index": getattr(item, "index", None),
                    "duration_sec": round((item.duration or 0) / 1000, 1) if hasattr(item, "duration") else None,
                    "deep_link": _deep_link(item.ratingKey),
                })
            results.append(row)
    return results


def _op_search(section: str | None, query: str | None, limit: int | None,
               libtype: str = "album") -> dict:
    """Search albums or tracks within a section (or across all music sections)."""
    plex = _connect()
    if not query:
        raise SafetyError("The 'query' argument is required for operation 'search'.")

    if libtype not in {"album", "track"}:
        raise SafetyError(f"Unsupported libtype '{libtype}' for search.")

    if section:
        try:
            targets = [plex.library.section(section)]
        except Exception as exc:
            raise SafetyError(
                f"No library section named '{section}': {scrub(str(exc))}"
            ) from exc
    else:
        targets = [s for s in plex.library.sections() if s.type == "artist"]

    results = _search_results(targets, query, libtype)
    results, truncated = cap_rows(results, limit)
    return {
        "query": query,
        "libtype": libtype,
        "sections_searched": [t.title for t in targets],
        "match_count": len(results),
        "truncated": truncated,
        "results": results,
        "note": "Result order is not meaningful; use rating_key to request track listings.",
    }


def _op_track_search(section: str | None, query: str | None, limit: int | None) -> dict:
    """Search tracks by title."""
    return _op_search(section, query, limit, libtype="track")


def _op_albums(section: str | None, limit: int | None) -> dict:
    """Every album in a section. The canonical 'what do I already own' call."""
    plex = _connect()
    if not section:
        raise SafetyError("The 'section' argument is required for operation 'albums'.")

    try:
        target = plex.library.section(section)
    except Exception as exc:
        raise SafetyError(
            f"No library section named '{section}': {scrub(str(exc))}"
        ) from exc

    albums = [_album_row(a) for a in target.albums()]
    albums.sort(key=lambda row: (row["title"] or "").lower())
    albums, truncated = cap_rows(albums, limit)

    return {
        "section": target.title,
        "album_count": len(albums),
        "truncated": truncated,
        "albums": albums,
    }


def _op_tracks(rating_key: str | int | None) -> dict:
    """Track listing for one album — the setlist as Plex has it."""
    plex = _connect()
    if rating_key in (None, ""):
        raise SafetyError(
            "The 'rating_key' argument is required for operation 'tracks'."
        )

    try:
        album = plex.fetchItem(int(rating_key))
    except Exception as exc:
        raise SafetyError(
            f"Could not fetch item {rating_key}: {scrub(str(exc))}"
        ) from exc

    rows = []
    for track in album.tracks():
        media_part = None
        try:
            media_part = track.media[0].parts[0].file
        except (IndexError, AttributeError):
            pass
        rows.append(
            {
                "index": track.index,
                "title": track.title,
                "duration_sec": round((track.duration or 0) / 1000, 1),
                "codec": track.media[0].audioCodec if track.media else None,
                "bitrate": track.media[0].bitrate if track.media else None,
                "file": media_part,
            }
        )

    rows, truncated = cap_rows(rows)
    return {
        "album": album.title,
        "artist": getattr(album, "parentTitle", None),
        "track_count": len(rows),
        "truncated": truncated,
        "deep_link": _deep_link(album.ratingKey),
        "tracks": rows,
    }


def held_titles(force: bool = False) -> list[str]:
    """Every album title across the configured inventory sections.

    Deduplicated, because nested sections index the same releases twice.
    Cached for INVENTORY_TTL_SECONDS — see _inventory_cache.
    """
    global _inventory_cache

    if not force and _inventory_cache is not None:
        age = time.monotonic() - _inventory_cache[0]
        if age < INVENTORY_TTL_SECONDS:
            return _inventory_cache[1]

    plex = _connect()

    if config.INVENTORY_SECTIONS:
        sections = []
        for name in config.INVENTORY_SECTIONS:
            try:
                sections.append(plex.library.section(name))
            except Exception as exc:
                raise SafetyError(
                    f"Configured inventory section '{name}' not found: "
                    f"{scrub(str(exc))}"
                ) from exc
    else:
        sections = [s for s in plex.library.sections() if s.type == "artist"]

    titles: set[str] = set()
    for section in sections:
        try:
            for album in section.albums():
                if album.title:
                    titles.add(album.title)
        except Exception as exc:
            raise SafetyError(
                f"Could not read albums from '{section.title}': {scrub(str(exc))}"
            ) from exc

    result = sorted(titles)
    _inventory_cache = (time.monotonic(), result)
    return result


def _op_inventory(limit: int | None) -> dict:
    """Holdings summary for the release watch.

    Returns a SAMPLE, not the full list. Holdings questions are answered by
    operation 'have', which compares against all titles server-side — dumping
    hundreds of titles into the conversation wastes context and invites the
    model to eyeball the list instead of calling 'have'.
    """
    titles = held_titles()
    sample_size = max(1, min(int(limit or 40), config.MAX_RESULT_ROWS))
    step = max(1, len(titles) // sample_size)
    sample = titles[::step][:sample_size]

    return {
        "sections": config.INVENTORY_SECTIONS or "all music sections",
        "total_titles": len(titles),
        "sample_size": len(sample),
        "sample_titles": sample,
        "IMPORTANT": (
            f"'sample_titles' is {len(sample)} titles spread across {len(titles)} "
            "held releases — it is NOT the full inventory and a title's absence "
            "from it means NOTHING. Never conclude a release is missing because "
            "it is not in this sample. Use operation 'have', which compares "
            "against all held titles."
        ),
    }


def _op_have(query: str | None) -> dict:
    """Decide whether a candidate release is already held.

    This is the anti-false-alarm gate. Holdings are decided by deterministic
    comparison against live inventory, never by the model's recollection.
    A near-miss returns 'uncertain' rather than being forced to a guess.
    """
    if not query:
        raise SafetyError("The 'query' argument is required for operation 'have'.")

    from ..matching import verdict

    result = verdict(query, held_titles())
    result["instruction"] = (
        "'held' means Tim owns this — do NOT report it as a gap. "
        "'not_held' is a genuine gap. 'uncertain' must be shown to Tim as "
        "uncertain, with the nearest held title, never resolved by guessing."
    )
    return result


def _op_artist_summary(section: str | None, query: str | None) -> dict:
    """Summary of an artist's holdings in one or all music sections.

    Returns album count, track count, and a sample of titles. Useful when a
    user asks 'how many X songs/albums do I have?' and the intent is a count
    rather than a list.
    """
    plex = _connect()
    if not query or not query.strip():
        raise SafetyError("The 'query' argument is required for operation 'artist_summary'.")

    if section:
        try:
            targets = [plex.library.section(section)]
        except Exception as exc:
            raise SafetyError(
                f"No library section named '{section}': {scrub(str(exc))}"
            ) from exc
    else:
        targets = [s for s in plex.library.sections() if s.type == "artist"]

    artist_name = query.strip()
    total_albums = 0
    total_tracks = 0
    album_titles: set[str] = set()
    sample_tracks: list[dict] = []

    for target in targets:
        try:
            artists = target.search(title=artist_name, libtype="artist")
        except Exception as exc:
            raise SafetyError(f"Artist search failed: {scrub(str(exc))}") from exc
        for artist in artists:
            for album in artist.albums():
                total_albums += 1
                album_titles.add(album.title)
                tracks = album.tracks() if hasattr(album, "tracks") else []
                total_tracks += len(tracks)
                for track in tracks:
                    if len(sample_tracks) < 5:
                        sample_tracks.append({
                            "title": track.title,
                            "album": album.title,
                            "index": track.index,
                        })

    return {
        "artist": artist_name,
        "sections_searched": [t.title for t in targets],
        "album_count": total_albums,
        "track_count": total_tracks,
        "album_titles": sorted(album_titles),
        "sample_tracks": sample_tracks,
        "note": (
            "Counts are unique to this Plex library; the same artist may "
            "appear under variants (e.g. 'Grateful Dead' vs 'The Grateful Dead')."
        ),
    }


def plex_query(
    operation: str,
    section: str | None = None,
    query: str | None = None,
    rating_key: str | int | None = None,
    limit: int | None = None,
) -> dict:
    """Entry point registered as the `plex_query` tool."""
    if operation == "sections":
        return _op_sections()
    if operation == "search":
        return _op_search(section, query, limit)
    if operation == "track_search":
        return _op_track_search(section, query, limit)
    if operation == "albums":
        return _op_albums(section, limit)
    if operation == "tracks":
        return _op_tracks(rating_key)
    if operation == "artist_summary":
        return _op_artist_summary(section, query)
    if operation == "inventory":
        return _op_inventory(limit)
    if operation == "have":
        return _op_have(query)

    raise SafetyError(
        f"Unknown operation '{operation}'. "
        "Valid: sections, search, track_search, albums, tracks, artist_summary, "
        "inventory, have"
    )
