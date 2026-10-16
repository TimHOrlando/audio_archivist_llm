"""
Notification — getting a sweep report off the server and to Tim.

Email carries the full report. SMS carries a short alert, because a carrier
gateway will mangle or silently truncate anything long and a multi-page
release report delivered as twelve fragmented texts is worse than no text at
all.

Nothing is sent unless the sweep actually found something, unless
LEMIEUX_NOTIFY_ON=always. A watch that mails "nothing new" every week trains
you to filter it, and then the one that matters goes unread too.

Credentials come from the environment, never from code or the repo. For Gmail
that means an App Password (Google account -> Security -> 2-Step Verification
-> App passwords), not the account password: Gmail refuses plain-password SMTP.
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

# Carrier gateways reject or split long messages. Keep well under the limit.
SMS_MAX_CHARS = 300


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


def _sms_text(finding_count: int, titles: list[str]) -> str:
    """Short alert. The detail lives in the email."""
    if finding_count == 1 and titles:
        text = f"Lemieux: 1 new release — {titles[0]}"
    else:
        text = f"Lemieux: {finding_count} new releases"
        if titles:
            text += f" — {titles[0]}"
            if len(titles) > 1:
                text += f" +{len(titles) - 1} more"
    text += ". Details in email."

    if len(text) > SMS_MAX_CHARS:
        text = text[: SMS_MAX_CHARS - 1] + "…"
    return text


def notify(
    report: str,
    finding_count: int,
    titles: list[str] | None = None,
    subject_prefix: str = "Lemieux release watch",
) -> dict:
    """Send the report. Returns what was sent and what was skipped."""
    titles = titles or []
    result = {"email": "skipped", "sms": "skipped", "errors": []}

    if finding_count == 0 and NOTIFY_ON != "always":
        result["reason"] = (
            "No new findings; LEMIEUX_NOTIFY_ON=findings. Nothing sent."
        )
        return result

    if EMAIL_TO:
        subject = (
            f"{subject_prefix}: {finding_count} new"
            if finding_count
            else f"{subject_prefix}: nothing new"
        )
        try:
            _send(EMAIL_TO, subject, report or "(empty report)")
            result["email"] = f"sent to {EMAIL_TO}"
        except NotifyError as exc:
            result["errors"].append(f"email: {exc}")
            result["email"] = "failed"

    if SMS_TO and finding_count:
        try:
            # Carrier gateways turn the subject into part of the message;
            # an empty subject keeps the text clean.
            _send(SMS_TO, "", _sms_text(finding_count, titles))
            result["sms"] = f"sent to {SMS_TO}"
        except NotifyError as exc:
            result["errors"].append(f"sms: {exc}")
            result["sms"] = "failed"

    return result
