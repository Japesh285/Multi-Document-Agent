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


def call_chat(
    messages: list[dict[str, str]],
    stream_to_stdout: bool = False,
) -> str:
    """
    Call Ollama /api/chat and return the full assistant response.
    If stream_to_stdout=True, tokens are printed live as they arrive.
    """
    payload = {
        "model":   OLLAMA_MODEL,
        "messages": messages,
        "stream":  True,
        "options": {"temperature": 0.05},   # near-deterministic for code
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
