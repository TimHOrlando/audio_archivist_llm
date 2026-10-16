"""
System prompt loading.

The Modelfile stays the single source of truth for Lemieux's persona and
policy, but the prompt is injected as a system message at runtime rather than
baked into a derived model via `ollama create`.

Two reasons:

1. Cloud models (`kimi-k3:cloud` and friends) are thin pointers to a remote
   host. A model derived from one does not reliably inherit the remote binding
   or its capability flags, which surfaces as `does not support chat`.
2. Editing the prompt should not require a rebuild. Injection means the next
   run picks up the change.

The practical consequence: LEMIEUX_MODEL can point straight at a base model.
No `ollama create` step is needed at all.
"""

from __future__ import annotations

import re

from .config import PROJECT_ROOT

# Matches:  SYSTEM """ ... """   (the only multi-line form we author)
_SYSTEM_BLOCK = re.compile(
    r'^\s*SYSTEM\s+"""(?P<body>.*?)"""', re.DOTALL | re.MULTILINE
)


class PromptError(Exception):
    """Raised when the system prompt cannot be loaded."""


def load_system_prompt(modelfile: str | None = None) -> str:
    """Extract the SYSTEM block from a Modelfile.

    `modelfile` is a filename relative to the project root, or an absolute
    path. Defaults to LEMIEUX_MODELFILE, else 'Modelfile'.
    """
    import os

    name = modelfile or os.getenv("LEMIEUX_MODELFILE", "Modelfile")
    path = PROJECT_ROOT / name

    if not path.is_file():
        raise PromptError(
            f"Modelfile not found at {path}. Set LEMIEUX_MODELFILE, or run "
            f"from the project root."
        )

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"Could not read {path}: {exc}") from exc

    match = _SYSTEM_BLOCK.search(text)
    if not match:
        raise PromptError(
            f'No SYSTEM """...""" block found in {path}. The prompt must be '
            f"in a triple-quoted SYSTEM directive."
        )

    body = match.group("body").strip()
    if not body:
        raise PromptError(f"The SYSTEM block in {path} is empty.")

    return body
