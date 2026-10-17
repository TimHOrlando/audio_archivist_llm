"""
Lemieux web chat — FastAPI server for phone-accessible conversations.

Serves a single-page mobile chat UI and exposes endpoints for conversation
state, chat turns, and manual release sweeps. Runs on hal9003 inside the
media network; access it via Tailscale/ZeroTier or a LAN connection.

Authentication: a single password defined in LEMIEUX_WEB_PASSWORD. The UI
authenticates via a 30-day HTTP-only cookie; a Bearer-token fallback remains
for compatibility. This is not a public-facing service; transport security
comes from Tailscale or your home network.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import os
import traceback
from pathlib import Path

from lemieux import config
from lemieux.agent import _get_client, _seed_messages, assistant_text, run_turn
from lemieux.notify import notify
from lemieux.watch import run_sweep as _run_sweep

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, Response
    from fastapi.responses import HTMLResponse, StreamingResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ImportError as exc:  # pragma: no cover - runtime guard for missing deps
    raise ImportError(
        "FastAPI is not installed. Run: pip install fastapi uvicorn"
    ) from exc

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(
    filename=str(PROJECT_ROOT / "web-error.log"),
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("lemieux.web")
logger.addHandler(logging.StreamHandler())  # also echo to uvicorn terminal


app = FastAPI(title="Lemieux", description="Plex archivist chat")
security = HTTPBearer(auto_error=False)

# In-memory sessions. Single-user service; restart clears history.
_sessions: dict[str, dict] = {}


def _deny():
    raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


_AUTH_TOKENS: set[str] = set()
_AUTH_COOKIE_NAME = "lemieux_auth"


def _set_auth_cookie(response: Response) -> None:
    token = secrets.token_urlsafe(32)
    _AUTH_TOKENS.add(token)
    response.set_cookie(
        _AUTH_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 days
    )


def _clear_auth_cookie(request: Request, response: Response) -> None:
    token = request.cookies.get(_AUTH_COOKIE_NAME)
    if token:
        _AUTH_TOKENS.discard(token)
    response.delete_cookie(_AUTH_COOKIE_NAME)


def _authed(request: Request) -> bool:
    """True if the request presents a valid bearer token or auth cookie."""
    if not config.WEB_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="LEMIEUX_WEB_PASSWORD is not configured; the server is disabled.",
        )

    # Cookie-based session (preferred on mobile browsers)
    token = request.cookies.get(_AUTH_COOKIE_NAME)
    if token and token in _AUTH_TOKENS:
        return True

    # Bearer fallback (e.g. localStorage-stored password via HTTP header).
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        if auth_header.split(None, 1)[1] == config.WEB_PASSWORD:
            return True

    # Same bearer fallback via query string, because EventSource cannot set
    # custom headers and some mobile contexts need an alternate credential path.
    bearer = request.query_params.get("bearer", "")
    if bearer and bearer == config.WEB_PASSWORD:
        return True

    return False


def require_auth(request: Request, response: Response) -> None:
    """Dependency that enforces authentication and refreshes the cookie."""
    if not _authed(request):
        _deny()
    # Refresh the cookie expiration on every successful request.
    token = request.cookies.get(_AUTH_COOKIE_NAME)
    if token and token in _AUTH_TOKENS:
        response.set_cookie(
            _AUTH_COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )


def verify_token(credentials: HTTPAuthorizationCredentials | None = None) -> None:
    """Compatibility shim for endpoints still using the Bearer dependency."""
    # Real auth check is performed by require_auth via the Request object.
    # We keep the HTTPBearer parameter in route signatures so FastAPI's docs
    # still show the API as authenticated, but we don't re-validate here.
    pass


def _session_id(request: Request) -> str:
    sid = request.cookies.get("lemieux_session")
    if not sid:
        sid = f"{time.time_ns()}"
    return sid


def _get_session(request: Request, response: Response) -> dict:
    sid = _session_id(request)
    if sid not in _sessions:
        sid = f"{time.time_ns()}"
    session = _sessions.setdefault(sid, {"messages": _seed_messages()})
    response.set_cookie("lemieux_session", sid, httponly=True, samesite="lax")
    return session


@app.get("/", response_class=HTMLResponse)
async def index():
    path = Path(__file__).resolve().parent / "templates" / "index.html"
    return HTMLResponse(content=path.read_text(encoding="utf-8"))


@app.get("/auth-check")
async def auth_check(request: Request, response: Response) -> dict:
    if not _authed(request):
        raise HTTPException(status_code=401, detail="unauthenticated")
    require_auth(request, response)  # refresh cookie
    return {"authenticated": True}


@app.post("/login")
async def login(request: Request, response: Response) -> dict:
    body = await request.json()
    supplied = str(body.get("password", "")).strip()

    if not config.WEB_PASSWORD:
        raise HTTPException(
            status_code=500, detail="LEMIEUX_WEB_PASSWORD is not configured"
        )
    if supplied != config.WEB_PASSWORD:
        raise HTTPException(status_code=401, detail="invalid password")

    _set_auth_cookie(response)
    return {"ok": True}


@app.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    _clear_auth_cookie(request, response)
    return {"ok": True}


@app.post("/chat")
async def chat(request: Request, response: Response) -> dict:
    require_auth(request, response)
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

    try:
        session["messages"] = run_turn(
            client, session["messages"], verbose=False, stream_callback=stream_callback
        )
    except Exception as exc:
        logger.error("run_turn failed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Model error: {exc}")

    reply = assistant_text(session["messages"])
    return {"reply": reply, "tools": tools_used}


@app.post("/session")
async def clear_session(request: Request, response: Response):
    require_auth(request, response)
    sid = _session_id(request)
    _sessions[sid] = {"messages": _seed_messages()}
    response.set_cookie("lemieux_session", sid, httponly=True, samesite="lax")
    return {"cleared": True}


@app.post("/sweep")
async def sweep(request: Request, response: Response) -> dict:
    require_auth(request, response)
    _ = _get_session(request, response)

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


@app.get("/sweep-stream")
async def sweep_stream(request: Request, response: Response):
    require_auth(request, response)

    async def event_stream():
        import asyncio

        queue: list[str] = []
        completed: dict = {}

        def progress_callback(message: str):
            queue.append(message)

        def run_sweep_sync():
            try:
                report, entries = _run_sweep(
                    config.WATCH_ARTISTS, verbose=False,
                    progress_callback=progress_callback,
                )
                if entries:
                    outcome = notify(report, entries)
                else:
                    outcome = {
                        "reason": "No not_held findings; nothing sent.",
                        "email": "skipped",
                        "sms": "skipped",
                    }
                completed["payload"] = {
                    "report": report,
                    "entries": entries,
                    "count": len(entries),
                    "notify": outcome,
                }
            except Exception as exc:
                logger.error("sweep_stream failed: %s\n%s", exc, traceback.format_exc())
                completed["error"] = str(exc)

        loop = asyncio.get_event_loop()
        sweep_task = loop.run_in_executor(None, run_sweep_sync)

        while not sweep_task.done() or queue:
            if queue:
                msg = queue.pop(0)
                yield f"event: progress\ndata: {json.dumps({'message': msg})}\n\n"
            else:
                await asyncio.sleep(0.5)

        if "error" in completed:
            yield f"event: error\ndata: {json.dumps({'error': completed['error']})}\n\n"
        else:
            yield f"event: done\ndata: {json.dumps(completed['payload'])}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "model": config.MODEL, "host": config.OLLAMA_HOST}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
