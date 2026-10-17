# Lemieux

An audio archivist and Plex librarian for a Grateful Dead collection, built on
an Ollama tool-calling loop. Named for David Lemieux, the Dead's vault
archivist, and written to work the way an archivist does: verify before
asserting, never guess at catalog data, and never touch the library without
being asked twice.

Two jobs:

1. **Library interrogation** — query Plex holdings, read FLAC tags, audit
   folder naming against collection standards.
2. **Release watch** — sweep for Grateful Dead, Jerry Garcia, Jerry Garcia
   Band and Dead & Company releases not already held, and keep reporting them
   until they are acquired.

**Still read-only.** No tool in this package can write, move, rename, or
   hard-delete anything in the media library. Write capability is a deliberate
   later addition, not a flag.

## Layout

```
Modelfile                 persona + policy (the system prompt lives here)
Modelfile.local           same prompt, local model base
requirements.txt
.env.example              copy to .env and fill in
release_ledger.json       per-install state: what the watch has reported
lemieux/
  config.py               paths, jail roots, collection conventions, .env loader
  safety.py               path jail, row caps, token scrubbing — the enforcement
  matching.py             title normalization and holdings comparison
  ledger.py               what the watch has already reported
  notify.py               email (full backlog) and SMS (current sweep only)
  prompt.py               pulls the SYSTEM block out of the Modelfile
  registry.py             tool schemas + dispatch
  agent.py                tool-calling loop and interactive CLI
  web.py                  FastAPI web chat + manual sweep trigger
  watch.py                release sweep, for cron
  tools/
    plex.py               plex_query  — sections, search, track_search,
                                        albums, tracks, artist_summary,
                                        inventory, have
    fs.py                 fs_inspect  — list, stat, tags, audit, find, read_text
    search.py             web_search  — release announcements (untrusted input)
    catalog.py            catalog_lookup  — MusicBrainz artist discographies
lemieux-web.service       systemd unit template for the web app
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in PLEX_TOKEN, OLLAMA_API_KEY, SMTP, WEB password
python -m lemieux.agent --check
```

`--check` prints effective config, warns about unmounted media roots, confirms
the system prompt loaded, and probes Plex. It fails loudly rather than letting
the model report against a library it cannot see.

No `ollama create` step is required. The persona is injected from the
Modelfile's `SYSTEM` block at runtime, so `LEMIEUX_MODEL` points straight at a
base model and prompt edits take effect on the next run. (Deriving a model
from a `:cloud` base does not reliably inherit its capabilities, which
surfaces as `does not support chat`.)

## Use

```bash
python -m lemieux.agent                          # interactive REPL
python -m lemieux.agent "list the library sections"

python -m lemieux.watch --artist "Jerry Garcia Band"
python -m lemieux.watch --notify                 # sweep all artists and notify
python -m lemieux.watch --ledger                 # what has been reported
python -m lemieux.watch --notify-check           # notification config

# Web chat (or use systemd)
python -m uvicorn lemieux.web:app --host 0.0.0.0 --port 8000
```

Tool calls and results are echoed as they happen, so you can see exactly what
the model touched and what it was told.

## Design

### Policy is not enforcement

The system prompt makes the model *want* to behave. `safety.py` makes
misbehavior impossible. These are separate layers on purpose, and the
distinction is worth keeping clear rather than pretending a prompt offers the
same guarantee as code.

| Control | Where | Effect |
|---|---|---|
| Path jail | `safety.resolve_in_jail` | Resolves **before** the containment check, so `..` and symlinks pointing out are both caught |
| No write verbs | absence from `registry.DISPATCH` | The model cannot call what does not exist |
| Row caps | `safety.cap_rows` | A "list everything" call can't blow out context; `truncated` is surfaced so partial lists are never reported as complete |
| Token scrubbing | `safety.scrub` | The Plex token is stripped from exceptions and URLs before reaching the model |
| Iteration ceiling | `config.MAX_TOOL_ITERATIONS` | A confused model can't loop on the filesystem; on exhaustion it is forced to write a final report from what it already gathered |
| Structured refusals | `registry.call_tool` | Refusals return JSON, not exceptions — the model explains rather than blindly retries |

The path jail genuinely cannot be talked around. A prompt rule can. Where a
property must hold, it lives in Python.

### The model never decides what you own

A false "you're missing this" on a box set already owned is the failure that
makes a watch worth ignoring. So holdings are decided by `matching.py`, not by
the model:

- **`held`** — owned. Not reported.
- **`not_held`** — a genuine gap.
- **`uncertain`** — reported *as* uncertain, with the nearest held title.
  Forcing a binary on a near-miss means either spamming known releases or
  silently dropping real ones.

Matching handles the variants these titles actually arrive in. Some rules
earned their place the hard way:

- Apostrophes are deleted, not spaced, so `Dave's` matches `Daves`.
- Disjoint numbers force the score down: `Volume 59` against `Volume 57`
  scores 0.72 on text alone, high enough to be called uncertain — which would
  suppress every new volume announcement.
- A held title that is the candidate **plus a date** is ambiguous, not a gap:
  `Electric on the Eel` may or may not be the August 10th 1991 edition.
- A held title that is the candidate **plus non-date detail** is the same
  release named properly: `GarciaLive Volume 22: September 25th, 1971` and
  `...Lion's Share`. This is asymmetric on purpose — the reverse direction
  (`Europe 72` vs `Europe 72: The Complete Recordings`) is two different
  products.
