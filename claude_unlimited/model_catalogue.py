"""Model-name catalogue sourced from LiteLLM, with offline fallbacks.

The lineups in openai_models.py were hand-maintained literals, and both the
listing and the parity table went stale silently whenever a provider shipped
or retired a model (docs/tickets/007). This module makes the *names* come
from a real source of truth: BerriAI/litellm's
model_prices_and_context_window.json, fetched through GitHub's public API —
the only non-provider host this project is allowed to talk to (AGENTS.md,
SECURITY.md). raw.githubusercontent.com is a different host and would make
that published claim false, so the fetch goes contents-endpoint (for the
blob sha; the file is >1 MB so `content` comes back empty) then the git
blob endpoint, both on api.github.com.

Fallback chain, best first, every step offline-safe:

  live fetch  ->  disk cache (last good fetch, in the app dir)
              ->  vendored data/models.json (trimmed snapshot in the package)
              ->  openai_models.py's literals (when current() is None)

Nothing here ever blocks daemon startup or raises out of a refresh: a
failed, slow or rate-limited fetch is a silent no-op that keeps whatever
catalogue was already loaded. Rate-limit responses back off hard — this
project has been fingerprint-bucketed by a provider before, and a background
loop that retries into a 429 is how that starts.

Tests must never fetch: load()/refresh() take an injected `source` callable
and the suite only ever feeds them fixture dicts.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from . import config

LITELLM_PATH = "model_prices_and_context_window.json"
CONTENTS_URL = f"https://api.github.com/repos/BerriAI/litellm/contents/{LITELLM_PATH}"
BLOB_URL = "https://api.github.com/repos/BerriAI/litellm/git/blobs/{sha}"

NETWORK_TIMEOUT_SECONDS = 30
# Hourly BASELINE: each due tick is only the cheap contents-endpoint sha
# check (~1 KB); the 2.3 MB blob is downloaded only when the sha actually
# changed. Providers ship models mid-day, and a daemon can run for weeks.
REFRESH_INTERVAL_SECONDS = 60 * 60
# Session-launch triggers (refresh_if_allowed) coalesce: no matter how many
# `claude-unlimited code` sessions start, at most one sha check per this
# window. Unauthenticated api.github.com allows 60 requests/hour total.
SHA_CHECK_MIN_INTERVAL_SECONDS = 5 * 60
# An ordinary failure waits a full interval before the next try; a
# rate-limit response waits a day. Never hammer a public API from a loop.
FAILURE_BACKOFF_SECONDS = REFRESH_INTERVAL_SECONDS
RATE_LIMIT_BACKOFF_SECONDS = 24 * 60 * 60

_VENDORED_FILE = Path(__file__).resolve().parent / "data" / "models.json"
_CACHE_BASENAME = "model_catalogue_cache.json"

# Only the fields the parser reads survive into the cache and the vendored
# snapshot, so both stay a few KB instead of LiteLLM's 2 MB.
_KEPT_FIELDS = (
    "litellm_provider", "mode", "max_input_tokens",
    "input_cost_per_token", "output_cost_per_token",
    "cache_creation_input_token_cost", "cache_creation_input_token_cost_above_1hr",
    "cache_read_input_token_cost",
    "supports_reasoning", "deprecation_date",
)

# OpenAI ships dozens of task-specific variants of each model under the same
# provider; none of them is something this bridge would route chat traffic
# to, and each one is a row in a Dashboard dropdown.
_OPENAI_NOISE = ("audio", "realtime", "search", "transcribe", "tts",
                 "image", "deep-research", "container", "chatgpt",
                 "instruct", "preview", "-chat")


class CatalogueError(Exception):
    pass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str
    supports_reasoning: bool
    input_cost: Optional[float]   # USD per input token, as LiteLLM states it
    output_cost: Optional[float]  # USD per output token
    rank: int                     # 0 = most capable in its lineup
    # Prompt-cache rates, USD per token (pricing.py turns these into a
    # ModelPrice). Trailing with defaults so older ModelInfo(...) call sites
    # keep working; None means LiteLLM didn't state the rate.
    cache_write_cost: Optional[float] = None      # 5-minute-TTL cache write
    cache_write_1h_cost: Optional[float] = None   # 1-hour-TTL cache write
    cache_read_cost: Optional[float] = None


@dataclass(frozen=True)
class Catalogue:
    anthropic: tuple  # tuple[ModelInfo, ...], most-capable-first
    openai: tuple     # tuple[ModelInfo, ...], most-capable-first


_DATE_SUFFIX = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")


def base_id(model_id: str) -> str:
    """A model id with any trailing date stamp removed.

    Real ids come both ways (`claude-haiku-4-5` and
    `claude-haiku-4-5-20251001` are the same model), and prefix/base
    matching is what keeps a dated id resolving — the same trap ticket 007
    calls out for pricing lookups."""
    return _DATE_SUFFIX.sub("", model_id)


def _strip_namespace(key: str) -> str:
    """LiteLLM sometimes namespaces keys by provider or variant
    (`anthropic.claude-...`, `azure/gpt-...`, `low/1024-x-1024/...`)."""
    key = key.rsplit("/", 1)[-1]
    if key.startswith("anthropic."):
        key = key[len("anthropic."):]
    return key


def _version_of(model_id: str) -> float:
    """Best-effort family version for ranking ties: claude-opus-4-8 -> 4.8,
    gpt-5.6-terra -> 5.6, o3-mini -> 3."""
    stripped = base_id(model_id)
    match = re.search(r"-(\d+(?:\.\d+)?)(?:-(\d+))?", stripped)
    if match:
        major = match.group(1)
        minor = match.group(2)
        if minor is not None and "." not in major:
            try:
                return float(f"{major}.{minor}")
            except ValueError:
                pass
        try:
            return float(major)
        except ValueError:
            pass
    match = re.match(r"^o(\d+)", stripped)
    if match:
        return float(match.group(1))
    return 0.0


def _is_past_deprecation(entry: dict, today: Optional[date]) -> bool:
    if today is None:
        return False
    raw = entry.get("deprecation_date")
    if not isinstance(raw, str):
        return False
    try:
        return date.fromisoformat(raw) < today
    except ValueError:
        return False


def _wanted(key: str, entry: dict, today: Optional[date]) -> Optional[str]:
    """The provider lineup this entry belongs to, or None to drop it."""
    if not isinstance(entry, dict):
        return None
    provider = entry.get("litellm_provider")
    mode = entry.get("mode")
    model_id = _strip_namespace(key)
    if _is_past_deprecation(entry, today):
        return None
    if provider == "anthropic" and mode == "chat" and model_id.startswith("claude-"):
        return "anthropic"
    if provider == "openai" and mode in ("chat", "responses"):
        if ":" in model_id:  # fine-tune ids like ft:gpt-4o
            return None
        codexish = "codex" in model_id
        is_gpt = model_id.startswith("gpt-")
        if not (is_gpt or codexish):
            # o-series (o1/o3/o4) are reasoning models, not Codex routing
            # targets — a Codex/ChatGPT account serves the GPT generation, so
            # they only clutter the parity dropdown.
            return None
        if any(noise in model_id for noise in _OPENAI_NOISE):
            return None
        # Drop pre-5 GPT generations (gpt-4*, gpt-4o*, gpt-4.1*, gpt-3.5*, and
        # unversioned experiments like gpt-daybreak): legacy/non-Codex, and
        # keeping them let a $600/M outlier or an old model outrank the current
        # flagship. Anything *codex* is kept regardless of version parse.
        if is_gpt and not codexish and _version_of(model_id) < 5:
            return None
        return "openai"
    return None


def trim_raw(raw: dict, today: Optional[date] = None) -> dict:
    """The subset of a raw LiteLLM dict this module cares about, with only
    the fields the parser reads. This is what the disk cache stores and what
    the vendored snapshot is generated from."""
    if not isinstance(raw, dict):
        raise CatalogueError("catalogue source is not a JSON object")
    out: dict = {}
    for key, entry in raw.items():
        if _wanted(key, entry, today) is None:
            continue
        out[key] = {f: entry.get(f) for f in _KEPT_FIELDS if entry.get(f) is not None}
    return out


def display_name_for(model_id: str) -> str:
    """`claude-haiku-4-5-20251001` -> `Claude Haiku 4.5`,
    `gpt-5.6-terra` -> `GPT-5.6 Terra`. Curated names in openai_models.py
    still win where they exist; this covers models shipped after this
    build."""
    tokens = base_id(model_id).split("-")
    merged: list[str] = []
    for token in tokens:
        if merged and re.fullmatch(r"\d+", token) and re.fullmatch(r"\d+", merged[-1]):
            merged[-1] = f"{merged[-1]}.{token}"
        else:
            merged.append(token)
    words = []
    for token in merged:
        if token.lower() == "gpt":
            words.append("GPT")
        elif re.fullmatch(r"o\d+", token):
            words.append(token.upper())
        elif re.fullmatch(r"[\d.]+", token):
            words.append(token)
        else:
            words.append(token.capitalize())
    if len(words) >= 2 and words[0] == "GPT":
        return f"GPT-{words[1]}" + ("" if len(words) == 2 else " " + " ".join(words[2:]))
    return " ".join(words)


def _sort_key(model_id: str, entry: dict, lineup: str = "openai"):
    cost = entry.get("output_cost_per_token")
    cost = cost if isinstance(cost, (int, float)) else 0.0
    version = _version_of(model_id)
    if lineup == "openai":
        # Generation FIRST for OpenAI: Codex capability tracks the GPT version,
        # not the per-token price. Cost-first put a fringe expensive reasoning
        # model (o1-pro, $600/M out) at rank 0 and buried the actual flagship
        # (gpt-6-astra) — wrong tier for the mapping + a polluted dropdown.
        return (-version, -cost, model_id)
    # Anthropic: price tracks capability well and there is no fringe outlier,
    # so cost-first keeps the flagship (fable) on top; version + id break ties.
    return (-cost, -version, model_id)


def parse(raw: dict, today: Optional[date] = None) -> Catalogue:
    """Pure: raw LiteLLM dict (full or trimmed) -> ordered lineups.

    Raises CatalogueError on anything structurally wrong; load()/refresh()
    turn that into "keep the previous catalogue"."""
    trimmed = trim_raw(raw, today)
    lineups: dict[str, dict[str, dict]] = {"anthropic": {}, "openai": {}}
    for key, entry in trimmed.items():
        lineup = _wanted(key, entry, today)
        if lineup is None:
            continue
        model_id = _strip_namespace(key)
        # A dated and an undated key for the same model are one model; keep
        # the undated spelling when both exist.
        base = base_id(model_id)
        existing = next((mid for mid in lineups[lineup] if base_id(mid) == base), None)
        if existing is not None:
            if len(model_id) < len(existing):
                del lineups[lineup][existing]
            else:
                continue
        lineups[lineup][model_id] = entry

    def build(lineup: str) -> tuple:
        ordered = sorted(lineups[lineup].items(), key=lambda kv: _sort_key(kv[0], kv[1], lineup))
        return tuple(
            ModelInfo(
                id=mid,
                display_name=display_name_for(mid),
                supports_reasoning=bool(entry.get("supports_reasoning")),
                input_cost=entry.get("input_cost_per_token"),
                output_cost=entry.get("output_cost_per_token"),
                rank=i,
                cache_write_cost=entry.get("cache_creation_input_token_cost"),
                cache_write_1h_cost=entry.get("cache_creation_input_token_cost_above_1hr"),
                cache_read_cost=entry.get("cache_read_input_token_cost"),
            )
            for i, (mid, entry) in enumerate(ordered)
        )

    catalogue = Catalogue(anthropic=build("anthropic"), openai=build("openai"))
    _validate(catalogue)
    return catalogue


# Loose on purpose: a rename upstream should not brick refresh, but a file
# that no longer contains anything recognizable as either lineup must be
# rejected, or a schema change upstream silently empties the model picker.
_ANTHROPIC_ANCHOR_PREFIXES = ("claude-opus", "claude-sonnet", "claude-haiku", "claude-fable")
_OPENAI_ANCHOR_PREFIX = "gpt-"


def _validate(catalogue: Catalogue) -> None:
    if not catalogue.anthropic or not catalogue.openai:
        raise CatalogueError("a lineup came back empty")
    anth_ids = [m.id for m in catalogue.anthropic]
    if not any(mid.startswith(p) for mid in anth_ids for p in _ANTHROPIC_ANCHOR_PREFIXES):
        raise CatalogueError("no recognizable Claude anchor model")
    if not any(m.id.startswith(_OPENAI_ANCHOR_PREFIX) for m in catalogue.openai):
        raise CatalogueError("no recognizable OpenAI anchor model")


# ---------------------------------------------------------------------------
# Fetching (api.github.com only — see module docstring)


class _RateLimited(Exception):
    pass


def _github_json(url: str, opener: Callable) -> dict:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "claude-unlimited-model-catalogue",
    })
    try:
        with opener(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            if not response.geturl().startswith("https://"):
                raise CatalogueError("non-HTTPS redirect")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # GitHub rate limits answer 403 (with X-RateLimit-Remaining: 0) or
        # 429. Both mean the same thing here: stop asking for a long time.
        if exc.code in (403, 429):
            raise _RateLimited(f"GitHub returned HTTP {exc.code}") from exc
        raise CatalogueError(f"GitHub returned HTTP {exc.code}") from exc


class GitHubSource:
    """The real two-step fetch via api.github.com.

    sha() is the cheap check: the contents endpoint returns ~1 KB of
    metadata whose blob sha changes iff the file changed (the file is
    >1 MB, so its `content` field comes back empty anyway). fetch() is the
    expensive step — the 2.3 MB git blob — and refresh() only calls it when
    the sha differs from the one the cache remembers. Both endpoints are on
    api.github.com, keeping the published only-GitHub's-API claim true."""

    def __init__(self, opener: Callable = urllib.request.urlopen):
        self._opener = opener

    def sha(self) -> str:
        meta = _github_json(CONTENTS_URL, self._opener)
        sha = (meta.get("sha") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise CatalogueError("contents endpoint returned no usable blob sha")
        return sha

    def fetch(self, sha: str) -> dict:
        blob = _github_json(BLOB_URL.format(sha=sha), self._opener)
        content = blob.get("content")
        if not content:
            raise CatalogueError("blob endpoint returned no content")
        return json.loads(base64.b64decode(content))


class _CallableSource:
    """Adapter for a plain injected callable (tests, load()): it has no
    cheap sha probe, so sha() answers None and every refresh downloads."""

    def __init__(self, fn: Callable[[], dict]):
        self._fn = fn

    def sha(self) -> Optional[str]:
        return None

    def fetch(self, sha: Optional[str]) -> dict:
        return self._fn()


def _as_source(source):
    if source is None:
        return GitHubSource()
    if hasattr(source, "sha") and hasattr(source, "fetch"):
        return source
    return _CallableSource(source)


def fetch_from_github(opener: Callable = urllib.request.urlopen) -> dict:
    """The raw LiteLLM dict via api.github.com (sha probe + blob)."""
    src = GitHubSource(opener)
    return src.fetch(src.sha())


# ---------------------------------------------------------------------------
# Loading, caching, refresh


def _cache_path() -> Path:
    return config.APP_DIR / _CACHE_BASENAME  # APP_DIR read at call time, so tests that redirect it are honored


def load(source: Optional[Callable[[], dict]] = None,
         today: Optional[date] = None) -> Optional[Catalogue]:
    """Fetch + parse + validate through an injected source.

    Returns the Catalogue on success and None on ANY failure — the callers
    all treat None as "keep what you had"."""
    source = source or fetch_from_github
    try:
        return parse(source(), today=today or date.today())
    except Exception:
        return None


_lock = threading.Lock()
_current: Optional[Catalogue] = None
_initialized = False
# In-memory mirrors of the persisted meta, so an unwritable app dir (where
# the cache meta cannot be recorded) still cannot turn the loop tick or a
# burst of session launches into a fetch-every-time loop.
_mem_backoff_until = 0.0   # failure / rate-limit backoff only
_mem_last_attempt = 0.0    # any network attempt (throttles session triggers)
_mem_checked_at = 0.0      # last successful sha check (hourly cadence)


def current() -> Optional[Catalogue]:
    """The catalogue the daemon is running on, or None before initialize()
    (openai_models.py then falls back to its literals)."""
    with _lock:
        return _current


def _read_cache_file() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache_file(data: dict) -> None:
    try:
        config.ensure_app_dir()
        tmp = _cache_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_cache_path())
    except OSError:
        pass  # a cache that cannot be written is a missing optimization, not an error


