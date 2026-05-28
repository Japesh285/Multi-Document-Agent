"""
llm.py — Ollama /api/chat streaming client.
"""

from __future__ import annotations
import json
import re
import httpx
from utils import get_logger

log = get_logger("llm")

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL    = "qwen2.5-coder:14b"

# Keep the model resident in VRAM between calls — kills cold-start latency.
# "30m" idle window before Ollama unloads. Set to "-1" for "never unload"
# (only safe if this is the only model you use on the box).
OLLAMA_KEEP_ALIVE = "30m"


def call_chat(
    messages: list[dict[str, str]],
    stream_to_stdout: bool = False,
) -> str:
    """
    Call Ollama /api/chat and return the full assistant response.
    If stream_to_stdout=True, tokens are printed live as they arrive.

    The `keep_alive` parameter tells Ollama to keep the model loaded in VRAM
    for OLLAMA_KEEP_ALIVE after the call returns, so subsequent calls
    in the same session don't pay the cold-load cost.
    """
    payload = {
        "model":      OLLAMA_MODEL,
        "messages":   messages,
        "stream":     True,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options":    {"temperature": 0.05},   # near-deterministic for code
    }

    log.debug("→ Ollama  model=%s  messages=%d", OLLAMA_MODEL, len(messages))

    full = ""
    try:
        with httpx.Client(timeout=180.0) as client:
            with client.stream("POST", OLLAMA_CHAT_URL, json=payload) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    full += token
                    if stream_to_stdout:
                        print(token, end="", flush=True)
                    if chunk.get("done"):
                        break
    except httpx.ConnectError:
        raise ConnectionError(
            "Cannot reach Ollama at http://localhost:11434 — is it running?"
        )

    if stream_to_stdout:
        print()

    log.debug("← Ollama  response_len=%d chars", len(full))
    return full


def warm_up(timeout: float = 60.0) -> bool:
    """
    Fire a tiny, throwaway request to force Ollama to load the model into
    VRAM. Combined with keep_alive, this means the first real user query
    no longer pays the cold-start cost.

    Returns True on success, False if Ollama is unreachable or errored.
    Safe to call at server boot.
    """
    log.debug("warm_up: priming Ollama model %s …", OLLAMA_MODEL)
    payload = {
        "model":      OLLAMA_MODEL,
        "messages":   [{"role": "user", "content": "ok"}],
        "stream":     False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options":    {"temperature": 0.0, "num_predict": 1},
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(OLLAMA_CHAT_URL, json=payload)
            r.raise_for_status()
            log.info("warm_up: model %s primed and held for %s",
                     OLLAMA_MODEL, OLLAMA_KEEP_ALIVE)
            return True
    except Exception as exc:
        log.warning("warm_up: skipped — %s", exc)
        return False


def extract_code(text: str) -> str:
    """Strip markdown fences from model response; return raw code."""
    for pattern in (
        r"```python\n(.*?)```",
        r"```\n(.*?)```",
        r"```(.*?)```",
    ):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            code = m.group(1).strip()
            log.debug("extract_code: stripped markdown fence")
            return code
    return text.strip()
