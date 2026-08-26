"""The daemon's HTTP surface: /health, the Dashboard management API (/api/*),
and the live proxy (everything else, e.g. /v1/messages) via Gateway.

Route namespaces are strictly partitioned: /api/* (management, CSRF and Host
checked, never touches an Anthropic credential) and everything else (proxy,
placeholder-token checked, never touches the Dashboard's CSRF token) share a
listener but never share auth logic.

Security posture:
  - Binds to 127.0.0.1 only, and refuses any other bind target loudly.
  - Every request's Host header must name this daemon, which blocks
    DNS-rebinding attacks from a malicious page in the same browser.
  - No CORS headers are ever sent, so no origin gets cross-origin access.
  - State-changing requests (POST/PATCH/DELETE) require a per-run CSRF token,
    generated at daemon start, never persisted, and embedded only in the HTML
    this daemon serves.
  - Every response sets Cache-Control: no-store and a strict CSP.
  - Profile JSON responses never include a credential value; Profile has no
    such field (see config.py) and secrets live only in secret_store.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import socket
import threading
import time

if platform.system() != "Windows":
    import resource  # POSIX-only stdlib module, absent on Windows
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import activity
from . import anthropic_oauth
from . import connection_test
from . import daemon_installer
from . import export_import
from . import i18n
from . import notifications
from . import openai_observation
from . import placeholder_token
from . import pricing
from . import profiles as profile_repo
from . import project_attribution
from . import project_usage
from . import session_tokens
from . import updater
from . import usage_history
from .config import APP_DIR, DEFAULT_SWITCH_THRESHOLD, UPDATE_MODES, ensure_app_dir, load_pool, update_settings
from .gateway import Gateway

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 4317
ALLOWED_HOST_NAMES = {"127.0.0.1", "localhost", "claude.unlimited"}

_gateway = Gateway()
_DAEMON_STARTED_AT = datetime.now(timezone.utc)
_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
_APP_JS = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
_FAVICON_SVG = (_STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")

# Regenerated every daemon start; lives only in this process's memory.
_CSRF_TOKEN = secrets.token_urlsafe(32)


class _RefusesNonLoopbackBind(RuntimeError):
    pass


def _assert_bound_to_loopback(server: ThreadingHTTPServer) -> None:
    bound_host = server.server_address[0]
    if bound_host not in ("127.0.0.1", "::1"):
        server.server_close()
        raise _RefusesNonLoopbackBind(
            f"Refusing to run: daemon socket bound to {bound_host!r}, not a loopback "
            "address."
        )


def _security_headers() -> dict:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


# Router state -> the word the Dashboard shows.
_STATUS_WORDS = {
    "eligible": "healthy",
    "draining": "near threshold",
    "exhausted": "exhausted",
    "cooldown": "cooldown",
    "auth_invalid": "needs re-auth",
    "disabled": "disabled",
}

# Defined unconditionally: declaring a struct layout has no OS-specific side
# effect and ctypes.wintypes imports on any platform, so this stays testable
# off Windows. Only using it via ctypes.windll below is Windows-only.
import ctypes
import ctypes.wintypes as wintypes


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _memory_mb() -> float:
    if platform.system() == "Windows":
        # UNVERIFIED on a real Windows machine, like the other Windows
        # backends. GetProcessMemoryInfo is the documented, dependency-free
        # (ctypes + psapi.dll) way to read a process's own working set.
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return round(counters.WorkingSetSize / (1024 * 1024), 1)

    usage = resource.getrusage(resource.RUSAGE_SELF)
    # macOS reports ru_maxrss in bytes, Linux in KB, per getrusage(2).
    divisor = 1024 * 1024 if platform.system() == "Darwin" else 1024
    return round(usage.ru_maxrss / divisor, 1)


def _process_stats() -> dict:
    memory_mb = _memory_mb()
    service = daemon_installer.status()
    return {
        "pid": os.getpid(),
        "uptime_seconds": (datetime.now(timezone.utc) - _DAEMON_STARTED_AT).total_seconds(),
        "memory_mb": memory_mb,
        "installed_as_service": service["installed"],
        "running_as_service": service["running"],
    }


def _window_label_for_display(p, runtime, label_attr: str, resets_attr: str):
    """A codex Profile's window label, derived from its reset time when the
    backend omitted x-codex-*-window-minutes and no label was recorded.

    Codex-only on purpose: oauth and api Profiles have Anthropic's fixed
    5h/7d windows, and the Dashboard's own labels for those must win.
    Without this, a weekly-only account would show whatever label was
    persisted at observation time until the next live request."""
    if runtime is None:
        return None
    recorded = getattr(runtime, label_attr, None)
    if recorded or p.kind != "codex":
        return recorded
    return openai_observation.label_from_reset_distance(
        getattr(runtime, resets_attr, None), datetime.now(timezone.utc))


def _profile_to_public_dict(p, runtime=None, usage=None, in_use_now=False) -> dict:
    """The wire shape sent to the browser. Explicit allowlist, not asdict(),
    so a future field added to Profile can't accidentally leak by default.

    `runtime` is the live Gateway.ProfileRuntime for this Profile, or None
    until the daemon has observed it at least once, in which case the
    Dashboard falls back to the configured enabled/disabled state.

    `usage` is this Profile's lifetime {"tokens", "cost_usd"} from
    usage_history.usage_by_profile(). An api-kind Profile has no
    session-based rate-limit window to show, so the Dashboard shows this
    instead. None reads as "no usage recorded yet", not "unknown".

    `in_use_now` (see Gateway.in_flight_ids()) differs from
    current_profile_id in GET /api/status: several Profiles can be in use at
    once via concurrent pinned `code --profile` sessions, while
    current_profile_id is a single shared rotation pointer."""
    state_value = runtime.state.value if runtime is not None else ("eligible" if p.enabled else "disabled")
    usage = usage or {}
    return {
        "id": p.id,
        "name": p.name,
        "kind": p.kind,
        "base_url": p.base_url,
        "auth_mode": p.auth_mode,
        "priority": p.priority,
        "switch_threshold": p.switch_threshold,
        "enabled": p.enabled,
        "automatic": p.automatic,
        "default_model": p.default_model,
        "monthly_budget_cap": p.monthly_budget_cap,
        "token_threshold": p.token_threshold,
        "tag_color": p.tag_color,
        "account_uuid": p.account_uuid,
        "plan": p.plan,
        "codex_home": p.codex_home,
        "codex_model": p.codex_model,
        "codex_reasoning_effort": p.codex_reasoning_effort,
        "state": state_value,
        "status_word": _STATUS_WORDS.get(state_value, state_value),
        "usage_5h_percent": runtime.last_usage_percent if runtime is not None else None,
        "usage_5h_resets_at": runtime.resets_at.isoformat() if runtime is not None and runtime.resets_at else None,
        "usage_7d_percent": runtime.last_usage_percent_7d if runtime is not None else None,
        "usage_7d_resets_at": runtime.resets_at_7d.isoformat() if runtime is not None and runtime.resets_at_7d else None,
        # None means "assume the Anthropic default" (5h/7d), which is the
        # case for oauth and api Profiles. Only a codex Profile's detected
        # window duration overrides the assumed label.
        "usage_window_label": _window_label_for_display(p, runtime, "window_label", "resets_at"),
        "usage_window_label_7d": _window_label_for_display(p, runtime, "window_label_7d", "resets_at_7d"),
        "tokens_total": usage.get("tokens", 0),
        "cost_usd_total": usage.get("cost_usd"),
        "in_use_now": in_use_now,
    }


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ClaudeUnlimited/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # Never log request paths, headers or bodies: they can contain
        # profile names and base URLs, which are treated as sensitive.
        pass

    # ---- shared request handling ----

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        for k, v in _security_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw_json(self, status: int, body: bytes, *, download_name: str | None = None) -> None:
        """Like _send_json, but for an already-serialized byte body (an
        export bundle) rather than an object to json.dumps."""
        self.send_response(status)
        for k, v in _security_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def _reject_bad_host(self) -> bool:
        host_header = (self.headers.get("Host") or "").split(":")[0]
        if host_header not in ALLOWED_HOST_NAMES:
            self._send_json(400, {"error": "invalid_host", "message": "Host header not recognized."})
            return True
        return False

    def _read_json_body(self, max_bytes: int = 1_000_000):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        if length > max_bytes:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _check_csrf(self) -> bool:
        token = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token, _CSRF_TOKEN):
            self._send_json(403, {"error": "csrf", "message": "Missing or invalid X-CSRF-Token header."})
            return False
        return True

    # ---- routes ----

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_bad_host():
            return
        path = urlparse(self.path).path

        if path == "/health":
            self._send_json(200, {"status": "ok", "version": __version__})
            return

        if path == "/":
            html = _INDEX_HTML.replace("__CSRF_TOKEN__", _CSRF_TOKEN)
            body = html.encode("utf-8")
            self.send_response(200)
            for k, v in _security_headers().items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/app.js":
            body = _APP_JS.encode("utf-8")
            self.send_response(200)
            for k, v in _security_headers().items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/favicon.svg":
            body = _FAVICON_SVG.encode("utf-8")
            self.send_response(200)
            for k, v in _security_headers().items():
                self.send_header(k, v)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/update":
            self._send_json(200, _public_update_state())
            return

        if path == "/api/status":
            with _gateway._lock:
                current_profile_id = _gateway._current_profile_id
            pool = load_pool()
            current_profile = pool.get(current_profile_id) if current_profile_id else None
            self._send_json(200, {
                "status": "ok",
                "version": __version__,
                "csrf_token": _CSRF_TOKEN,
                "default_switch_threshold": DEFAULT_SWITCH_THRESHOLD,
                "uptime_seconds": (datetime.now(timezone.utc) - _DAEMON_STARTED_AT).total_seconds(),
                "current_profile_id": current_profile_id,
                "current_profile_name": current_profile.name if current_profile is not None else None,
            })
            return

        if path == "/api/profiles":
            runtime_map = _gateway.runtime_snapshot()
            usage_map = usage_history.usage_by_profile(usage_history.list_events())
            in_flight = _gateway.in_flight_ids()
            items = [_profile_to_public_dict(p, runtime_map.get(p.id), usage_map.get(p.id), p.id in in_flight)
                     for p in profile_repo.list_profiles()]
            self._send_json(200, {"profiles": items})
            return

        if path == "/api/settings":
            pool = load_pool()
            self._send_json(200, {"settings": asdict(pool.settings)})
            return

        if path == "/api/locales":
            pool = load_pool()
            self._send_json(200, {
                "available": i18n.list_locales(),
                "names": i18n.locale_display_names(),
                "current": pool.settings.language,
            })
            return

        if path.startswith("/api/locales/"):
            code = path.removeprefix("/api/locales/")
            try:
                self._send_json(200, {"language": code, "strings": i18n.load_locale(code)})
            except i18n.UnknownLocaleError as exc:
                self._send_json(404, {"error": "unknown_locale", "message": str(exc)})
            return

        if path == "/api/service":
            self._send_json(200, daemon_installer.status())
            return

        if path == "/api/process":
            self._send_json(200, _process_stats())
            return

        if path == "/api/placeholder-token":
            self._send_json(200, {"token": placeholder_token.get_or_create()})
            return

        if path == "/api/session-token":
            # A GET, not a POST: it must be callable from
            # `claude-unlimited code`, a CLI process with no CSRF token to
            # present, like /api/placeholder-token above. get_or_create()
            # reuses a live token for the same profile_id instead of minting
            # one per call, so this stays an idempotent lookup.
            qs = parse_qs(urlparse(self.path).query)
            profile_id = qs.get("profile_id", [None])[0]
            if not profile_id:
                self._send_json(400, {"error": "bad_request", "message": "profile_id is required."})
                return
            profile = next((p for p in profile_repo.list_profiles() if p.id == profile_id), None)
            if profile is None:
                self._send_json(404, {"error": "not_found", "message": "No profile with that id."})
                return
            if not profile.enabled:
                self._send_json(400, {"error": "disabled", "message": "This profile is disabled — enable it first."})
                return
            self._send_json(200, {"token": session_tokens.get_or_create(profile_id)})
            return

        if path == "/api/usage/projects":
            counts = project_usage.get_counts()
            total = sum(counts.values())
            token_totals = usage_history.tokens_by_project(usage_history.list_events())
            items = [
                {
                    "project_id": pid,
                    "display_name": project_attribution.display_name(pid),
                    "requests": count,
                    "percent": round(count / total * 100, 1) if total else 0,
                    "tokens": token_totals.get(pid, {}).get("tokens", 0),
                    "cost_usd": token_totals.get(pid, {}).get("cost_usd"),
                }
                for pid, count in sorted(counts.items(), key=lambda kv: -kv[1])
            ]
            self._send_json(200, {"projects": items, "total_requests": total})
            return

        if path == "/api/usage/summary":
            qs = parse_qs(urlparse(self.path).query)
            range_key = qs.get("range", [None])[0]
            if range_key not in usage_history.RANGE_KEYS:
                range_key = "1w"
            granularity = usage_history.RANGE_GRANULARITY[range_key]
            events = usage_history.list_events()
            ranged_events = usage_history.filter_events_since(events, range_key)
            bucket_hours = 6  # 4 bars over the last 24h; 24 one-hour bars is too dense for the card
            if granularity == "hour":
                chart_totals = usage_history.hourly_totals(ranged_events, bucket_hours=bucket_hours)
            elif granularity == "week":
                chart_totals = usage_history.weekly_totals(ranged_events)
            elif granularity == "month":
                chart_totals = usage_history.monthly_totals(ranged_events)
            else:
                default_days = usage_history.RANGE_TO_DAYS[range_key]
                days = min(max(int(qs.get("days", [str(default_days)])[0]), 1), 31)
                chart_totals = usage_history.daily_totals(ranged_events, days=days)
            profiles_by_id = {p.id: p for p in profile_repo.list_profiles()}
            by_profile_days = min(max(int(qs.get("days", ["7"])[0]), 1), 31)
            by_profile_totals = usage_history.daily_totals_by_profile(events, days=by_profile_days)
            # usage_history is append-only, so it still carries entries for
            # a deleted Profile's id. Drop anything that isn't a current
            # Profile so the chart never shows a bare internal id.
            for bucket in by_profile_totals:
                bucket["profiles"] = {pid: tok for pid, tok in bucket["profiles"].items() if pid in profiles_by_id}
            self._send_json(200, {
                "range": range_key,
                "granularity": granularity,
                "bucket_hours": bucket_hours if granularity == "hour" else None,
                "daily_totals": chart_totals,
                "daily_totals_by_profile": by_profile_totals,
                "profile_colors": {pid: p.tag_color for pid, p in profiles_by_id.items() if p.tag_color},
                "profile_names": {pid: p.name for pid, p in profiles_by_id.items()},
                "model_split": usage_history.model_split(ranged_events),
                "hourly_histogram": usage_history.hourly_histogram(ranged_events),
                "cost_by_profile": usage_history.cost_by_profile(ranged_events),
                "total_events": len(events),
                "pricing_source": pricing.PRICING_SOURCE,
                "pricing_fetched": pricing.PRICING_FETCHED,
            })
            return

        if path == "/api/activity":
            qs = parse_qs(urlparse(self.path).query)
            limit = min(int(qs.get("limit", ["200"])[0]), 1000)
            category = qs.get("category", [None])[0]
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            events = activity.list_events(limit=limit, category=category, since=since, until=until)
            self._send_json(200, {"events": [asdict(e) for e in events]})
            return

        if path == "/api/activity/export":
            qs = parse_qs(urlparse(self.path).query)
            category = qs.get("category", [None])[0]
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            events = activity.list_events(limit=activity.MAX_EVENTS, category=category, since=since, until=until)
            body = json.dumps({"events": [asdict(e) for e in events]}, indent=2).encode("utf-8")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self._send_raw_json(200, body, download_name=f"claude-unlimited-activity-{stamp}.json")
            return

        # Anything left that isn't a Dashboard or static route is an
        # upstream API path, the same rule do_POST uses. Claude Code issues
        # GET /v1/models to build its `/model` picker; a 404 here makes the
        # client fall back to its built-in Claude list, so the proxy path
        # answers it per active Profile instead.
        if not path.startswith("/api/"):
            self._handle_proxy_request("GET", path)
            return

        self.send_response(404)
        for k, v in _security_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_bad_host():
            return
        path = urlparse(self.path).path

        if not path.startswith("/api/"):
            self._handle_proxy_request("POST", path)
            return

        if not self._check_csrf():
            return

        if path == "/api/profiles":
            try:
                body = self._read_json_body()
                credential = body.pop("credential", "")
                if body.get("kind") == "oauth" and not body.get("account_uuid"):
                    # Resolve the account uuid from Anthropic directly
                    # rather than asking for a value nobody can look up.
                    try:
                        account = anthropic_oauth.fetch_account_profile(credential)
                    except anthropic_oauth.ProfileLookupError as exc:
                        # Recorded server-side too, so the failure leaves a
                        # trace beyond the browser toast that shows it.
                        activity.record("error", "Add Profile failed — could not resolve account", meta=str(exc)[:200])
                        self._send_json(400, {"error": "profile_lookup", "message": str(exc)})
                        return
                    body["account_uuid"] = account.account_uuid
                    body["plan"] = anthropic_oauth.plan_from_account(account)

                existing = profile_repo.find_by_account_uuid(body["account_uuid"]) if body.get("kind") == "oauth" and body.get("account_uuid") else None
                if existing is not None:
                    profile_repo.update_credential(existing.id, credential)
                    if "plan" in body:
                        existing = profile_repo.update_profile(existing.id, plan=body["plan"])
                    self._send_json(200, {"profile": _profile_to_public_dict(existing), "reused_existing": True})
                    return

                p = profile_repo.create_profile(credential=credential, **body)
                self._send_json(201, {"profile": _profile_to_public_dict(p)})
            except profile_repo.ValidationError as exc:
                activity.record("error", "Add Profile failed — validation", meta=str(exc)[:200])
                self._send_json(400, {"error": "validation", "message": str(exc)})
            except profile_repo.ProfileRepositoryError as exc:
                activity.record("error", "Add Profile failed", meta=str(exc)[:200])
                self._send_json(500, {"error": "repository", "message": str(exc)})
            except (ValueError, TypeError) as exc:
                activity.record("error", "Add Profile failed — bad request", meta=str(exc)[:200])
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
            return

        if path.startswith("/api/profiles/") and path.endswith("/take-over"):
            profile_id = path.removeprefix("/api/profiles/").removesuffix("/take-over")
            profile = profile_repo.list_profiles()
            profile = next((p for p in profile if p.id == profile_id), None)
            if profile is None:
                self._send_json(404, {"error": "not_found", "message": "No profile with that id."})
                return
            if not profile.enabled:
                self._send_json(400, {"error": "disabled",
                                       "message": "Enable this Profile first — Take over doesn't re-enable it."})
                return
            _gateway.force_active(profile_id)
            runtime_map = _gateway.runtime_snapshot()
            self._send_json(200, {"profile": _profile_to_public_dict(profile, runtime_map.get(profile_id))})
            return

        if path.startswith("/api/profiles/") and path.endswith("/credential"):
            # A separate route rather than a PATCH /api/profiles/<id> field,
            # because the secret lives in secret_store, not the Pool config
            # JSON update_profile() writes. Lets the Dashboard rotate an API
            # key or pasted OAuth token without re-adding the Profile.
            profile_id = path.removeprefix("/api/profiles/").removesuffix("/credential")
            if not any(p.id == profile_id for p in profile_repo.list_profiles()):
                # update_credential() doesn't check this: it would write an
                # orphaned secret for an unknown id and no-op the config
                # update. Checked here so an unknown id gets a clear 404.
                self._send_json(404, {"error": "not_found", "message": "No profile with that id."})
                return
            try:
                body = self._read_json_body()
                credential = body.get("credential", "")
                # Records its own "<name> credential refreshed" activity
                # entry, so there is nothing else to log here.
                profile_repo.update_credential(profile_id, credential)
            except profile_repo.ValidationError as exc:
                self._send_json(400, {"error": "validation", "message": str(exc)})
                return
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/api/import-claude-code":
            try:
                imported = anthropic_oauth.read_claude_code_credentials()
                account = anthropic_oauth.fetch_account_profile(imported.access_token)
                name = account.email or "Imported Claude account"
                plan = anthropic_oauth.plan_from_account(account)

                # Also refreshes a Profile whose plan has changed: this flow
                # already re-fetches the account, so keeping plan current is
                # free.
                profile_out, reused = profile_repo.upsert_oauth_profile(
                    name=name, account_uuid=account.account_uuid, credential=imported.access_token,
                    plan=plan, refresh_token=imported.refresh_token, expires_at=imported.expires_at,
                )
                status = 200 if reused else 201

                self._send_json(status, {"profile": _profile_to_public_dict(profile_out), "reused_existing": reused, "account": {
                    "email": account.email, "org_name": account.org_name,
                    "has_claude_max": account.has_claude_max, "has_claude_pro": account.has_claude_pro,
                }})
            except anthropic_oauth.CredentialImportError as exc:
                activity.record("error", "Import current login failed — no local session found", meta=str(exc)[:200])
                self._send_json(404, {"error": "no_local_login", "message": str(exc)})
            except anthropic_oauth.ProfileLookupError as exc:
                activity.record("error", "Import current login failed — could not resolve account", meta=str(exc)[:200])
                self._send_json(502, {"error": "profile_lookup", "message": str(exc)})
            except profile_repo.ValidationError as exc:
                activity.record("error", "Import current login failed — validation", meta=str(exc)[:200])
                self._send_json(400, {"error": "validation", "message": str(exc)})
            except profile_repo.ProfileRepositoryError as exc:
                activity.record("error", "Import current login failed", meta=str(exc)[:200])
                self._send_json(500, {"error": "repository", "message": str(exc)})
            return

        if path == "/api/reset":
            count = profile_repo.reset_all_profiles()
            self._send_json(200, {"removed": count})
            return

        if path == "/api/service/install":
            try:
                body = self._read_json_body()
                port = int(body.get("port", DEFAULT_PORT))
                daemon_installer.install(port)
                activity.record("config", "Registered to start automatically on login", meta=f"port {port}")
                self._send_json(200, daemon_installer.status())
            except daemon_installer.DaemonInstallerError as exc:
                self._send_json(500, {"error": "install_failed", "message": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
            return

        if path == "/api/service/uninstall":
            try:
                daemon_installer.uninstall()
                activity.record("config", "No longer starts automatically on login")
                self._send_json(200, daemon_installer.status())
            except daemon_installer.DaemonInstallerError as exc:
                self._send_json(500, {"error": "uninstall_failed", "message": str(exc)})
            return

        if path == "/api/process/kill":
            # Respond before acting: the client must have the confirmation
            # in hand before this process can go away.
            service = daemon_installer.status()
            self._send_json(200, {"killed": True, "was_service": service["installed"]})

            def _do_kill() -> None:
                # Same reason as restart: whatever starts this daemon next
                # should come back showing what it showed before.
                _gateway._persist()
                if service["installed"]:
                    try:
                        daemon_installer.stop()
                    except daemon_installer.DaemonInstallerError:
                        pass
                else:
                    self.server.shutdown()

            threading.Thread(target=_do_kill, daemon=True).start()
            return

        if path == "/api/update/check":
            self._send_json(200, _run_update_check())
            return

        if path == "/api/update/install":
            # Installs whatever the last check found, regardless of mode: this
            # is an explicit click, not the background policy.
            settings = load_pool().settings
            # An explicit click may override the idle guard: the person
            # pressing it can see their own sessions, and is choosing to
            # accept the restart.
            force = bool((self._read_json_body() or {}).get("force"))
            if not force and not _gateway.is_idle(_UPDATE_IDLE_REQUIRED_SECONDS):
                self._send_json(409, {
                    "error": "sessions_active",
                    "message": "A session used the pool recently. Installing now would "
                               "require a restart and could interrupt it. Try again once "
                               "things are idle, or stop the session first.",
                })
                return
            outcome = updater.run_update_cycle(__version__, "auto_install")
            _record_update_outcome(outcome, settings)
            if outcome.error:
                self._send_json(502, {"error": "update_failed", "message": outcome.error})
                return
            if outcome.action != "installed":
                self._send_json(200, {"installed": False, "message": "Already up to date."})
                return
            self._send_json(200, {"installed": True, "version": outcome.release.version,
                                   "restart_required": True})
            return

        if path == "/api/process/restart":
            service = daemon_installer.status()
            if not service["installed"]:
                self._send_json(400, {
                    "error": "not_installed",
                    "message": "Can't restart a foreground daemon from itself — nothing would bring it back. "
                               "Run `claude-unlimited install` first so it's managed as a background service.",
                })
                return
            self._send_json(200, {"restarting": True})

            def _do_restart() -> None:
                # Snapshot first: the replacement process reads this back, so
                # usage percentages and states survive rather than the
                # Dashboard coming back blank.
                _gateway._persist()
                try:
                    daemon_installer.start()  # atomic stop+start via the service manager
                except daemon_installer.DaemonInstallerError:
                    pass

            threading.Thread(target=_do_restart, daemon=True).start()
            return

        if path == "/api/placeholder-token/regenerate":
            token = placeholder_token.regenerate()
            activity.record("config", "Placeholder token regenerated")
            self._send_json(200, {"token": token})
            return

        if path.startswith("/api/profiles/") and path.endswith("/test"):
            profile_id = path.removeprefix("/api/profiles/").removesuffix("/test")
            try:
                result = connection_test.test_connection(profile_id)
                if result["ok"]:
                    activity.record("session", "Connection test succeeded", meta=f"profile={profile_id}, {result['elapsed_ms']}ms")
                else:
                    activity.record("error", "Connection test failed", meta=f"profile={profile_id}, status={result['status']}")
                self._send_json(200, result)
            except connection_test.ConnectionTestError as exc:
                self._send_json(502, {"ok": False, "error": "connection_test_failed", "message": str(exc)})
            except connection_test.ConnectionTestThrottled as exc:
                self._send_json(429, {"ok": False, "error": "connection_test_throttled", "message": str(exc),
                                       "retry_after_seconds": exc.retry_after_seconds})
            return

        if path == "/api/notifications/test":
            notifications.send_macos_notification(
                "Claude Unlimited", "This is a test notification — if you can see this, notifications are working.")
            self._send_json(200, {"sent": True})
            return

        if path == "/api/export":
            try:
                body = self._read_json_body()
                bundle = export_import.build_export_bundle(
                    include_profiles=bool(body.get("include_profiles", False)),
                    include_settings=bool(body.get("include_settings", False)),
                    include_activity=bool(body.get("include_activity", False)),
                    passphrase=body.get("passphrase") or None,
                )
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self._send_raw_json(200, bundle, download_name=f"claude-unlimited-export-{stamp}.json")
                activity.record("config", "Exported a bundle")
            except export_import.ExportImportError as exc:
                self._send_json(400, {"error": "export_failed", "message": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
            return

        if path == "/api/import/preview":
            try:
                body = self._read_json_body(max_bytes=8_000_000)
                bundle = body.get("bundle", "")
                parsed = export_import.import_bundle(bundle, passphrase=body.get("passphrase") or None)
                self._send_json(200, {
                    "profiles": [
                        {"name": p.get("name"), "kind": p.get("kind"), "account_uuid": p.get("account_uuid")}
                        for p in parsed.profiles
                    ],
                    "settings_included": parsed.settings is not None,
                    "activity_count": len(parsed.activity) if parsed.activity else 0,
                })
            except export_import.WrongPassphraseError as exc:
                self._send_json(401, {"error": "wrong_passphrase", "message": str(exc)})
            except export_import.ExportImportError as exc:
                self._send_json(400, {"error": "import_failed", "message": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
            return

        if path == "/api/import/apply":
            try:
                body = self._read_json_body(max_bytes=8_000_000)
                bundle = body.get("bundle", "")
                parsed = export_import.import_bundle(bundle, passphrase=body.get("passphrase") or None)
                result = export_import.apply_import(
                    parsed,
                    import_profiles=bool(body.get("import_profiles", False)),
                    import_settings=bool(body.get("import_settings", False)),
                    conflict_strategy=body.get("conflict_strategy", "keep_existing"),
                )
                self._send_json(200, {"result": result})
            except export_import.WrongPassphraseError as exc:
                self._send_json(401, {"error": "wrong_passphrase", "message": str(exc)})
            except export_import.ExportImportError as exc:
                self._send_json(400, {"error": "import_failed", "message": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
            return

        self.send_response(404)
        for k, v in _security_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def _check_placeholder_token(self) -> Optional[tuple]:
        """None on rejection, having already sent the 401. Otherwise a
        (forced_profile_id_or_None,) tuple: the shared placeholder token
        carries no forced profile and normal Rotation applies, while a
        session_tokens-minted one pins the request to the Profile
        `claude-unlimited code --profile` asked for."""
        auth = self.headers.get("Authorization", "")
        presented = auth[len("Bearer "):] if auth.startswith("Bearer ") else auth
        if placeholder_token.matches(presented):
            return (None,)
        forced_profile_id = session_tokens.resolve(presented)
        if forced_profile_id is not None:
            return (forced_profile_id,)
        self._send_json(401, {"type": "error", "error": {"type": "authentication_error",
                                                           "message": "[claude-unlimited] invalid local credential"}})
        return None

    def _handle_proxy_request(self, method: str, path: str) -> None:
        """Everything that isn't /health or /api/*: the live proxy path.

        Gated by the placeholder token or a session token, never by CSRF:
        Claude Code has no CSRF token, which is a Dashboard-browser
        concept."""

        auth_result = self._check_placeholder_token()
        if auth_result is None:
            return
        (forced_profile_id,) = auth_result

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length > 0 else b""
        inbound_headers = {k: v for k, v in self.headers.items()}

        result = _gateway.handle(method, path, inbound_headers, body, forced_profile_id=forced_profile_id)

        if result.error is not None:
            _FORCED_PROFILE_ERROR_MESSAGES = {
                "forced_profile_missing": "[claude-unlimited] The Profile this session is pinned to no longer exists.",
                "forced_profile_disabled": "[claude-unlimited] The Profile this session is pinned to is disabled.",
                "forced_profile_needs_reauth": "[claude-unlimited] The Profile this session is pinned to needs re-authentication.",
                "upstream_unreachable": "[claude-unlimited] Could not reach Anthropic for the Profile this session is pinned to.",
            }
            message = (
                "[claude-unlimited] The request body is too large to forward."
                if result.error == "bad_request"
                else _FORCED_PROFILE_ERROR_MESSAGES.get(
                    result.error, "[claude-unlimited] No eligible Profile is available right now.")
            )
            self._send_json(result.status, {
                "type": "error",
                "error": {"type": "overloaded_error" if result.status == 503 else "api_error",
                          "message": message},
            })
            return

        self.send_response(result.status)
        for k, v in result.headers.items():
            if k.lower() in ("connection", "transfer-encoding", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for chunk in result.body_chunks:
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected mid-stream

    def do_PATCH(self) -> None:  # noqa: N802
        if self._reject_bad_host() or not self._check_csrf():
            return
        path = urlparse(self.path).path
        if path.startswith("/api/profiles/"):
            profile_id = path.removeprefix("/api/profiles/")
            try:
                body = self._read_json_body()
                p = profile_repo.update_profile(profile_id, **body)
                self._send_json(200, {"profile": _profile_to_public_dict(p)})
            except profile_repo.ValidationError as exc:
                self._send_json(400, {"error": "validation", "message": str(exc)})
            except profile_repo.ProfileRepositoryError as exc:
                self._send_json(404, {"error": "not_found", "message": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
            return

        if path == "/api/settings":
            try:
                body = self._read_json_body()
                s = update_settings(**body)
                self._send_json(200, {"settings": asdict(s)})
            except ValueError as exc:
                self._send_json(400, {"error": "validation", "message": str(exc)})
            return

        self.send_response(404)
        for k, v in _security_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802
        if self._reject_bad_host() or not self._check_csrf():
            return
        path = urlparse(self.path).path
        if path.startswith("/api/profiles/"):
            profile_id = path.removeprefix("/api/profiles/")
            try:
                profile_repo.delete_profile(profile_id)
                self._send_json(204, None) if False else self._send_json(200, {"deleted": profile_id})
            except profile_repo.ProfileRepositoryError as exc:
                self._send_json(404, {"error": "not_found", "message": str(exc)})
            return

        self.send_response(404)
        for k, v in _security_headers().items():
            self.send_header(k, v)
        self.end_headers()


def make_server(host: str = LOOPBACK_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"Refusing to bind to {host!r} — this daemon must only ever bind to a "
            "loopback address."
        )
    # Rebuild the module-level Gateway on every call rather than only at
    # import time. A daemon process calls make_server() once, so this
    # changes nothing in production, but the test suite calls it many times
    # per process. A Gateway built at import time would be constructed
    # before any test fixture had patched APP_DIR, so it would load — and
    # later write — the real ~/.claude-unlimited/runtime_state.json.
    global _gateway
    _gateway = Gateway()
    server = ThreadingHTTPServer((host, port), _DashboardHandler)
    _assert_bound_to_loopback(server)
    return server


PID_FILE = APP_DIR / "daemon.pid"


_OAUTH_REFRESH_LOOP_INTERVAL_SECONDS = 60
_UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_UPDATE_CHECK_STARTUP_DELAY_SECONDS = 120
# An update replaces the running code and needs a restart to take effect, so
# it must never land in the middle of someone's session. Nothing installs
# until the pool has served nothing for this long.
_UPDATE_IDLE_REQUIRED_SECONDS = 15 * 60
# How often to re-check whether a downloaded update can finally be applied.
_UPDATE_IDLE_RECHECK_SECONDS = 5 * 60

# Last known update state, shown by GET /api/update. In memory only: a
# restart should re-check rather than trust a stale verdict from a previous
# process, and a check is cheap.
_update_state: dict = {"checked_at": None, "available": None, "action": "none", "error": None}
_update_lock = threading.Lock()


def _record_update_outcome(outcome, settings) -> None:
    release = outcome.release
    with _update_lock:
        _update_state.update({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "available": None if release is None else {
                "version": release.version, "tag": release.tag, "notes": release.notes[:4000],
            },
            "action": outcome.action,
            "error": outcome.error,
        })
    if outcome.action == "none":
        return
    if outcome.action == "installed":
        activity.record("config", f"Updated to {release.version}", meta="restart to finish")
        notifications.notify_if_enabled("update_available", "Claude Unlimited",
                                          f"Updated to {release.version} — restart to finish.", settings)
        return
    activity.record("config", f"Update available: {release.version}", meta=outcome.action)
    notifications.notify_if_enabled("update_available", "Claude Unlimited",
                                      f"Version {release.version} is available.", settings)


def _public_update_state(settings=None) -> dict:
    """The wire shape for update state. Shared by every endpoint that returns
    it, so a check response can never come back missing fields the Dashboard
    is already displaying."""
    settings = settings or load_pool().settings
    with _update_lock:
        state = dict(_update_state)
    state["current_version"] = __version__
    state["update_mode"] = settings.update_mode
    return state


def _run_update_check(settings=None, *, respect_idle: bool = True) -> dict:
    """One check-and-act pass honoring the configured update_mode.

    In auto_install mode the install is held back while the pool is in use:
    installing swaps the code out from under a running daemon and needs a
    restart to take effect, which would end a live session mid-turn. The
    download still happens immediately, so applying it later is just a file
    move and a restart.

    Never raises: it runs from a background thread and from a request
    handler."""
    settings = settings or load_pool().settings
    mode = settings.update_mode
    deferred = False
    if (respect_idle and mode in updater.MODE_INSTALLS
            and not _gateway.is_idle(_UPDATE_IDLE_REQUIRED_SECONDS)):
        mode = "auto_download"  # fetch and verify now, install once idle
        deferred = True

    outcome = updater.run_update_cycle(__version__, mode)
    _record_update_outcome(outcome, settings)
    with _update_lock:
        _update_state["install_deferred_until_idle"] = bool(deferred and outcome.action == "downloaded")
    return _public_update_state(settings)


def _install_deferred_update_if_idle() -> None:
    """Applies an update that was downloaded while the pool was busy, once it
    has been quiet long enough, then restarts so the new code is actually
    running."""
    with _update_lock:
        pending = _update_state.get("install_deferred_until_idle")
    if not pending or not _gateway.is_idle(_UPDATE_IDLE_REQUIRED_SECONDS):
        return
    settings = load_pool().settings
    if settings.update_mode not in updater.MODE_INSTALLS:
        return

    outcome = updater.run_update_cycle(__version__, "auto_install")
    _record_update_outcome(outcome, settings)
    with _update_lock:
        _update_state["install_deferred_until_idle"] = outcome.action == "downloaded"
    if outcome.action != "installed":
        return
    _gateway._persist()  # snapshot before the process goes away
    if daemon_installer.status()["installed"]:
        try:
            daemon_installer.start()  # atomic stop+start; new code takes effect
        except daemon_installer.DaemonInstallerError:
            activity.record("error", "Update installed but the restart failed",
                             meta="restart the daemon to finish")


def _update_check_loop() -> None:
    """Polls for a new release on a slow timer.

    Deliberately unhurried and jittered by the startup delay: this is a
    background courtesy, not something worth hammering a public API for.
    Every pass is best-effort, so being offline is a no-op, not a crash."""
    time.sleep(_UPDATE_CHECK_STARTUP_DELAY_SECONDS)
    since_last_check = _UPDATE_CHECK_INTERVAL_SECONDS
    while True:
        try:
            if since_last_check >= _UPDATE_CHECK_INTERVAL_SECONDS:
                _run_update_check()
                since_last_check = 0
            else:
                # Between full checks, keep asking whether an already
                # downloaded update can finally be applied.
                _install_deferred_update_if_idle()
        except Exception:
            pass
        time.sleep(_UPDATE_IDLE_RECHECK_SECONDS)
        since_last_check += _UPDATE_IDLE_RECHECK_SECONDS


def _oauth_refresh_loop() -> None:
    """Keeps OAuth Profiles' access tokens fresh independently of traffic.

    The Dashboard's poll only runs while someone has it open, and the
    proxy's per-request refresh only touches the Profile choose() just
    picked. Without this loop, an idle Profile can pass its token expiry
    unrefreshed and land on AUTH_INVALID, with no way back except a manual
    re-auth, since choose() never picks an AUTH_INVALID Profile again.

    Runs for the life of the daemon process. Every call is best-effort, so
    a transient network failure can never crash the daemon."""
    while True:
        time.sleep(_OAUTH_REFRESH_LOOP_INTERVAL_SECONDS)
        try:
            _gateway.runtime_snapshot()
        except Exception:
            pass


def run_foreground(host: str = LOOPBACK_HOST, port: int = DEFAULT_PORT) -> None:
    server = make_server(host, port)
    threading.Thread(target=_oauth_refresh_loop, daemon=True).start()
    threading.Thread(target=_update_check_loop, daemon=True).start()
    print(f"CSRF token for this run (Dashboard needs it, never logged again): {_CSRF_TOKEN}")
    # Written on every start on every OS. Harmless where the installer
    # backend gets a pid another way (launchctl, systemctl), and the only
    # way the Windows Task Scheduler backend knows what to stop, since
    # schtasks doesn't expose a scheduled task's child-process pid.
    try:
        ensure_app_dir()
        PID_FILE.write_text(str(os.getpid()))
    except OSError:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def find_free_loopback_port(preferred: int = DEFAULT_PORT) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((LOOPBACK_HOST, preferred))
            return preferred
        except OSError:
            probe.bind((LOOPBACK_HOST, 0))
            return probe.getsockname()[1]
