"""ECO — Efficient Context Optimization.

Rewrites **the text a tool printed** — and nothing else — into a shorter form
before the request leaves the daemon, so the same work costs fewer input
tokens. Claude Code re-sends the whole history every turn, so a tool result
compacted once is re-compacted identically on every later turn; that is where
the saving compounds, and it is why determinism matters more here than
compression ratio.

Pure, like `router.py`: no I/O, no clock, no mutable module state.

The five invariants (each has a property test in tests/test_eco.py):

1. **Deterministic** — `f(x) == f(x)`, in this process and a fresh one.
   Load-bearing: Anthropic caches a PREFIX, so non-deterministic output is a
   cache miss on every turn, costing far more than ECO saves. Nothing here may
   iterate a set, depend on `hash()`, or read a clock.
2. **Idempotent** — `f(f(x)) == f(x)`. Failover re-compacts from the original
   body, but the invariant must hold regardless.
3. **Never grows, never empties** — if the result is empty or not shorter than
   the input, the input is returned unchanged.
4. **Never touches errors** — `is_error` blocks are left alone. An error trace
   is exactly what the model needs in full.
5. **Fails open** — any exception inside a filter returns the raw input. A bug
   in ECO must never fail a request.

Provenance: the filter behaviours and detection heuristics are *inspired by*
decolua/9router's RTK (MIT). This is an independent implementation; no code was
copied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

# Below this, compaction cannot save enough to be worth the risk of changing
# what the model sees.
MIN_COMPACT_SIZE = 500
# Above this, the body is pathological; leave it alone rather than spend time
# rewriting megabytes.
RAW_CAP = 10 * 1024 * 1024
# Detection reads only the head of the text — a sniff, not a scan.
DETECT_WINDOW = 1024

# Every truncation leaves a marker that reads as ours. Never the reference's
# `[full diff: rtk …]`, which names a CLI the model cannot run.
MARKER = "ECO"

MAX_LINES = 2000


@dataclass(frozen=True)
class CompactionStats:
    """What a pass saved. Bytes are exact; tokens are calibrated elsewhere
    against what the provider actually billed."""

    bytes_before: int = 0
    bytes_after: int = 0
    hits: tuple[tuple[str, int], ...] = ()

    @property
    def bytes_saved(self) -> int:
        return max(self.bytes_before - self.bytes_after, 0)

    @property
    def touched(self) -> bool:
        return bool(self.hits)


# ---- filters ----------------------------------------------------------------
# Every filter is a pure `str -> str`. A filter never raises for the caller:
# apply_filter catches, and never_worse() enforces invariant 3.


_BLANK_RUN = re.compile(r"\n{3,}")


# Irregular plurals we actually emit. Naive "+s" produced "20 entrys" in
# shipped output — this text is read by a model and by the user.
_PLURALS = {"entry": "entries", "directory": "directories", "match": "matches"}


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {_PLURALS.get(word, word + 's')}"


_DATA_ISH = re.compile(r"^[\s]*[\[\]{}(),\"\']")


def _looks_like_data_or_code(text: str) -> bool:
    """Is this structured content rather than a log?

    Repeated lines in a log are noise; repeated lines in a JSON array or a
    source file are VALUES. Collapsing them makes the model reason about a
    4-element array that really has 63, and build edits against text the file
    does not contain.
    """
    lines = [ln for ln in text.split("\n")[:80] if ln.strip()]
    if not lines:
        return False
    return sum(bool(_DATA_ISH.match(ln)) for ln in lines) >= max(len(lines) // 3, 3)


def dedup_log(text: str) -> str:
    """Collapse runs of identical lines and blank streaks; cap total lines.

    The universal fallback: it assumes nothing about the tool, only that
    repeated identical lines carry no information past the first.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        run = 1
        # Only collapse runs of NON-EMPTY identical lines; blank runs are
        # handled separately so "\n\n\n" does not become a duplicate marker.
        if line.strip():
            while i + run < len(lines) and lines[i + run] == line:
                run += 1
        if run >= 2:
            out.append(line)
            out.append(f"… ({_plural(run - 1, 'duplicate line')}, {MARKER})")
        else:
            out.append(line)
        i += run

    joined = "\n".join(out)
    capped = joined.split("\n")
    if len(capped) > MAX_LINES:
        dropped = len(capped) - MAX_LINES
        capped = capped[:MAX_LINES] + [f"… +{_plural(dropped, 'line')} truncated ({MARKER})"]
        joined = "\n".join(capped)
    return joined


