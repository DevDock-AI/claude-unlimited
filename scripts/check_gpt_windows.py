#!/usr/bin/env python3
"""Pre-push freshness check for claude_unlimited/gpt_windows.py.

The capacity guard's GPT windows are a HARDCODED table (docs/adr/0009). This
diffs it against the Codex CLI's own cache of the ChatGPT backend's model
listing — ~/.codex/models_cache.json, written by any recent `codex` run — and
fails when a listed slug is missing from the table or carries a different
context_window / max_context_window.

Deliberately NOT a pytest: the suite must never read a user's files. Run it
by hand before pushing (see CONTRIBUTING.md), or wire it as a pre-push hook:

    printf '#!/bin/sh\\nexec python3 scripts/check_gpt_windows.py\\n' > .git/hooks/pre-push
    chmod +x .git/hooks/pre-push

Exit codes: 0 in sync (or no cache to compare against — said on stderr),
1 out of sync, 2 the cache could not be read.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from claude_unlimited import gpt_windows  # noqa: E402

CACHE = os.path.join(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")), "models_cache.json")


def main() -> int:
    if not os.path.exists(CACHE):
        print(f"check_gpt_windows: no {CACHE} — nothing to compare against "
              f"(run `codex` once to populate it)", file=sys.stderr)
        return 0
    try:
        with open(CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)
        models = cache.get("models") if isinstance(cache, dict) else None
        if not isinstance(models, list):
            raise ValueError("no `models` list")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"check_gpt_windows: could not read {CACHE}: {exc}", file=sys.stderr)
        return 2

    fetched = cache.get("fetched_at", "?") if isinstance(cache, dict) else "?"
    problems: list[str] = []
    seen = 0
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or "").strip().lower()
        if not slug or entry.get("visibility") != "list":
            continue  # hidden/internal slugs are not selectable targets
        seen += 1
        window = entry.get("context_window")
        ceiling = entry.get("max_context_window")
        row = gpt_windows.CODEX_BACKEND_WINDOWS.get(slug)
        if row is None:
            problems.append(f"  {slug}: missing from CODEX_BACKEND_WINDOWS "
                            f"(backend says {window} / {ceiling})")
        elif (window, ceiling) != row:
            problems.append(f"  {slug}: table has {row[0]} / {row[1]}, backend says {window} / {ceiling}")
        pct = entry.get("effective_context_window_percent")
        if isinstance(pct, (int, float)) and abs(pct / 100.0 - gpt_windows.CODEX_BACKEND_EFFECTIVE) > 1e-9:
            problems.append(f"  {slug}: effective_context_window_percent is {pct}, "
                            f"table assumes {gpt_windows.CODEX_BACKEND_EFFECTIVE * 100:g}")

    if problems:
        print(f"check_gpt_windows: claude_unlimited/gpt_windows.py is OUT OF SYNC with {CACHE} "
              f"(fetched {fetched}):")
        print("\n".join(problems))
        print("Update the table (and its source/date comments), then re-run. Changing a row is a "
              "spending decision — see docs/adr/0009.")
        return 1
    print(f"check_gpt_windows: {seen} listed slug(s) match the table (cache fetched {fetched}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