def _load_offline() -> Optional[Catalogue]:
    """Last good fetch from disk, else the vendored snapshot. Never network."""
    cached = _read_cache_file()
    entries = cached.get("entries")
    if isinstance(entries, dict):
        try:
            return parse(entries, today=date.today())
        except Exception:
            pass  # a corrupt cache falls through to the vendored copy
    try:
        return parse(json.loads(_VENDORED_FILE.read_text(encoding="utf-8")),
                     today=date.today())
    except Exception:
        return None


def initialize() -> Optional[Catalogue]:
    """Called once at daemon start. Loads the best offline catalogue
    immediately (no network, cannot block) and nothing else: actual
    refetching is driven by `claude-unlimited code` session launches
    (refresh_if_allowed, via POST /api/models/refresh) and by the hourly
    tick of the daemon's update-check loop (refresh_if_due) — deliberately
    NOT a one-shot at daemon start, so short-lived invocations (doctor,
    installers, tests) never touch the network at all. The loop step lives
    outside updater.run_update_cycle(), which returns early on every no-op
    path (ticket 007, constraint 1)."""
    global _current, _initialized
    with _lock:
        if _initialized:
            return _current
        _initialized = True
    catalogue = _load_offline()
    if catalogue is not None:
        with _lock:
            _current = catalogue
    return catalogue


