"""
catalog_lookup — fetch a canonical discography for any artist from MusicBrainz.

This tool gives the model a reference list of official releases so it can
cross-check against Plex holdings for questions like "How many Jimi Hendrix
albums am I missing?". Results are treated as a *catalog reference*, not as
proof Tim lacks an album — he may own it under a different title, in a comp,
or in a box set.

MusicBrainz is free and rate-limited. We sleep between requests and cache
results per process to avoid hammering their servers.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Any

from ..safety import SafetyError, cap_rows

_USER_AGENT = (
    "LemieuxPlexArchivist/0.2 "
    "(tim.h.orlando@gmail.com; personal media library assistant)"
)

# Optional app name/contact for MusicBrainz rate-limit etiquette.
_MUSICBRAINZ_CONTACT = os.getenv("LEMIEUX_MUSICBRAINZ_CONTACT", "tim.h.orlando@gmail.com")

# Number of release groups to fetch. MusicBrainz paginates.
_MAX_RELEASE_GROUPS = 100


def _request_json(url: str) -> dict:
    """Make a GET request to MusicBrainz with a proper user agent.

    Raises SafetyError on network or HTTP problems so the model can explain
    the failure rather than crashing the loop.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            import json
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SafetyError(
            f"MusicBrainz request failed: HTTP {exc.code} for {url}"
        ) from exc
    except Exception as exc:
        raise SafetyError(f"MusicBrainz request failed: {exc}") from exc


@lru_cache(maxsize=128)
def _search_artist(name: str) -> dict | None:
    """Find the best-matching artist for a name."""
    query = urllib.parse.quote(name)
    url = f"https://musicbrainz.org/ws/2/artist/?query=artist:{query}&fmt=json&limit=1"
    data = _request_json(url)
    artists = data.get("artists", [])
    return artists[0] if artists else None


@lru_cache(maxsize=128)
def _release_groups(artist_mbid: str) -> list[dict[str, Any]]:
    """All release groups for an artist.

    We do NOT server-filter by primary type because MusicBrainz's tagging is
    inconsistent: many anthology, live, and reissue releases that a collector
    would care about end up marked Compilation or with no primary type at all.
    Instead we fetch up to the limit and filter client-side, keeping the
    primary and secondary types visible so the model can reason about them.
    """
    url = (
        "https://musicbrainz.org/ws/2/release-group"
        f"?artist={artist_mbid}"
        "&fmt=json"
        f"&limit={_MAX_RELEASE_GROUPS}"
    )
    data = _request_json(url)
    return data.get("release-groups", [])


def _primary_type(release_group: dict) -> str:
    return (release_group.get("primary-type") or "").lower()


def _secondary_types(release_group: dict) -> set[str]:
    return {t.lower() for t in (release_group.get("secondary-types") or [])}


def _title(release_group: dict) -> str:
    return release_group.get("title") or "Unknown release"


def _first_date(release_group: dict) -> str:
    """Best available date: first-release-date, then earliest known release."""
    date = release_group.get("first-release-date", "")
    if not date:
        releases = (release_group.get("releases") or [])[:1]
        if releases:
            date = releases[0].get("date", "")
    return date


def _is_interesting_album(group: dict) -> bool:
    """True if this release group represents a canonical studio/live album.

    Filters out singles, EPs, broadcasts, DJ mixes, and compilations.
    """
    ptype = _primary_type(group)
    stypes = _secondary_types(group)

    # Explicitly drop these categories.
    if ptype in {"single", "ep", "broadcast", "dj mix"}:
        return False
    if "compilation" in stypes or "dj-mix" in stypes or "mixtape" in stypes:
        return False

    return True


def catalog_lookup(artist: str, limit: int | None = None) -> dict:
    """Return a canonical album list for an artist from MusicBrainz.

    Use this to compare against Plex holdings. You must still verify that an
    album is actually missing; titles vary, compilations hide albums, and Tim
    may own it under a box set.
    """
    if not artist or not artist.strip():
        raise SafetyError("The 'artist' argument is required.")

    artist_data = _search_artist(artist.strip())
    if artist_data is None:
        return {
            "artist": artist,
            "disambiguation": None,
            "album_count": 0,
            "albums": [],
            "note": f"No MusicBrainz artist match for '{artist}'. The catalog cannot be determined.",
        }

    artist_name = artist_data.get("name", artist)
    mbid = artist_data.get("id")
    disambiguation = artist_data.get("disambiguation")

    groups = _release_groups(mbid) if mbid else []

    albums = []
    seen: set[str] = set()
    for group in groups:
        if not _is_interesting_album(group):
            continue

        title = _title(group)
        # Dedupe by title; MusicBrainz sometimes lists the same album twice
        # under different release group IDs.
        if title.lower() in seen:
            continue
        seen.add(title.lower())

        albums.append({
            "title": title,
            "type": _primary_type(group) or "album",
            "date": _first_date(group),
            "musicbrainz_id": group.get("id"),
        })

    albums.sort(key=lambda row: (row.get("date") or "9999", row["title"]))
    albums, truncated = cap_rows(albums, limit)

    return {
        "artist": artist_name,
        "disambiguation": disambiguation,
        "query": artist,
        "album_count": len(albums),
        "truncated": truncated,
        "albums": albums,
        "note": (
            "This is a CATALOG REFERENCE from MusicBrainz, not a list of "
            "confirmed gaps. Cross-check each album with Plex before saying "
            "Tim is missing it. Compilations, box sets, and alternate titles "
            "can make an 'album' already present under a different name."
        ),
        "source_url": f"https://musicbrainz.org/artist/{mbid}",
    }
