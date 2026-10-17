"""
Notification — getting the release watch results off the server and to Tim.

Email carries every release that is currently not_held in the ledger: title,
release date or pre-sale status, and source URL. SMS carries a short alert
about just this sweep's new not_held findings, with the first title and its
source URL. Both bodies are templatable via the environment.

Only releases with a 'not_held' verdict are included. 'uncertain' releases are
still committed to the ledger but never appear in email or SMS — a human call
is required first.

Nothing is sent unless the sweep actually found a not_held gap, unless
LEMIEUX_NOTIFY_ON=always.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

SMTP_HOST = os.getenv("LEMIEUX_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("LEMIEUX_SMTP_PORT", "587"))
SMTP_USER = os.getenv("LEMIEUX_SMTP_USER", "")
SMTP_PASSWORD = os.getenv("LEMIEUX_SMTP_PASSWORD", "")

EMAIL_TO = os.getenv("LEMIEUX_EMAIL_TO", "")
# Full carrier gateway address, e.g. 4075551234@vtext.com — see .env.example.
SMS_TO = os.getenv("LEMIEUX_SMS_TO", "")

# "findings" (default) or "always".
NOTIFY_ON = os.getenv("LEMIEUX_NOTIFY_ON", "findings").lower()

# Plain-text templates, overridable from .env.
EMAIL_TEMPLATE = os.getenv(
    "LEMIEUX_EMAIL_TEMPLATE",
    "Lemieux found {count} new release(s):\n\n{releases}\n",
)
EMAIL_RELEASE_TEMPLATE = os.getenv(
    "LEMIEUX_EMAIL_RELEASE_TEMPLATE",
    "• {title} — {status_date}\n  {url}",
)
SMS_TEMPLATE = os.getenv(
    "LEMIEUX_SMS_TEMPLATE",
    "Lemieux: {count} new — {title}{more} {url}",
)
SMS_MORE_TEMPLATE = os.getenv("LEMIEUX_SMS_MORE_TEMPLATE", " +{extra_count} more")

# Carrier gateways reject or split long messages. Keep well under the limit.
SMS_MAX_CHARS = int(os.getenv("LEMIEUX_SMS_MAX_CHARS", "300"))


class NotifyError(Exception):
    """Raised when a notification cannot be sent."""


def configured() -> dict:
    """What is set up, without revealing any credential."""
    return {
        "smtp_host": SMTP_HOST,
        "smtp_port": SMTP_PORT,
        "smtp_user_set": bool(SMTP_USER),
        "smtp_password_set": bool(SMTP_PASSWORD),
        "email_to": EMAIL_TO or None,
        "sms_to": SMS_TO or None,
        "notify_on": NOTIFY_ON,
        "email_template_set": EMAIL_TEMPLATE != (
            "Lemieux found {count} new release(s):\n\n{releases}\n"
        ),
        "sms_template_set": SMS_TEMPLATE != (
            "Lemieux: {count} new — {title}{more} {url}"
        ),
    }


def _send(to_address: str, subject: str, body: str) -> None:
    """Send one message over SMTP with STARTTLS."""
    if not SMTP_USER or not SMTP_PASSWORD:
        raise NotifyError(
            "LEMIEUX_SMTP_USER and LEMIEUX_SMTP_PASSWORD must be set. For "
            "Gmail, use an App Password — plain account passwords are "
            "rejected."
        )

    message = EmailMessage()
    message["From"] = SMTP_USER
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise NotifyError(
            f"SMTP authentication failed for {SMTP_USER}. With Gmail this "
            f"almost always means an App Password is required. ({exc})"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"Could not send to {to_address}: {exc}") from exc


def _maybe_shorten(url: str) -> str:
    """Placeholder for a short-link / redirect service.

    When a service is configured (e.g. LEMIEUX_SHORTENER_URL), this becomes
    the integration point. For now the original URL is returned unchanged.
    """
    return url


def _not_held_entries(entries: list[dict]) -> list[dict]:
    """Filter to definite gaps only; uncertain releases are not notified."""
    return [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("verdict") == "not_held"
    ]


def _ledger_not_held() -> list[dict]:
    """All not_held releases currently tracked in the ledger.

    The email is the persistent inbox for all holes in the library, while the
    SMS is a tap on the shoulder for the current sweep only.
    """
    from .ledger import entries

    data = entries()
    return _not_held_entries(data.get("releases", []))


def _status_date_fragment(entry: dict) -> str:
    """Combine status and release date into one clean fragment.

    Examples:
      pre_order + date  -> "pre-sale — 2025-11-21"
      announced + no date -> "announced"
      no status + date -> "2025-11-21"
      neither -> ""
    """
    raw_status = (entry.get("status") or "").strip()
    release_date = (entry.get("release_date") or "").strip()
    status_lower = raw_status.lower().replace(" ", "_").replace("-", "_")

    if status_lower in {"pre_order", "preorder"}:
        status_line = "pre-sale"
    elif status_lower in {"released", "announced", "upcoming", "pending"}:
        status_line = raw_status.lower()
    elif raw_status:
        status_line = raw_status
    else:
        status_line = ""

    if status_line and release_date:
        return f"{status_line} — {release_date}"
    return status_line or release_date


def _format_email_release(entry: dict) -> str:
    """Format a single release for the email body."""
    title = (entry.get("title") or "Unknown release").strip()
    url = _maybe_shorten((entry.get("url") or "").strip())
    status_date = _status_date_fragment(entry)

    # Provide backward-compatible variables so custom templates can still use
    # {status} and {date} separately if desired.
    raw_status = (entry.get("status") or "").strip()
    status_lower = raw_status.lower().replace(" ", "_").replace("-", "_")
    if status_lower in {"pre_order", "preorder"}:
        status = "pre-sale"
    elif raw_status:
        status = raw_status.lower()
    else:
        status = ""
    date = (entry.get("release_date") or "").strip()

    return EMAIL_RELEASE_TEMPLATE.format(
        title=title,
        url=url,
        status=f"{status} " if status else "",
        date=f" — {date}" if date else "",
        status_date=status_date,
    )


def _email_body(entries: list[dict]) -> str:
    """Build the concise email body from all ledger not_held releases."""
    # Start with the current sweep's entries so the most recently discovered
    # gaps appear first, then append the rest of the ledger not yet held.
    gaps = _not_held_entries(entries)
    seen = {entry.get("title", "") for entry in gaps}

    for entry in _ledger_not_held():
        title = entry.get("title", "")
        if title and title not in seen:
            seen.add(title)
            gaps.append(entry)

    if not gaps:
        return ""

    release_lines = [_format_email_release(entry) for entry in gaps]
    return EMAIL_TEMPLATE.format(
        count=len(gaps),
        releases="\n".join(release_lines),
    )


def _sms_body(entries: list[dict]) -> str:
    """Build a short, link-bearing SMS alert from this sweep's not_held entries."""
    gaps = _not_held_entries(entries)
    if not gaps:
        return ""

    first = gaps[0]
    title = (first.get("title") or "Unknown release").strip()
    url = _maybe_shorten((first.get("url") or "").strip())

    more = ""
    if len(gaps) > 1:
        more = SMS_MORE_TEMPLATE.format(extra_count=len(gaps) - 1)

    text = SMS_TEMPLATE.format(
        count=len(gaps),
        title=title,
        more=more,
        url=url,
    )

    if len(text) > SMS_MAX_CHARS:
        text = text[: SMS_MAX_CHARS - 1] + "…"
    return text


