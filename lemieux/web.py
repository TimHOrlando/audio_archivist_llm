"""
Lemieux web chat — FastAPI server for phone-accessible conversations.

Serves a single-page mobile chat UI and exposes endpoints for conversation
state, chat turns, and manual release sweeps. Runs on hal9003 inside the
media network; access it via Tailscale/ZeroTier or a LAN connection.

Authentication: a single password via the Authorization Bearer header. Set
LEMIEUX_WEB_PASSWORD in .env. This is not a public-facing service; transport
security comes from Tailscale or your home network.
"""

from __future__ import annotations

import json
import time
import os
from pathlib import Path

from lemieux import config
from lemieux.agent import _get_client, _seed_messages, assistant_text, run_turn
from lemieux.notify import notify
from lemieux.watch import run_sweep as _run_sweep

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, Response
    from fastapi.responses import HTMLResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ImportError as exc:  # pragma: no cover - runtime guard for missing deps
    raise ImportError(
        "FastAPI is not installed. Run: pip install fastapi uvicorn"
    ) from exc



app = FastAPI(title="Lemieux", description="Plex archivist chat")
security = HTTPBearer(auto_error=False)

# In-memory sessions. Single-user service; restart clears history.
_sessions: dict[str, dict] = {}


def _deny():
    raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def verify_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if not config.WEB_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="LEMIEUX_WEB_PASSWORD is not configured; the server is disabled.",
        )
    if not credentials or credentials.scheme.lower() != "bearer":
        _deny()
    if credentials.credentials != config.WEB_PASSWORD:
        _deny()
    return credentials.credentials


def _session_id(request: Request) -> str:
    # Use a cookie to keep a session across requests from the same browser.
    sid = request.cookies.get("lemieux_session")
    if not sid:
        sid = f"{time.time_ns()}"
    return sid


def _get_session(request: Request, response: Response) -> dict:
    sid = _session_id(request)
    # If we already have a session it probably carries stale messages from an
    # earlier browser session. Start fresh each request is surprising for a real
    # chat app, but this service is used transiently from a phone. We choose
    # persistence: if the cookie matches a known session, reuse it.
    if sid not in _sessions:
        sid = f"{time.time_ns()}"
    session = _sessions.setdefault(sid, {"messages": _seed_messages()})
    response.set_cookie("lemieux_session", sid, httponly=True, samesite="strict")
    return session


@app.get("/", response_class=HTMLResponse)
async def index():
    path = Path(__file__).resolve().parent / "templates" / "index.html"
    return HTMLResponse(content=path.read_text(encoding="utf-8"))


@app.post("/chat")
async def chat(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    verify_token(credentials)
    session = _get_session(request, response)

    body = await request.json()
    user_text = str(body.get("message", "")).strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="message is required")

    client = _get_client()
    session["messages"].append({"role": "user", "content": user_text})

    tools_used: list[dict] = []

    def stream_callback(kind: str, data: dict):
        if kind == "tool":
            tools_used.append(data)

    session["messages"] = run_turn(
        client, session["messages"], verbose=False, stream_callback=stream_callback
    )

    reply = assistant_text(session["messages"])
    return {"reply": reply, "tools": tools_used}


@app.post("/session")
async def clear_session(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    verify_token(credentials)
    sid = _session_id(request)
    _sessions[sid] = {"messages": _seed_messages()}
    response.set_cookie("lemieux_session", sid, httponly=True, samesite="strict")
    return {"cleared": True}


@app.post("/sweep")
async def sweep(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    verify_token(credentials)
    session = _get_session(request, response)

    body = await request.json()
    artists = body.get("artists")
    if artists and not isinstance(artists, list):
        artists = [str(artists)]

    report, entries = _run_sweep(artists or config.WATCH_ARTISTS, verbose=False)

    if entries:
        outcome = notify(report, entries)
    else:
        outcome = {"reason": "No not_held findings; nothing sent.", "email": "skipped", "sms": "skipped"}

    return {"report": report, "entries": entries, "count": len(entries), "notify": outcome}


@app.get("/health")
async def health():
    return {"status": "ok", "model": config.MODEL, "host": config.OLLAMA_HOST}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
