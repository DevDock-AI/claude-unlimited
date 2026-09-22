"""The page paints its HTML text before the locale file arrives (and keeps it
if the locale fetch fails), so a plain-text fallback that says something
different from the English locale is a second, stale copy of the copy. Seven
had drifted — including two Help commands still describing an older build."""

import html
import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "claude_unlimited"


def test_every_plain_text_fallback_matches_the_english_locale():
    en = json.loads((STATIC / "locales" / "en.json").read_text(encoding="utf-8"))
    page = (STATIC / "static" / "index.html").read_text(encoding="utf-8")
    drifted = []
    for m in re.finditer(r'<([a-z0-9]+)[^>]*data-i18n="([^"]+)"[^>]*>([^<]*)</\1>', page):
        key, text = m.group(2), html.unescape(m.group(3)).strip()
        assert key in en, f"{key} is used by the page but missing from en.json"
        if text and text != en[key].strip():
            drifted.append(key)
    assert drifted == []