- Artist names are stripped from candidates, so `Live at The Warfield` and
  `Live at The Warfield (Jerry Garcia Band)` cannot produce different
  verdicts. A verdict is a fact about the library, not about phrasing.

### The watch keeps reminding you about gaps

The ledger records every reported release, but only **held** releases are
suppressed from future reports. A release that remains `not_held` is
re-reported every sweep and re-sent by email/SMS until it is acquired. The
ledger survives restarts and does not forget what was announced.

Dates are deliberately *not* a trigger for already-released titles. They are
extracted from web pages, and one release legitimately carries several
(announcement, street, streaming). Treating every variation as news would
re-report the same held record forever, so only a verdict change to `held`
silences a previously reported gap.

Commit happens once, last, after the report is written. Recording before
reporting would mean a run that died mid-way silences those releases
permanently. A corrupt ledger raises rather than degrading to empty, because
"empty" would silently re-report everything.

Set `LEMIEUX_LEDGER_SUPPRESS_ONLY_HELD=0` to restore the legacy behavior where
releases are silenced after one report unless verdict/status/date changes.

### Search results are untrusted

`web_search` wraps every snippet as `untrusted_content` with an explicit
notice. Pages about collectible box sets are exactly where text aimed at an
agent would be planted. Nothing in a search result can authorize an action.

The new `catalog_lookup` tool uses MusicBrainz as a catalog reference, but its
results are also treated as **untrusted evidence** about what exists — not as
proof that Tim lacks an album. Compilations, box sets, and variant titles mean
a "missing" album may already be in the library under a different name.

## Notifications

Only releases with a **definite `not_held` verdict** trigger an email or SMS.
`uncertain` releases are still committed to the ledger, so they are not lost,
but they never ping you — a human decision is required first.

**Email** carries a concise list of **every** `not_held` release currently in
the ledger: title, release date or pre-sale status, and source URL. The subject
line shows how many are new this sweep and how many are still open in total.

**SMS** carries a one-line alert about **only this sweep's** `not_held`
findings: the count, the first release title, and its source URL.

Both bodies are templatable through `.env` (see `LEMIEUX_EMAIL_TEMPLATE`,
`LEMIEUX_EMAIL_RELEASE_TEMPLATE`, `LEMIEUX_SMS_TEMPLATE`,
`LEMIEUX_SMS_MORE_TEMPLATE`, and `LEMIEUX_SMS_MAX_CHARS` in `.env.example`).

Whether to notify is read from the `release_ledger` commit **tool result**,
not from the report prose — so a phone buzzing is a fact about what was
recorded, not a function of how the model phrased its summary.

Nothing is sent unless the sweep found a `not_held` gap this run, unless
`LEMIEUX_NOTIFY_ON=always`. A quiet sweep is a successful sweep.

Gmail requires an App Password; plain-password SMTP is rejected.

Carrier email-to-SMS gateways are being retired by several carriers. If texts
stop arriving while email still works, the gateway is the first suspect. Raw
URLs are used for now; a short-link adapter can be added later without changing
the templates.

## Web chat

A FastAPI web app (`lemieux/web.py`) serves a mobile-first chat UI. It runs on
`hal9003` and is reachable over Tailscale or your LAN. From your phone you can
ask it anything about the library and trigger manual release sweeps.

```bash
python -m uvicorn lemieux.web:app --host 0.0.0.0 --port 8080
```

Open `http://hal9003:8080` in your phone browser (or the Tailscale address).
The UI authenticates with the password from `.env` via an HTTP-only cookie and
shows a **Log out** button; sessions last 30 days.

### Endpoints

- `GET /` — chat UI
- `POST /login` — authenticate and set the `lemieux_auth` cookie
- `POST /logout` — clear the auth cookie and session
- `GET /auth-check` — verify the current auth cookie
- `POST /chat` — send a message, get the assistant reply
- `POST /session` — clear the current conversation
- `POST /sweep` — run the release sweep manually (sends email/SMS if configured)
- `GET /sweep-stream` — Server-Sent Events stream of a sweep with live progress
- `GET /health` — liveness check

### systemd service

Copy `lemieux-web.service` to `/etc/systemd/system/lemieux-web.service` after
updating `WorkingDirectory` and the `User` to match your install. Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable lemieux-web.service
sudo systemctl start lemieux-web.service
sudo systemctl status lemieux-web.service
```

### Release watch cron

Run the release watch every Monday at 6 PM EST (22:00 UTC):

```bash
0 22 * * 1 /opt/lemieux/.venv/bin/python -m lemieux.watch --notify
```

The web chat's **Run sweep** button connects to `/sweep-stream`, so progress
messages like “Building Plex inventory...” appear in the chat as the sweep runs.
It still performs the same email/SMS notification step at the end.

## Not yet built

- `fs_mutate` — retag/rename/move, `dry_run=True` default, manifest required
  above 10 files, no delete verb at any setting
- `audit` reworked to report observed structure per tree rather than judging
  against one assumed convention (official box sets and downloaded shows use
  different folder forms)
- Voice input in the chat UI
- Live progress for regular `/chat` tool calls (only the sweep streams today)

## Notes

`fs_inspect(operation="tags")` needs `mutagen` and real FLAC files; it has not
been exercised against a real library. Run it against one known-good show
folder before trusting its output.

The ledger (`release_ledger.json`), `.env`, and any session state are per-install
state and are gitignored.
