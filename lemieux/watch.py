"""
Release watch — the scheduled sweep.

    python -m lemieux.watch              # sweep every configured artist
    python -m lemieux.watch --artist "Jerry Garcia Band"
    python -m lemieux.watch --report sweep.md

Designed to run unattended from cron on the Plex host. It drives the same
agent loop as the interactive CLI, so every holdings claim passes through
plex_query 'have' rather than the model's judgement.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

from . import config
from .agent import _get_client, _seed_messages, assistant_text, run_turn

SWEEP_PROMPT = """\
Perform a release sweep for: {artists}.

Today is {today}.

Procedure — follow it exactly:

1. Call plex_query with operation 'inventory' to establish current holdings.

2. For EACH artist, run AT MOST 3 web_search calls. Do not combine artists
   into one query. Focus on releases announced or shipping in the last 12
   months and anything on pre-order. Three good searches beat ten
   near-duplicates, and you have a limited tool budget.

3. Then check candidates. For EVERY candidate release, call plex_query with
   operation 'have' and the release title. You do not decide what Tim owns —
   that tool does.

   SEARCH, THEN CHECK. Do not run every search you can think of before
   checking anything: if you exhaust the budget on searches, the run ends
   with findings you never verified. Check as you go.

4. For every candidate that came back 'not_held' or 'uncertain', call
   release_ledger with operation 'check' before putting it in the report.
   Pass the verdict, release date, and status you found.

   - should_report true  -> include it.
   - should_report false -> LEAVE IT OUT. Tim has already been told, and
     nothing has changed. Repeating it is how this watch becomes noise he
     stops reading.

5. Report ONLY releases that passed BOTH the holdings check and the ledger
   check.

6. LAST STEP, after the report is written: call release_ledger with operation
   'commit' once, passing entries_list — every release you actually reported,
   as {{title, verdict, release_date, status, url}}. Do not commit anything
   you did not report, and do not commit before writing the report.

Format each finding as:

  **<Title>** — <series/volume if any>
  - Show date(s) and venue, if the release names them
  - Release date / pre-order status
  - Format and pressing (CD, vinyl, digital; limited run?)
  - Source URL
  - Confidence: verified / probable / rumored
  - Holdings verdict: not_held, or uncertain (naming the nearest held title)

Mark pre-orders, limited pressings, and subscription-only bonus discs clearly —
those have deadlines.

If every candidate was already held or already reported, say so plainly in one
line: a quiet sweep is a successful sweep, not a failure. Do not pad the
report with releases that failed either check just to have something to show.

End with a one-line summary: candidates checked, already held, already
reported, and genuine new findings.

OUTPUT DISCIPLINE — this report is emailed and texted, so it must be the
report and nothing else:

  - Do NOT narrate your process. No "I'll start with the inventory", no
    "checking holdings now", no "good haul". Call the tools silently and
    write once, at the end.
  - Do NOT give purchasing or acquisition advice. Not which edition is the
    better buy, not which is more efficient, not what to chase. You report
    what exists and whether it is held. Sourcing is Tim's decision and you do
    not weigh in on it.
  - Do NOT add a closing commentary paragraph after the summary line. The
    summary line is the end.
  - DO state deadline facts that belong to a release — a pre-order date, a
    limited pressing size — inside that release's entry, as facts rather
    than as a recommendation to act.