def tree(text: str) -> str:
    """`tree` output: drop the trailing count footer, cap the listing.

    The footer ("12 directories, 48 files") restates what the lines above
    already show, and a deep tree is mostly noise past the first screens.
    """
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and re.match(r"^\s*\d+ director(y|ies), \d+ files?\s*$", lines[-1]):
        lines.pop()
        # Strip again: the footer is usually preceded by a blank line, and
        # leaving it made tree's own output shrink by one byte on a second
        # pass — not a fixed point, so the fixed-point guard refused to
        # compact tree at all.
        while lines and not lines[-1].strip():
            lines.pop()
    if len(lines) > 200:
        dropped = len(lines) - 200
        lines = lines[:200] + [f"… +{_plural(dropped, 'line')} truncated ({MARKER})"]
    return "\n".join(lines)


# At least one of the two columns must be a real status code, and the
# path follows a SINGLE space. "    module0.py" is not a status line.
_PORCELAIN = re.compile(r"^(?=[ MADRCU?!]{2})(?!  )[ MADRCU?!]{2} (?! )")


def git_status(text: str) -> str:
    """Counts plus a bounded sample of paths, instead of every path.

    The model needs to know WHAT changed and roughly how much; a 400-path
    listing costs far more than that knowledge is worth.
    """
    lines = [ln for ln in text.split("\n") if ln.strip()]
    branch = next((ln for ln in lines if ln.startswith("On branch")), None)
    groups: dict[str, list[str]] = {"Staged": [], "Modified": [], "Untracked": []}
    for ln in lines:
        if not _PORCELAIN.match(ln):
            continue
        index, worktree, path = ln[0], ln[1], ln[3:].strip()
        if index == "?" and worktree == "?":
            groups["Untracked"].append(path)
        elif index != " ":
            groups["Staged"].append(path)
        else:
            groups["Modified"].append(path)

    out: list[str] = []
    if branch:
        out.append(f"* {branch[len('On branch '):].strip()}")
    marks = {"Staged": "+", "Modified": "~", "Untracked": "?"}
    for name, paths in groups.items():
        if not paths:
            continue
        out.append(f"{marks[name]} {name}: {len(paths)}")
        for path in paths[:10]:
            out.append(f"    {path}")
        if len(paths) > 10:
            out.append(f"    … +{len(paths) - 10} more")
    if not any(groups.values()):
        # Long-format `git status` ("\tmodified:   x") matches no porcelain
        # line. Summarising it to the branch line alone reports a clean tree
        # that isn't — fabrication, not compaction.
        return text
    return "\n".join(out) if out else text


_COMMIT = re.compile(r"^commit [0-9a-f]{7,40}")


def git_log(text: str) -> str:
    """Keep each commit's header and subject; drop any embedded diff body."""
    out: list[str] = []
    in_diff = False
    omitted = False
    for line in text.split("\n"):
        if _COMMIT.match(line):
            in_diff = False
            omitted = False
            out.append(line)
            continue
        if line.startswith("diff --git") or line.startswith("@@ "):
            if not omitted:
                out.append(f"… diff body omitted ({MARKER})")
                omitted = True
            in_diff = True
            continue
        if in_diff:
            continue
        out.append(line)
    if len(out) > 200:
        dropped = len(out) - 200
        out = out[:200] + [f"… +{_plural(dropped, 'line')} truncated ({MARKER})"]
    return "\n".join(out)


