"""PRO number rules that differ by customer, read from app settings so they stay out of code.

    PRO_MIN_DIGITS / PRO_MAX_DIGITS  length of a valid PRO after suffixes are removed (default 6 / 12)
    PRO_STRIP_SUFFIXES               comma-separated trailing suffixes removed before validating
    PRO_SUFFIX_EXAMPLES              comma-separated suffixes quoted in review guidance

Deployed from ``.env`` (git-ignored) to the Function app settings, like RECEIVING_ORG_ALIASES. Unset
means neutral rules: 6-12 digits, no suffixes. App settings are fixed for the life of the process, so
reading them is deterministic inside the Durable orchestrator.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Mapping

DEFAULT_MIN_DIGITS = 6
DEFAULT_MAX_DIGITS = 12


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value and value.strip() else default
    except ValueError:
        return default


def _list(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def rules(env: Mapping[str, str] | None = None) -> dict:
    env = os.environ if env is None else env
    return {
        "minDigits": _int(env.get("PRO_MIN_DIGITS"), DEFAULT_MIN_DIGITS),
        "maxDigits": _int(env.get("PRO_MAX_DIGITS"), DEFAULT_MAX_DIGITS),
        "stripSuffixes": _list(env.get("PRO_STRIP_SUFFIXES")),
        "suffixExamples": _list(env.get("PRO_SUFFIX_EXAMPLES")),
    }


@lru_cache(maxsize=8)
def patterns(min_digits: int, max_digits: int, strip_suffixes: tuple) -> tuple[re.Pattern | None, re.Pattern]:
    """(trailing-suffix pattern or None, base-PRO pattern). Longest suffix first, case-insensitive."""
    suffix = None
    if strip_suffixes:
        alternatives = "|".join(re.escape(s) for s in sorted(strip_suffixes, key=len, reverse=True))
        suffix = re.compile(rf"(?:{alternatives})$", re.IGNORECASE)
    return suffix, re.compile(rf"^[0-9]{{{min_digits},{max_digits}}}$")
