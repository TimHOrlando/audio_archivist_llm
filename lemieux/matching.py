"""
Title normalization and matching.

The release watch lives or dies on one question: does Tim already own this?
A false positive ("you're missing Dave's Picks 42") on something he owns
destroys trust in every future report, so this is deterministic Python, never
a model judgement.

Release titles vary wildly across sources:
    "Dave's Picks Volume 58"
    "Dave's Picks, Vol. 58"
    "Daves Picks 58"
    "DAVE'S PICKS VOL 58 - Boston Garden"
All four are the same release.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# Words that carry no identifying information and only add noise to matching.
# NOTE: "live" is deliberately NOT a stopword. In this catalog it carries
# identity — GarciaLive, Live/Dead, Live At The Fillmore — and dropping it
# makes "GarciaLive Volume 20" unmatchable against "Garcia Live Volume 20".
_STOPWORDS = {
    "the", "a", "an", "and", "of", "at", "in", "on",
    "recording", "recordings",
    "cd", "cds", "disc", "discs", "box", "set", "boxset",
    "edition", "remaster", "remastered", "deluxe",
}

# Volume markers that should all collapse to the same token.
_VOLUME = re.compile(r"\b(?:vol(?:ume)?\.?|v\.?)\s*(\d+)\b", re.IGNORECASE)

_ROMAN = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
    "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12,
}


def normalize_title(title: str) -> str:
    """Reduce a release title to a comparable canonical form.

    Smart quotes, case, punctuation, volume spellings, and noise words are all
    flattened. "Dave's Picks, Vol. 58" and "DAVES PICKS VOLUME 58" both become
    "daves picks vol58".
    """
    if not title:
        return ""

    # Normalize unicode: curly quotes, accents, and friends.
    text = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = text.lower()

    # Collapse volume markers to a single token BEFORE stripping punctuation,
    # so "vol. 58" and "volume 58" both become "vol58".
    text = _VOLUME.sub(lambda m: f"vol{int(m.group(1))}", text)

    # Apostrophes are DELETED, not spaced: "Dave's" must collapse to "daves"
    # to match the apostrophe-less spelling. Replacing with a space yields
    # "dave s", which is two tokens and will not match "daves".
    text = text.replace("'", "")

    # Every other non-alphanumeric becomes a separator.
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    # Ordinals lose their suffix: "August 10th" and "August 10" are the same
    # date, and "10th" would otherwise read as a word rather than a number.
    text = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", text)

    tokens = []
    for token in text.split():
        if token in _STOPWORDS:
            continue
        if token in _ROMAN:
            token = str(_ROMAN[token])
        tokens.append(token)

    return " ".join(tokens)


def _token_set(title: str) -> set[str]:
    return set(normalize_title(title).split())


# Artist names that appear decoratively around a release title. A source — or
# the model writing a query — may cite the same release as:
#   "Live At The Warfield"
#   "Live at The Warfield (Jerry Garcia Band)"
#   "Jerry Garcia Band - Live at The Warfield"
# Left alone, the artist tokens drag the title toward any held album actually
# NAMED for the artist, and the holdings verdict changes with phrasing. A
# verdict must be a fact about the library, not about wording.
ARTIST_NAMES = (
    "Jerry Garcia Band",
    "Jerry Garcia",
    "Grateful Dead",
    "Dead & Company",
    "Dead and Company",
    "Phil Lesh and Friends",
    "Phil Lesh",
    "Bob Weir",
    "JGB",
)


def strip_artist(title: str) -> str:
    """Remove a leading or trailing artist name from a release title.

    Returns the title unchanged when stripping would leave nothing — a title
    that IS the artist name ("Jerry Garcia Band", the 1978 album) must survive
    intact, or it could never be matched at all.
    """
    if not title:
        return title

    text = title.strip()

    # A title that IS an artist name is a real release — the 1978 album
    # "Jerry Garcia Band", for one. Never strip it: a shorter artist name
    # would otherwise match as a prefix and reduce it to "Band".
    normalized = normalize_title(text)
    if any(normalized == normalize_title(name) for name in ARTIST_NAMES):
        return text

    for name in sorted(ARTIST_NAMES, key=len, reverse=True):
        lowered, needle = text.lower(), name.lower()

        # Trailing "(Artist)" or " - Artist" / ": Artist"
        for opener, closer in (("(", ")"), ("[", "]")):
            suffix = f"{opener}{needle}{closer}"
            if lowered.endswith(suffix):
                candidate = text[: -len(suffix)].strip(" -:—–")
                if candidate:
                    return candidate

        for separator in (" - ", " – ", " — ", ": "):
            tail = f"{separator}{needle}"
            if lowered.endswith(tail):
                candidate = text[: -len(tail)].strip()
                if candidate:
                    return candidate

            head = f"{needle}{separator}"
            if lowered.startswith(head):
                candidate = text[len(head) :].strip()
                if candidate:
                    return candidate

        if lowered.startswith(needle + " "):
            candidate = text[len(name) :].strip(" -:—–")
            if candidate:
                return candidate

    return text


def _numbers(title: str) -> set[str]:
    """Every integer in a normalized title: volumes, years, dates."""
    return set(re.findall(r"\d+", normalize_title(title)))


# A numbered release whose numbers are entirely different is a DIFFERENT
# release, however similar the words. "Dave's Picks Volume 59" against
# "Dave's Picks Volume 57" is 0.72 on text alone — high enough to be called
# uncertain, which would suppress every new volume announcement. Since a new
# volume in an owned series is the single most important thing this watch
# exists to catch, disjoint numbers force the score down.
_DISJOINT_NUMBERS_CEILING = 0.35

_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
    "st", "nd", "rd", "th",  # ordinal suffixes left by "10th" -> "10 th"
}

# A held title that is wholly contained in a candidate, where the ONLY extra
# words are a date, is ambiguous — not a confident gap.
#
#   held:      "Electric on the Eel"
#   candidate: "Electric on the Eel: August 10th, 1991"
#
# Tim may own exactly that edition under a shorter title, or may own a
# different night. Scoring this 'not_held' reports a gap he may already have;
# scoring it 'held' hides a real one. It belongs in the uncertain band, where
# a human decides.
_DATE_QUALIFIED_SCORE = 0.70


def _is_date_qualified_superset(smaller: set[str], larger: set[str]) -> bool:
    """True when `larger` is `smaller` plus only date-ish tokens."""
    if len(smaller) < 2 or not smaller < larger:
        return False
    extras = larger - smaller
    return bool(extras) and all(
        token.isdigit() or token in _MONTHS for token in extras
    )


def similarity(a: str, b: str) -> float:
    """Similarity of two titles, 0.0 to 1.0.

    Blends sequence similarity with token overlap: sequence ratio alone is
    fooled by reordering, token overlap alone by subsets.
    """
    norm_a, norm_b = normalize_title(a), normalize_title(b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0

    # Word spacing is not reliable across sources: "GarciaLive" and
    # "Garcia Live" are the same series. Compare space-stripped forms too.
    compact_a, compact_b = norm_a.replace(" ", ""), norm_b.replace(" ", "")
    if compact_a == compact_b:
        return 1.0

    sequence = max(
        difflib.SequenceMatcher(None, norm_a, norm_b).ratio(),
        difflib.SequenceMatcher(None, compact_a, compact_b).ratio(),
    )

    tokens_a, tokens_b = _token_set(a), _token_set(b)
    if tokens_a and tokens_b:
        overlap = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    else:
        overlap = 0.0

    score = (sequence * 0.5) + (overlap * 0.5)

    # Both titles are numbered and share no number at all -> different release.
    numbers_a, numbers_b = _numbers(a), _numbers(b)
    if numbers_a and numbers_b and not (numbers_a & numbers_b):
        return min(score, _DISJOINT_NUMBERS_CEILING)

    # One title is the other plus a date -> ambiguous, force into 'uncertain'.
    if _is_date_qualified_superset(tokens_a, tokens_b) or (
        _is_date_qualified_superset(tokens_b, tokens_a)
    ):
        score = max(score, _DATE_QUALIFIED_SCORE)

    return score


# Above this, treat as the same release.
HELD_THRESHOLD = 0.85
# Between this and HELD_THRESHOLD, a human decides.
UNCERTAIN_THRESHOLD = 0.62


_HELD_SUPERSET_SCORE = 0.90


def _held_is_fuller_title(candidate: str, held: str) -> bool:
    """True when the HELD title is the candidate plus non-date detail.

        candidate: "GarciaLive Volume 22: September 25th, 1971"
        held:      "GarciaLive Volume 22: September 25th, 1971 Lion's Share"

    Same release; the library copy simply carries the fuller official title,
    venue and all. Without this it scores 0.79 and asks Tim about a record he
    owns.

    This is deliberately ASYMMETRIC — it fires only when HELD is the fuller
    title, never the reverse. The reverse direction is a different situation
    entirely:

        held:      "Europe 72"
        candidate: "Europe 72: The Complete Recordings"

    Those are different products (a 1972 album and a 73-CD box), and calling
    that 'held' would hide a major release. Extra detail on a candidate means
    "possibly something more"; extra detail on a held title means "the same
    thing, named properly".

    Numbers must match exactly, so a new volume or a different night never
    qualifies, and date-only extras are excluded — those stay ambiguous via
    _is_date_qualified_superset.
    """
    cand_tokens, held_tokens = _token_set(candidate), _token_set(held)

    # Conservative floor: short titles carry too little signal.
    if len(cand_tokens) < 3 or not cand_tokens < held_tokens:
        return False

    # A different volume or night must never collapse into a held release.
    if _numbers(candidate) != _numbers(held):
        return False

    extras = held_tokens - cand_tokens
    # Date-only extras are the ambiguous "which night?" case, not this one.
    return bool(extras) and not all(
        token.isdigit() or token in _MONTHS for token in extras
    )


def find_matches(
    candidate: str, inventory: list[str], limit: int = 5
) -> list[dict]:
    """Score `candidate` against every held title, best first."""
    scored = []
    for held in inventory:
        score = similarity(candidate, held)
        if _held_is_fuller_title(candidate, held):
            score = max(score, _HELD_SUPERSET_SCORE)
        scored.append({"held_title": held, "score": round(score, 4)})

    scored.sort(key=lambda row: row["score"], reverse=True)
    return scored[:limit]


def verdict(candidate: str, inventory: list[str]) -> dict:
    """Decide whether `candidate` is already held.

    Returns one of three verdicts. 'uncertain' exists on purpose: forcing a
    binary answer on a near-miss is how a watch either spams known releases or
    silently drops real gaps. An uncertain result is reported to Tim for a
    human call, never resolved by guessing.
    """
    if not inventory:
        return {
            "candidate": candidate,
            "verdict": "unknown",
            "reason": "Inventory is empty — cannot determine holdings.",
            "matches": [],
        }

    # Decide on the title with any decorative artist name removed, so the
    # verdict does not depend on how the candidate happened to be phrased.
    bare = strip_artist(candidate)
    matches = find_matches(bare, inventory)
    best = matches[0]["score"] if matches else 0.0

    if best >= HELD_THRESHOLD:
        result = "held"
    elif best >= UNCERTAIN_THRESHOLD:
        result = "uncertain"
    else:
        result = "not_held"

    return {
        "candidate": candidate,
        "matched_as": bare if bare != candidate else None,
        "verdict": result,
        "best_score": best,
        "thresholds": {
            "held": HELD_THRESHOLD,
            "uncertain": UNCERTAIN_THRESHOLD,
        },
        "matches": matches,
    }