def find_paths(text: str) -> str:
    """Group a flat path list by directory with bounded samples."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for path in lines:
        head, _, tail = path.rpartition("/")
        head = head or "."
        if head not in groups:
            groups[head] = []
            order.append(head)          # insertion order, never set order
        groups[head].append(tail or path)

    out: list[str] = []
    for directory in order[:20]:
        names = groups[directory]
        out.append(f"{directory}/  ({_plural(len(names), 'entry')})")
        for name in names[:10]:
            out.append(f"    {name}")
        if len(names) > 10:
            out.append(f"    … +{len(names) - 10} more")
    if len(order) > 20:
        out.append(f"… +{len(order) - 20} more directories ({MARKER})")
    return "\n".join(out)


# The path group must LOOK like a path: no spaces, and either a directory
# separator or a file extension. Without this, a clock prefix ("12:30:")
# parsed as path=12 line=30 and every log became "grep output".
_GREP_LINE = re.compile(r"^(?P<path>(?=[^:\n]*[/.])[^:\s]+):(?P<line>\d+):")


def grep_matches(text: str) -> str:
    """Group `path:line:content` hits by file, bounded per file."""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    passthrough: list[str] = []
    for line in text.split("\n"):
        m = _GREP_LINE.match(line)
        if not m:
            if line.strip():
                passthrough.append(line)
            continue
        path = m.group("path")
        if path not in groups:
            groups[path] = []
            order.append(path)
        groups[path].append(line[m.end("path") + 1:])

    out: list[str] = []
    for path in order:
        hits = groups[path]
        out.append(f"{path}  ({_plural(len(hits), 'match')})")
        for hit in hits[:10]:
            out.append(f"    {hit}")
        if len(hits) > 10:
            out.append(f"    … +{len(hits) - 10} more")
    out.extend(passthrough[:5])
    return "\n".join(out)


_LS_ROW = re.compile(r"^[dl-][rwxsStT-]{9}[+@]?\s")
# Directories a listing is usually not ABOUT. Unlike the reference we never
# drop them silently — a model that cannot see `.git` may conclude the path is
# not a repository.
_NOISY_DIRS = ("node_modules", ".git", "venv", ".venv", "__pycache__", "target", "dist", "build")


def ls_listing(text: str) -> str:
    """Drop permissions/owner/date columns; summarise by extension."""
    lines = text.split("\n")
    names: list[str] = []
    hidden = 0
    for line in lines:
        if line.startswith("total ") or not line.strip():
            continue
        if _LS_ROW.match(line):
            parts = line.split(None, 8)
            name = parts[8] if len(parts) > 8 else parts[-1]
        else:
            name = line.strip()
        if name in _NOISY_DIRS:
            hidden += 1
            continue
        names.append(name)

    counts: dict[str, int] = {}
    for name in names:
        ext = name.rpartition(".")[2] if "." in name[1:] else "(no ext)"
        counts[ext] = counts.get(ext, 0) + 1

    out = list(names[:200])
    if len(names) > 200:
        out.append(f"… +{len(names) - 200} more entries ({MARKER})")
    if hidden:
        # Deliberate deviation from the reference, which omits these silently.
        out.append(f"… +{hidden} hidden (build dirs) ({MARKER})")
    if counts:
        # sorted(): determinism. Never dict/set iteration order.
        histogram = ", ".join(f"{ext} {n}" for ext, n in sorted(counts.items()))
        out.append(f"— {histogram}")
    return "\n".join(out)


_BUILD_MARKERS = ("npm ", "yarn ", "cargo ", "mvn ", "pip ", "Compiling ", "webpack", "gradle")
_ERROR_LINE = re.compile(r"\b(error|ERROR|FAILED|failed)\b")
_WARN_LINE = re.compile(r"\b(warning|WARN)\b", re.IGNORECASE)
_DEPRECATION = re.compile(r"\bdeprecat", re.IGNORECASE)


def build_output(text: str) -> str:
    """Keep every error; sample warnings; collapse progress — IN ORDER.

    Two rules learned the hard way: hoisting errors to the top reordered the
    file a model was reading, and dropping the remainder silently let it plan
    against text that was no longer there. Order is preserved and every
    omission is counted.
    """
    warnings_kept = deprecations_kept = 0
    compiling = 0
    omitted = 0
    out: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        if _ERROR_LINE.search(line):
            out.append(line)                      # every error, always, in place
        elif _DEPRECATION.search(line):
            if deprecations_kept < 3:
                out.append(line)
                deprecations_kept += 1
            else:
                omitted += 1
        elif _WARN_LINE.search(line):
            if warnings_kept < 5:
                out.append(line)
                warnings_kept += 1
            else:
                omitted += 1
        elif line.lstrip().startswith("Compiling "):
            compiling += 1
        else:
            out.append(line)
    if compiling:
        out.append(f"Compiled {_plural(compiling, 'package')} ({MARKER})")
    if omitted:
        out.append(f"… +{_plural(omitted, 'line')} omitted ({MARKER})")
    return "\n".join(out)


_DIFF_HEADER = re.compile(r"^diff --git ")
_HUNK = re.compile(r"^@@ ")


def git_diff(text: str) -> str:
    """One summary line per file plus bounded hunks.

    AGGRESSIVE because it discards diff body the model cannot reconstruct.
    Every file is still named with its +/- totals, so nothing disappears
    without a trace.
    """
    files: list[tuple[str, int, int, list[str]]] = []
    current: Optional[list] = None
    hunk_lines = 0
    in_hunk = False
    for line in text.split("\n"):
        if _DIFF_HEADER.match(line):
            if current:
                files.append(tuple(current))          # type: ignore[arg-type]
            current = [line[len("diff --git "):].strip(), 0, 0, [], 0]
            hunk_lines = 0
            in_hunk = False
            continue
        if current is None:
            continue
        if _HUNK.match(line):
            hunk_lines = 0
            in_hunk = True
            current[3].append(line)
            continue
        if line.startswith("+") and not (not in_hunk and line.startswith("+++ ")):
            current[1] += 1
        elif line.startswith("-") and not (not in_hunk and line.startswith("--- ")):
            current[2] += 1
        hunk_lines += 1
        if hunk_lines <= 100:
            current[3].append(line)
        else:
            current[4] += 1          # counted here, or the tally under-reports
    if current:
        files.append(tuple(current))                  # type: ignore[arg-type]

    out: list[str] = []
    for name, added, removed, body, dropped in files:
        out.append(f"{name}  +{added} -{removed}")
        # No second slice here: `dropped` already counts everything the
        # per-hunk bound withheld, and re-slicing dropped uncounted lines.
        out.extend(body)
        if dropped:
            out.append(f"… +{_plural(dropped, 'line')} of hunk omitted ({MARKER})")
    return "\n".join(out) if out else text


SMART_TRUNCATE_MIN_LINES = 250


def smart_truncate(text: str) -> str:
    """Last resort: keep the head and the tail, name what went.

    The head carries what the command was doing and the tail carries how it
    ended; the middle of a 5,000-line dump is where the least information per
    byte lives.
    """
    lines = text.split("\n")
    if len(lines) < SMART_TRUNCATE_MIN_LINES:
        return text
    head, tail = lines[:120], lines[-60:]
    dropped = len(lines) - len(head) - len(tail)
    if dropped <= 0:
        return text
    return "\n".join(head + [f"… +{_plural(dropped, 'line')} truncated ({MARKER})"] + tail)


FILTERS: dict[str, Callable[[str], str]] = {
    "git-diff": git_diff,
    "smart-truncate": smart_truncate,
    "build-output": build_output,
    "tree": tree,
    "git-status": git_status,
    "git-log": git_log,
    "find": find_paths,
    "grep": grep_matches,
    "ls": ls_listing,
    "dedup-log": dedup_log,
}

# Ordered most-specific first: detect() returns the first match, and the
# universal fallback must be considered last.
_DETECT_ORDER: tuple[str, ...] = (
    "git-diff", "git-status", "git-log", "tree", "grep", "ls", "build-output", "find",
    "dedup-log",      # universal fallback for LIGHT
    "smart-truncate", # AGGRESSIVE-only last resort, after everything else
)

# LIGHT only collapses what is provably redundant and always leaves a count.
# Anything that discards detail the model cannot reconstruct — a tree's tail,
# `ls -l`'s size/date columns, a `git show` diff body — lives in AGGRESSIVE,
# whose UI copy says exactly that.
_LIGHT = ("dedup-log", "build-output", "git-status", "grep")
_AGGRESSIVE_ONLY = ("tree", "ls", "find", "git-log", "git-diff", "smart-truncate")

TIERS: dict[str, tuple[str, ...]] = {
    "off": (),
    "light": _LIGHT,
    "aggressive": _LIGHT + _AGGRESSIVE_ONLY,
}


def already_compacted(text: str) -> bool:
    """Did ECO already rewrite this?

    Filters indent and re-shape their output, which can make it look like a
    DIFFERENT tool's output to the next pass — compacted `find` output read as
    git porcelain and was rewritten into a fabricated "Modified: 44" line.
    Marking our own output and refusing to touch it again makes idempotence
    structural instead of something each detector has to get right.
    """
    return f"({MARKER})" in text or f", {MARKER})" in text


def detect(text: str) -> Optional[str]:
    """The best single candidate, kept for callers that want just a name."""
    return next(iter(detect_all(text)), None)


def detect_all(text: str) -> tuple[str, ...]:
    """Which filter fits this text, by sniffing CONTENT rather than tool name —
    the same shape arrives from Bash, Grep, Read and MCP tools alike.

    Returns every filter whose shape matches, most specific first. More than
    one can match — `dedup-log` accepts almost any multi-line text — and the
    caller tries them in order, because a filter that matches is not the same
    as a filter that helps.
    """
    if not text or already_compacted(text):
        return ()
    found: list[str] = []
    window = text[:DETECT_WINDOW]
    for name in _DETECT_ORDER:
        if name == "git-diff":
            if _DIFF_HEADER.match(window) or _HUNK.match(window):
                found.append(name)
        elif name == "git-status":
            if window.startswith("On branch") or _porcelain_ratio(window) >= 0.6:
                found.append(name)
        elif name == "git-log":
            if _COMMIT.match(window):
                found.append(name)
        elif name == "tree":
            if any(glyph in window for glyph in ("├──", "└──", "│")):
                found.append(name)
        elif name == "grep":
            head = [ln for ln in window.split("\n")[:5] if ln.strip()]
            if head and sum(bool(_GREP_LINE.match(ln)) for ln in head) >= max(len(head) // 2, 1):
                found.append(name)
        elif name == "ls":
            rows = [ln for ln in window.split("\n") if ln.strip()]
            # A bare "total N" header is not enough: "total 5 tests failed."
            # is prose. Require real permission rows.
            if rows and sum(bool(_LS_ROW.match(ln)) for ln in rows) >= max(len(rows) // 2, 2):
                found.append(name)
        elif name == "build-output":
            rows = [ln for ln in window.split("\n") if ln.strip()]
            structural = sum(bool(_ERROR_LINE.search(ln) or _WARN_LINE.search(ln)
                                  or ln.lstrip().startswith("Compiling "))
                             for ln in rows)
            # A README that merely says "pip install x" is not a build log.
            if structural >= 3 and any(m in window for m in _BUILD_MARKERS):
                found.append(name)
        elif name == "find":
            lines = [ln for ln in window.split("\n") if ln.strip()]
            if len(lines) >= 3 and all("/" in ln and ":" not in ln for ln in lines):
                found.append(name)
        elif name == "dedup-log":
            # The fallback: enough lines that duplicate collapsing can pay off,
            # and not structured data whose repeated lines are values.
            if (len([ln for ln in window.split("\n") if ln.strip()]) >= 5
                    and not _looks_like_data_or_code(text)):
                found.append(name)
        elif name == "smart-truncate":
            # Counts the WHOLE text, not the sniff window: this is about how
            # long the output is, which the first 1024 chars cannot tell us.
            if len(text.split("\n")) >= SMART_TRUNCATE_MIN_LINES:
                found.append(name)
    return tuple(found)


def _porcelain_ratio(window: str) -> float:
    lines = [ln for ln in window.split("\n") if ln.strip()]
    if not lines:
        return 0.0
    return sum(bool(_PORCELAIN.match(ln)) for ln in lines) / len(lines)


def never_worse(original: str, candidate: str) -> str:
    """Invariant 3. A filter that empties its input or fails to shrink it has
    not helped, and silently losing output is worse than spending the tokens."""
    if not candidate:
        return original
    if len(candidate) >= len(original):
        return original
    return candidate


def apply_filter(name: str, text: str) -> str:
    """Invariants 3 and 5: never worse, and never raises."""
    fn = FILTERS.get(name)
    if fn is None:
        return text
    try:
        return never_worse(text, fn(text))
    except Exception:
        # Fails open. A bug in a filter must never fail the user's request.
        return text


def compact_text(text: str, tier: str) -> tuple[str, Optional[str]]:
    """Compact one tool-result string. Returns (text, filter_name_or_None)."""
    allowed = TIERS.get(tier, ())
    if not allowed or not text:
        return text, None
    size = len(text)
    if size < MIN_COMPACT_SIZE or size > RAW_CAP:
        return text, None
    for name in detect_all(text):
        if name not in allowed:
            continue
        out = apply_filter(name, text)
        if out != text:
            break
    else:
        return text, None
    # A second pass must be a no-op across EVERY candidate in this tier, not
    # just the first: build-output's result was still reachable by
    # smart-truncate, so f(f(x)) != f(x) under aggressive.
    for follow_up in detect_all(out):
        if follow_up in allowed and apply_filter(follow_up, out) != out:
            return text, None
    return out, name


# ---- request walking --------------------------------------------------------


def _compact_block(block: dict, tier: str, hits: dict[str, int]) -> dict:
    """One `tool_result` block. Returns a NEW block when it changed."""
    # Invariant 4: an error trace is exactly what the model needs in full.
    if block.get("is_error"):
        return block

    content = block.get("content")

    if isinstance(content, str):
        out, name = compact_text(content, tier)
        if name is None:
            return block
        hits[name] = hits.get(name, 0) + (len(content) - len(out))
        new = dict(block)
        new["content"] = out
        return new

    if isinstance(content, list):
        changed = False
        parts: list = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                out, name = compact_text(part["text"], tier)
                if name is not None:
                    hits[name] = hits.get(name, 0) + (len(part["text"]) - len(out))
                    part = {**part, "text": out}
                    changed = True
            parts.append(part)
        if not changed:
            return block
        new = dict(block)
        new["content"] = parts
        return new

    return block


def compact_request(body: dict, tier: str) -> tuple[dict, CompactionStats]:
    """Returns a NEW body with tool-result text compacted, plus what it saved.

    Never mutates the input: the original must survive intact because a
    failover re-sends it to a different account, and re-compacting from
    already-compacted text is how markers stack up.
    """
    if not isinstance(body, dict) or tier not in TIERS or tier == "off":
        return body, CompactionStats()

    messages = body.get("messages")
    if not isinstance(messages, list):
        return body, CompactionStats()

    hits: dict[str, int] = {}
    before = 0
    after = 0
    new_messages: list = []
    changed = False

    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            new_messages.append(message)
            continue
        blocks: list = []
        message_changed = False
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                original_len = _block_text_len(block)
                new_block = _compact_block(block, tier, hits)
                if new_block is not block:
                    message_changed = True
                    before += original_len
                    after += _block_text_len(new_block)
                block = new_block
            blocks.append(block)
        if message_changed:
            changed = True
            message = {**message, "content": blocks}
        new_messages.append(message)

    if not changed:
        return body, CompactionStats()

    # sorted(): the hit list must not depend on dict iteration order for the
    # same input — invariant 1 covers what we STORE as well as what we send.
    stats = CompactionStats(
        bytes_before=before,
        bytes_after=after,
        hits=tuple(sorted(hits.items())),
    )
    return {**body, "messages": new_messages}, stats


def _block_text_len(block: dict) -> int:
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(p.get("text", "")) for p in content
                   if isinstance(p, dict) and p.get("type") == "text")
    return 0
