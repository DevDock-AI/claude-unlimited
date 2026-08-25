"""Locale loading.

Adding a language means dropping a `<code>.json` file into locales/ with the
same keys as en.json — no code change, no registration step.

en.json is the canonical key set. A locale missing a key falls back to the
English string, so a partial translation still renders instead of showing a
blank or a raw key.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LOCALE = "en"

_LOCALE_CODE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


class UnknownLocaleError(ValueError):
    pass


def _read_locale_file(code: str) -> dict:
    return json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))


def list_locales() -> List[str]:
    """Every available language, derived from the files present rather than
    a hardcoded list."""
    codes = sorted(p.stem for p in LOCALES_DIR.glob("*.json") if _LOCALE_CODE_RE.match(p.stem))
    return codes or [DEFAULT_LOCALE]


def locale_display_names() -> Dict[str, str]:
    names = {}
    for code in list_locales():
        try:
            names[code] = _read_locale_file(code).get("_meta", {}).get("language_name", code)
        except (OSError, json.JSONDecodeError):
            names[code] = code
    return names


def load_locale(code: str) -> dict:
    """Returns the translation dict for `code`, with any key missing from
    that file filled in from en.json. Raises UnknownLocaleError for a code
    with no matching file; callers should treat that as a 404 rather than
    silently serving English for a mistyped URL."""
    if not _LOCALE_CODE_RE.match(code) or code not in list_locales():
        raise UnknownLocaleError(f"No locale file for {code!r}.")

    strings = _read_locale_file(code)
    if code != DEFAULT_LOCALE:
        base = _read_locale_file(DEFAULT_LOCALE)
        merged = dict(base)
        merged.update(strings)
        strings = merged
    return strings