def refresh(source=None) -> bool:
    """One best-effort, sha-gated refresh. Never raises.

    First the cheap contents-endpoint sha check: when the blob sha equals
    the one the cache remembers (and there is still a catalogue to keep),
    this is a no-op that only records checked_at — the 2.3 MB blob is NOT
    downloaded. Only a changed (or unknown) sha downloads, parses,
    validates, and replaces both the in-memory catalogue and the disk
    cache. On any failure both keep their previous value and a backoff is
    recorded so the next attempt waits.

    `source` may be None (real GitHub), an object with sha()/fetch(), or a
    plain callable returning the raw dict (no cheap probe: always
    downloads)."""
    global _current, _mem_backoff_until, _mem_last_attempt, _mem_checked_at
    src = _as_source(source)
    now = time.time()
    cached = _read_cache_file()
    meta = cached.get("meta")
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta["last_attempt"] = now
    _mem_last_attempt = now
    try:
        sha = src.sha()
        if (sha is not None and sha == meta.get("sha")
                and isinstance(cached.get("entries"), dict) and cached["entries"]
                and current() is not None):
            # Upstream unchanged: record the successful check and stop
            # before the expensive blob download.
            meta["checked_at"] = now
            meta["backoff_until"] = 0
            _mem_checked_at = now
            _mem_backoff_until = 0.0
            _write_cache_meta(meta)
            return True
        raw = src.fetch(sha)
        trimmed = trim_raw(raw, today=date.today())
        catalogue = parse(trimmed, today=date.today())
    except _RateLimited:
        meta["backoff_until"] = now + RATE_LIMIT_BACKOFF_SECONDS
        _mem_backoff_until = meta["backoff_until"]
        _write_cache_meta(meta)
        return False
    except Exception:
        meta["backoff_until"] = now + FAILURE_BACKOFF_SECONDS
        _mem_backoff_until = meta["backoff_until"]
        _write_cache_meta(meta)
        return False
    with _lock:
        _current = catalogue
    meta["fetched_at"] = now
    meta["checked_at"] = now
    if sha is not None:
        meta["sha"] = sha
    else:
        meta.pop("sha", None)  # provenance unknown: force a download next time
    meta["backoff_until"] = 0
    _mem_checked_at = now
    _mem_backoff_until = 0.0
    _write_cache_file({"meta": meta, "entries": trimmed})
    return True


