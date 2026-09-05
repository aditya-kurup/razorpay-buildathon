"""Versioned prompt selection for authority-level experiments."""

from __future__ import annotations

from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent
AUTHORITY_LEVELS = (
    "options_narrowed_in_code",
    "constraints_in_prompt",
    "no_constraints",
)

_PROMPT_FILES = {
    "options_narrowed_in_code": "v1_options_narrowed_in_code.md",
    "constraints_in_prompt": "v1_constraints_in_prompt.md",
    "no_constraints": "v1_no_constraints.md",
}

CUSTOMER_INITIATED_PROMPT_FILE = "v1_customer_initiated.md"


def get_prompt(authority_level: str = "options_narrowed_in_code") -> str:
    """Load one versioned authority prompt by its configured name."""

    try:
        filename = _PROMPT_FILES[authority_level]
    except KeyError as exc:
        raise ValueError(
            f"unknown authority level {authority_level!r}; "
            f"choose from {AUTHORITY_LEVELS}"
        ) from exc
    return (PROMPT_DIR / filename).read_text(encoding="utf-8")


def get_customer_initiated_prompt() -> str:
    """Load the single-turn customer-initiated proposer prompt."""

    return (PROMPT_DIR / CUSTOMER_INITIATED_PROMPT_FILE).read_text(encoding="utf-8")


__all__ = [
    "AUTHORITY_LEVELS",
    "CUSTOMER_INITIATED_PROMPT_FILE",
    "PROMPT_DIR",
    "get_customer_initiated_prompt",
    "get_prompt",
]