"""


def committed_entries(messages: list[dict]) -> list[dict]:
    """Structured releases the ledger actually recorded this run.

    Each entry is {title, verdict, release_date, status, url} as supplied to
    release_ledger('commit', entries_list=...). Only 'not_held' verdicts will
    trigger notifications, but all committed entries are returned for logging
    and the ledger UI.

    Read from the tool RESULT, not from the report prose. Whether a release
    was reported is a factual question, and parsing it from model-written text
    would make unattended behavior depend on wording.
    """
    entries: list[dict] = []
    seen: set[str] = set()
    for message in messages:
        if message.get("role") != "tool" or message.get("name") != "release_ledger":
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        for entry in payload.get("recorded") or []:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title")
            if title and title not in seen:
                seen.add(title)
                entries.append(entry)
    return entries


def run_sweep(artists: list[str], verbose: bool = True,
             progress_callback=None) -> tuple[str, list[dict]]:
    """Run one sweep. Returns (report text, structured entries recorded this run).

    If `progress_callback` is provided, call it with descriptive status
    strings such as "Building Plex inventory..." or "Searching Grateful
    Dead..." so live listeners (e.g. the web UI) can surface progress.
    """
    if progress_callback:
        progress_callback(f"Starting sweep for {len(artists)} artist(s)")

    client = _get_client()
    messages = _seed_messages()
    messages.append(
        {
            "role": "user",
            "content": SWEEP_PROMPT.format(
                artists=", ".join(artists),
                today=_dt.date.today().isoformat(),
            ),
        }
    )

    def _tool_cb(kind: str, data: dict):
        if not progress_callback:
            return
        name = data.get("name")
        args = data.get("arguments") or {}
        if name == "plex_query" and args.get("operation") == "inventory":
            progress_callback("Building Plex inventory...")
        elif name == "web_search":
            progress_callback(f"Searching: {args.get('query', '')[:60]}...")
        elif name == "release_ledger" and args.get("operation") == "commit":
            progress_callback("Committing findings to ledger...")
        else:
            # plex_query "have" and release_ledger "check" happen frequently;
            # skip most to avoid noise, but keep the user feeling motion.
            op = args.get("operation")
            if name == "plex_query" and op:
                progress_callback(f"Checking Plex: {op}")

    messages = run_turn(client, messages, verbose=verbose, stream_callback=_tool_cb)
    # Every assistant message, not just the last: the report is often written
    # alongside the final commit call, and taking messages[-1] would return
    # only the closing acknowledgement.
    report = assistant_text(messages)
    entries = committed_entries(messages)
    if progress_callback:
        held = sum(1 for e in entries if e.get("verdict") == "held")
        not_held = sum(1 for e in entries if e.get("verdict") == "not_held")
        progress_callback(f"Sweep complete — {len(entries)} recorded ({not_held} not held, {held} held)")
    return report, entries


def main() -> int:
    parser = argparse.ArgumentParser(description="Grateful Dead family release watch.")
    parser.add_argument(
        "--artist",
        action="append",
        help="Limit the sweep to this artist (repeatable). Defaults to all "
        "configured in LEMIEUX_WATCH_ARTISTS.",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="Also write the report to this file, with a timestamp header.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-tool-call output (for cron).",
    )
    parser.add_argument(
        "--ledger",
        action="store_true",
        help="Print the release ledger and exit.",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Email the report and text an alert (see LEMIEUX_* SMTP vars). "
        "By default only fires when the sweep found something.",
    )
    parser.add_argument(
        "--notify-check",
        action="store_true",
        help="Show notification settings and exit. Reveals no credentials.",
    )
    args = parser.parse_args()

    if args.notify_check:
        from .notify import configured

        print(json.dumps(configured(), indent=2))
        return 0

    if args.ledger:
        from .ledger import entries

        data = entries()
        print(f"Ledger: {data['ledger_path']}")
        if not data["exists"]:
            print("  (no ledger yet — nothing has been reported)")
            return 0
        print(f"  {data['total_tracked']} release(s) tracked\n")
        for row in data["releases"]:
            print(f"  {row.get('last_reported', '?')}  {row.get('title', '?')}")
            print(
                f"      verdict={row.get('verdict')}  "
                f"status={row.get('status')}  "
                f"release_date={row.get('release_date')}  "
                f"reported {row.get('times_reported', 1)}x"
            )
        return 0

    artists = args.artist or config.WATCH_ARTISTS
    if not artists:
        print("No artists configured. Set LEMIEUX_WATCH_ARTISTS.", file=sys.stderr)
        return 2

    if not args.quiet:
        print(f"Sweeping: {', '.join(artists)}\n", flush=True)

    report, entries = run_sweep(artists, verbose=not args.quiet)

    print("\n" + report)

    if args.report:
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            with open(args.report, "a", encoding="utf-8") as handle:
                handle.write(f"\n\n# Release sweep — {stamp}\n\n{report}\n")
        except OSError as exc:
            print(f"Could not write report to {args.report}: {exc}", file=sys.stderr)
            return 1

    not_held_count = sum(
        1 for entry in entries if entry.get("verdict") == "not_held"
    )
    if not args.notify and entries:
        from .notify import configured

        if configured()["email_to"] or configured()["sms_to"]:
            print(
                f"\n[{len(entries)} finding(s) recorded ({not_held_count} "
                f"not_held) — rerun with --notify to email/text them]",
                file=sys.stderr,
            )

    if args.notify:
        from .notify import notify

        outcome = notify(report, entries)
        if not args.quiet:
            print(f"\nNotify: {json.dumps(outcome)}", file=sys.stderr)
        if outcome.get("errors"):
            for problem in outcome["errors"]:
                print(f"Notification error — {problem}", file=sys.stderr)
            # The sweep itself succeeded; a delivery failure is reported but
            # must not look like the research failed.
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