def _write_cache_meta(meta: dict) -> None:
    """Update the meta block (attempt timestamps, backoff) while keeping
    whatever last-good entries the cache already holds."""
    cached = _read_cache_file()
    cached["meta"] = meta
    _write_cache_file(cached)


def _backoff_pending(meta: dict, now: float) -> bool:
    return now < _mem_backoff_until or now < float(meta.get("backoff_until") or 0)


def refresh_if_due(source=None) -> bool:
    """refresh(), but only when the last successful sha CHECK is at least
    an hour old and no backoff is pending: the hourly baseline for daemons
    that run for weeks. Cheap enough to call from a loop tick: timestamp
    reads, no network on the no-op path. The backoff is persisted in the
    cache file, so a restart-looping daemon cannot turn into a request
    loop."""
    meta = _read_cache_file().get("meta")
    meta = meta if isinstance(meta, dict) else {}
    now = time.time()
    if _backoff_pending(meta, now):
        return False
    checked = max(float(meta.get("checked_at") or 0),
                  float(meta.get("fetched_at") or 0),
                  _mem_checked_at)
    if now - checked < REFRESH_INTERVAL_SECONDS:
        return False
    return refresh(source)


def refresh_if_allowed(source=None) -> bool:
    """The session-launch trigger behind POST /api/models/refresh.

    A `code` launch may check ahead of the hourly baseline (models ship
    mid-day and a daemon can run for weeks), but rapid launches coalesce:
    at most one sha check per SHA_CHECK_MIN_INTERVAL_SECONDS, and any
    recorded failure/rate-limit backoff is honored. Throttled on
    last_attempt (success or not), so a burst of launches during an outage
    cannot spam GitHub either. Never raises."""
    meta = _read_cache_file().get("meta")
    meta = meta if isinstance(meta, dict) else {}
    now = time.time()
    if _backoff_pending(meta, now):
        return False
    attempted = max(float(meta.get("last_attempt") or 0), _mem_last_attempt)
    if now - attempted < SHA_CHECK_MIN_INTERVAL_SECONDS:
        return False
    return refresh(source)


def _reset_for_tests() -> None:
    global _current, _initialized, _mem_backoff_until, _mem_last_attempt, _mem_checked_at
    with _lock:
        _current = None
        _initialized = False
        _mem_backoff_until = 0.0
        _mem_last_attempt = 0.0
        _mem_checked_at = 0.0
