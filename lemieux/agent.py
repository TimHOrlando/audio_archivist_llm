"""
Lemieux — agent loop and CLI.

Runs the tool-calling conversation against Ollama. Usage:

    python -m lemieux.agent                      # interactive REPL
    python -m lemieux.agent "audit /disks/media_storage/dead"
    python -m lemieux.agent --check              # config + connectivity only
"""

from __future__ import annotations

import json
import sys

from . import config
from .prompt import PromptError, load_system_prompt
from .registry import TOOL_SCHEMAS, call_tool


def _seed_messages() -> list[dict]:
    """Start a transcript, with the system prompt if injection is enabled."""
    if not config.INJECT_SYSTEM:
        return []
    try:
        return [{"role": "system", "content": load_system_prompt()}]
    except PromptError as exc:
        # Running without the persona is a real degradation — the safety rules
        # live in that prompt — so say so loudly rather than carrying on quietly.
        print(f"WARNING: no system prompt loaded ({exc})", file=sys.stderr)
        print(
            "WARNING: the model will run WITHOUT its archivist persona and "
            "policy rules. The path jail still applies.",
            file=sys.stderr,
        )
        return []


def assistant_text(messages: list[dict]) -> str:
    """All prose the assistant produced this turn, in order.

    NOT just messages[-1]. A model often writes its report in the same turn as
    a trailing tool call (committing the ledger, say), and then closes with a
    one-line acknowledgement. Taking only the last message discards the report
    and leaves the user with "done!" and nothing else.
    """
    parts = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = (message.get("content") or "").strip()
        if content and content not in parts:
            parts.append(content)
    return "\n\n".join(parts)


def _get_client():
    try:
        from ollama import Client
    except ImportError:
        sys.exit("ollama is not installed. Run: pip install ollama")
    return Client(host=config.OLLAMA_HOST)


def _normalize_tool_call(call) -> tuple[str, dict]:
    """Extract (name, arguments) from a tool call.

    The ollama client has returned both dicts and pydantic objects across
    versions, so handle either rather than pinning to one shape.
    """
    function = call["function"] if isinstance(call, dict) else call.function
    name = function["name"] if isinstance(function, dict) else function.name
    raw_args = (
        function.get("arguments", {})
        if isinstance(function, dict)
        else function.arguments
    )

    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            raw_args = {}

    return name, dict(raw_args or {})


def run_turn(client, messages: list[dict], verbose: bool = True,
             stream_callback=None) -> list[dict]:
    """Drive one user turn to completion, resolving tool calls along the way.

    Returns the updated message list. Appends every assistant and tool message
    so the transcript stays intact across turns.

    If `stream_callback` is provided, it is called with ("tool", {"name": ...,
    "arguments": ...}) before a tool runs, giving a caller such as a web app a
    chance to show live progress.
    """
    for iteration in range(config.MAX_TOOL_ITERATIONS):
        response = client.chat(
            model=config.MODEL,
            messages=messages,
            tools=TOOL_SCHEMAS,
        )

        message = response["message"] if isinstance(response, dict) else response.message
        message = dict(message) if not isinstance(message, dict) else message
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return messages

        for call in tool_calls:
            name, arguments = _normalize_tool_call(call)
            if verbose:
                print(f"  → {name}({json.dumps(arguments, default=str)})", flush=True)

            if stream_callback:
                stream_callback("tool", {"name": name, "arguments": arguments})

            result = call_tool(name, arguments)

            if verbose:
                preview = result if len(result) <= 300 else result[:300] + "…"
                print(f"  ← {preview}", flush=True)

            messages.append({"role": "tool", "name": name, "content": result})

    # Out of iterations. Do NOT return here — the last message would be a tool
    # result, and the caller would print raw JSON as the report, discarding
    # every finding. Force one final turn with NO tools offered, so the model
    # has to write prose from what it already gathered.
    if verbose:
        print(
            f"  [iteration limit {config.MAX_TOOL_ITERATIONS} reached — "
            f"requesting final summary]",
            flush=True,
        )

    messages.append(
        {
            "role": "user",
            "content": (
                "You have reached the tool-call limit for this run. Stop "
                "searching. Write your final report NOW using only what you "
                "have already gathered.\n\n"
                "Report every finding whose holdings status you confirmed. "
                "For any candidate you found but did not get to check, list "
                "it under 'Unverified — holdings not checked' and say plainly "
                "that you did not confirm whether it is held. Do not guess at "
                "holdings for those, and do not silently drop them."
            ),
        }
    )

    final = client.chat(model=config.MODEL, messages=messages)
    message = final["message"] if isinstance(final, dict) else final.message
    messages.append(dict(message) if not isinstance(message, dict) else message)
    return messages


def check() -> int:
    """Print effective config and probe Plex. Run this before anything else."""
    report = config.config_report()
    print(json.dumps(report, indent=2))

    if config.INJECT_SYSTEM:
        try:
            prompt_text = load_system_prompt()
            print(f"\nSystem prompt: loaded, {len(prompt_text)} chars")
        except PromptError as exc:
            print(f"\nWARNING: system prompt NOT loaded — {exc}")

    unmounted = [p for p, ok in report["roots_mounted"].items() if not ok]
    if unmounted:
        print(f"\nWARNING: media roots not present on this host: {unmounted}")

    if not report["plex_token_present"]:
        print("\nWARNING: PLEX_TOKEN is unset — plex_query will refuse every call.")
        return 1

    print("\nProbing Plex…")
    print(call_tool("plex_query", {"operation": "sections"}))
    return 0


def main() -> int:
    args = sys.argv[1:]

    if args and args[0] in ("--check", "-c"):
        return check()

    client = _get_client()
    messages: list[dict] = _seed_messages()

    if args:
        messages.append({"role": "user", "content": " ".join(args)})
        messages = run_turn(client, messages)
        print("\n" + assistant_text(messages))
        return 0

    print(f"Lemieux ({config.MODEL}) — read-only. Ctrl-D to exit.\n")
    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue

        before = len(messages)
        messages.append({"role": "user", "content": prompt})
        messages = run_turn(client, messages)
        print(f"\nlemieux> {assistant_text(messages[before:])}\n")


if __name__ == "__main__":
    raise SystemExit(main())