def notify(
    report: str,
    entries: list[dict],
    subject_prefix: str = "Lemieux release watch",
) -> dict:
    """Send the report. Returns what was sent and what was skipped.

    `report` is kept for logging/backward compatibility but is no longer the
    email body.

    SMS is built from this sweep's not_held entries only. Email lists every
    not_held release in the ledger, so the inbox is a complete view of holes
    yet to be filled.
    """
    # Preserve compatibility: a plain list of strings could be passed by an
    # older caller; turn it into minimal structured entries.
    structured: list[dict]
    if entries and isinstance(entries[0], str):
        structured = [{"title": entry, "verdict": "not_held"} for entry in entries]
    else:
        structured = [
            entry for entry in entries if isinstance(entry, dict)
        ]

    this_sweep_gaps = _not_held_entries(structured)
    this_sweep_count = len(this_sweep_gaps)

    result = {"email": "skipped", "sms": "skipped", "errors": []}

    if this_sweep_count == 0 and NOTIFY_ON != "always":
        result["reason"] = (
            "No new not_held findings this sweep; LEMIEUX_NOTIFY_ON=findings. "
            "Nothing sent."
        )
        return result

    # Email subject reflects the total unacquired backlog, not just today.
    total_unacquired = len(_ledger_not_held())
    subject = (
        f"{subject_prefix}: {this_sweep_count} new, {total_unacquired} open"
        if this_sweep_count
        else f"{subject_prefix}: nothing new, {total_unacquired} open"
    )

    if EMAIL_TO:
        body = _email_body(structured)
        try:
            _send(EMAIL_TO, subject, body or "(no not_held releases)")
            result["email"] = f"sent to {EMAIL_TO}"
        except NotifyError as exc:
            result["errors"].append(f"email: {exc}")
            result["email"] = "failed"

    if SMS_TO and this_sweep_count:
        body = _sms_body(structured)
        try:
            # Carrier gateways turn the subject into part of the message;
            # an empty subject keeps the text clean.
            _send(SMS_TO, "", body or "Lemieux: new release found.")
            result["sms"] = f"sent to {SMS_TO}"
        except NotifyError as exc:
            result["errors"].append(f"sms: {exc}")
            result["sms"] = "failed"

    return result
