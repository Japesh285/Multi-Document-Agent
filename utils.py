"""
utils.py — Logging configuration and display helpers.
"""

from __future__ import annotations
import logging
import os
import sys

# Set VERBOSE=1 in environment to enable debug output
_VERBOSE = os.environ.get("VERBOSE", "0").strip() == "1"

_FMT  = "%(asctime)s  %(levelname)-7s  %(name)s — %(message)s"
_DATEFMT = "%H:%M:%S"

logging.basicConfig(
    level=logging.DEBUG if _VERBOSE else logging.WARNING,
    format=_FMT,
    datefmt=_DATEFMT,
    stream=sys.stderr,
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# CLI display helpers
# ---------------------------------------------------------------------------

_SEP = "─" * 52


def print_section(title: str, body: str) -> None:
    print(f"\n  ── {title} {'─' * max(0, 46 - len(title))}")
    for line in body.splitlines():
        print(f"  {line}")
    print(f"  {_SEP}")


def print_result(result: str, error: str | None, attempts: int, elapsed: float) -> None:
    label = f"attempt {attempts}  {elapsed:.1f}s"
    if error:
        print_section(f"FAILED  [{label}]", error)
    else:
        print_section(f"Result  [{label}]", result)


def print_debug_prompt(messages: list[dict]) -> None:
    """Print full prompt when VERBOSE=1."""
    log = get_logger("debug")
    if not _VERBOSE:
        return
    for i, m in enumerate(messages):
        role    = m.get("role", "?").upper()
        content = m.get("content", "")
        log.debug("MSG[%d] %s (%d chars):\n%s", i, role, len(content), content[:400])
