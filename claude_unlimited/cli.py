"""Claude Unlimited CLI: daemon lifecycle, plus two deliberate exceptions.

Profile CRUD (list, edit, delete, threshold, priority, enable) has no CLI
surface and happens exclusively in the Dashboard. `add-account` (alias `ac`)
and `reauth` are the exceptions, because an OAuth browser handshake needs
interactivity — opening a browser, waiting for a redirect — that a JSON API
triggered from a page does not fit. `add-account` creates one Profile and
`reauth` re-authenticates an existing one; everything else about a Profile
goes through the Dashboard, over the same config.py and secret_store.
"""

from __future__ import annotations

import argparse
import errno
import json
import uuid
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import __version__
from . import anthropic_oauth
from . import daemon_installer
from . import hud as hud_installer
from . import i18n
from . import updater
from . import profiles as profile_repo
from .config import CLAUDE_ACCOUNTS_DIR, CODEX_ACCOUNTS_DIR, ensure_app_dir, load_pool
from .daemon import DEFAULT_PORT, LOOPBACK_HOST, run_foreground

# The escalation ladder for stopping a process. SIGKILL is Unix-only — even
# naming `signal.SIGKILL` raises AttributeError on Windows — so it is included
# only where it exists. On macOS/Linux this stays (SIGTERM, SIGKILL) exactly as
# before; on Windows it is (SIGTERM,), where os.kill maps SIGTERM to
# TerminateProcess.
_STOP_SIGNALS = tuple(
    s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGKILL", None))
    if s is not None
)


def _resolve_launcher(name: str) -> str:
    """Full path to an external CLI, honoring Windows PATHEXT (.cmd/.exe), or
    the bare name if not found. `shutil.which` already does PATHEXT resolution;
    passing the resolved path (not the bare name) to subprocess is what makes a
    non-.exe launcher work at all on Windows."""
    return shutil.which(name) or name


def _run_tool(argv: list, **kw):
    """subprocess.run for an external CLI that may be a Windows `.cmd`/`.bat`
    shim (npm installs Claude Code and Codex as `.cmd`). CreateProcess resolves
    only `.exe` and ignores PATHEXT, so a shim must be routed through `cmd /c`.
    On POSIX this is a plain resolved-path run."""
    exe = _resolve_launcher(argv[0])
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return subprocess.run(["cmd", "/c", exe, *argv[1:]], **kw)
    return subprocess.run([exe, *argv[1:]], **kw)


def _probe_health(host: str, port: int, timeout: float = 1.0) -> bool:
    """True only if something at host:port answers like this daemon's
    /health, not merely that the port is open."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
            body = json.loads(resp.read())
            return body.get("status") == "ok" and "version" in body
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError):
        return False


def _running_version(host: str, port: int, timeout: float = 1.0) -> Optional[str]:
    """The version a daemon at host:port reports, or None if none answers.

    Upgrading only replaces files on disk. A daemon already running keeps
    serving the version it started with, so "is it up?" is the wrong question
    after an install — "is the one that is up the one just installed?" is the
    right one."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
            body = json.loads(resp.read())
            if body.get("status") == "ok":
                return body.get("version")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError):
        pass
    return None


def _wait_for_version(host: str, port: int, expected: str, timeout: float = 20.0) -> Optional[str]:
    """Waits for the daemon to answer on `expected`, returning whatever it
    last reported (None if nothing answered at all)."""
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        seen = _running_version(host, port, timeout=1.0)
        if seen == expected:
            return seen
        time.sleep(0.3)
    return seen


def _banner() -> None:
    print(f"Claude Unlimited {__version__}")
    print("=" * (18 + len(__version__)))


def doctor() -> int:
    _banner()
    ok = True

    print(f"Python: OK — {sys.version.split()[0]}")

    try:
        import claude_unlimited.secret_store as _ss

        print(f"Secret store: OK — {_ss.BACKEND_NAME} backend loaded")
    except Exception as exc:
        print(f"Secret store: MISSING — {exc}")
        ok = False

    ensure_app_dir()
    pool = load_pool()
    print(f"Config dir: OK — {pool.shared_claude_dir}")
    print(f"Profiles configured: {len(pool.profiles)}")
    if not pool.profiles:
        print("  (none yet — run `claude-unlimited add-account`, or add one from the Dashboard)")

    # Heal any missing launcher (e.g. `cu` after updating from an older
    # version whose updater couldn't), then report what's actually on PATH.
    try:
        updater.ensure_cli_aliases()
    except Exception:
        pass
    on_path = [name for name in ("claude-unlimited", "cu") if shutil.which(name)]
    if len(on_path) == 2:
        print("CLI launchers: OK — claude-unlimited and cu both on PATH")
    else:
        missing = [n for n in ("claude-unlimited", "cu") if n not in on_path]
        print(f"CLI launchers: {', '.join(on_path) or 'none'} on PATH — "
              f"{', '.join(missing)} missing (ensure ~/.local/bin is on your PATH)")
        ok = False

    print("Live proxy: ready — rotation, credential substitution, and usage tracking active.")

    if hud_installer.is_supported():
        # Also the least surprising place to repair it: someone running
        # `doctor` because the HUD vanished gets it back from this line.
        installed = (hud_installer.BUNDLE_DIR / hud_installer.BUNDLE_NAME).is_dir()
        if not installed:
            installed = hud_installer.ensure_installed(version=__version__) == "installed"
        print(f"HUD - Heads-Up Display: {'OK — installed in ~/Applications' if installed else 'not installed (run `cu hud install`)'}")

    if sys.platform == "darwin":
        notif_ok = shutil.which("osascript") is not None
        notif_via = "osascript"
    elif sys.platform == "linux":
        notif_ok = shutil.which("notify-send") is not None
        notif_via = "notify-send"
    elif sys.platform == "win32":
        notif_ok = shutil.which("powershell") is not None
        notif_via = "powershell"
    else:
        notif_ok, notif_via = False, "unknown platform"
    print(f"Desktop notifications: {'OK — ' + notif_via + ' found' if notif_ok else notif_via + ' not found on PATH — unavailable'}")

    service_status = daemon_installer.status()
    if service_status["installed"]:
        print(f"Background service: installed — {'running' if service_status['running'] else 'NOT running'}"
              + (f" (pid {service_status['pid']})" if service_status["pid"] else ""))
    else:
        print("Background service: not installed — run `claude-unlimited install` to start on login, "
              "or `claude-unlimited start` to run in the foreground")

    print(f"Dashboard languages available: {', '.join(i18n.list_locales())} (current: {pool.settings.language})")

    print("\nResult: " + ("READY" if ok else "NEEDS ATTENTION"))
    return 0 if ok else 1


def status() -> int:
    _banner()
    s = daemon_installer.status()
    if not s["installed"]:
        print(
            "Not installed as a background service. Run `claude-unlimited install` to have "
            "it start automatically on login, or `claude-unlimited start` to run it in the "
            "foreground of this terminal for now."
        )
        return 0
    if s["running"]:
        print(f"Installed and running — pid {s['pid']}.")
    else:
        print("Installed, but not currently running. Run `claude-unlimited service-start` to start it.")
    return 0


def start(port: int) -> int:
    _banner()
    running = _running_version(LOOPBACK_HOST, port)
    if running is not None:
        if running != __version__:
            # Saying "nothing more to do" here is how an upgrade quietly fails:
            # the files on disk are new, the daemon answering is old, and
            # nothing says so.
            print(f"Version {running} is already running at http://{LOOPBACK_HOST}:{port}/, "
                  f"but {__version__} is installed.")
            print("Run `claude-unlimited restart` to serve the installed version.")
            return 1
        print(f"Already running at http://{LOOPBACK_HOST}:{port}/ — nothing more to do.")
        print("(Open that URL for the Dashboard, or `claude-unlimited status` for details.)")
        return 0
    print(f"Starting daemon on {LOOPBACK_HOST}:{port} (Ctrl-C to stop)")
    print(f"Dashboard: http://{LOOPBACK_HOST}:{port}/")
    try:
        run_foreground(host=LOOPBACK_HOST, port=port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"\nPort {port} is already in use by something else (not Claude Unlimited — "
                "its own health check didn't answer). Free the port, or run "
                f"`claude-unlimited start --port <other>` to use a different one.",
                file=sys.stderr,
            )
            return 1
        raise
    return 0


def _request_models_refresh(host: str, port: int, timeout: float = 0.5) -> None:
    """Fire-and-forget kick of POST /api/models/refresh at `code` launch.

    The daemon answers immediately (the actual sha check runs in its own
    background thread, throttled and backed off in model_catalogue), so
    this normally costs a couple of milliseconds — and every failure is
    swallowed, because launching `claude` must never be delayed or blocked
    by a model-catalogue nicety. Rapid launches coalesce daemon-side: at
    most one GitHub sha check per ~5 minutes."""
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/models/refresh", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except Exception:
        pass


def _fetch_placeholder_token(host: str, port: int, timeout: float = 2.0) -> str:
    with urllib.request.urlopen(f"http://{host}:{port}/api/placeholder-token", timeout=timeout) as resp:
        return json.loads(resp.read())["token"]


def _spawn_background_daemon(port: int) -> None:
    """Launches the daemon in its own session, fully detached, because it
    must keep running after code() execvp's this process into `claude`.

    This is not the service-install path (see docs/adr/0002-*): nothing here
    survives a reboot or gets supervised. It only makes sure something is
    listening right now, the equivalent of `claude-unlimited start &`.
    `claude-unlimited install` is what provides real persistence."""
    log_dir = Path.home() / ".claude-unlimited" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_log = open(log_dir / "daemon.out.log", "a", encoding="utf-8")
    err_log = open(log_dir / "daemon.err.log", "a", encoding="utf-8")
    # Detach so the daemon outlives this launcher. `start_new_session` is a
    # POSIX setsid; on Windows CPython silently ignores it, leaving the daemon
    # tied to the console — it dies when the terminal closes and catches the
    # terminal's Ctrl-C. Windows needs explicit creation flags instead.
    detach_kwargs = {}
    argv = [sys.executable, "-m", "claude_unlimited", "start", "--port", str(port)]
    if os.name == "nt":
        detach_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        # Confirmed on real hardware: stdout/stderr redirected to a file (as
        # here) makes CPython fully block-buffer them, same as any non-TTY
        # destination — the daemon's startup banner and everything after it
        # sat unflushed in daemon.out.log/.err.log indefinitely, even though
        # the daemon was up and answering requests. -u forces unbuffered I/O.
        # macOS/Linux dodge this because launchd/systemd set PYTHONUNBUFFERED
        # on the *service* unit; this spawn path (used by `code`'s implicit
        # daemon start) is a separate launcher those units never cover.
        argv.insert(1, "-u")
    else:
        detach_kwargs["start_new_session"] = True
    subprocess.Popen(
        argv,
        stdout=out_log, stderr=err_log, stdin=subprocess.DEVNULL,
        **detach_kwargs,
    )


def _fetch_session_token(host: str, port: int, profile_id: str, timeout: float = 2.0) -> str:
    url = f"http://{host}:{port}/api/session-token?profile_id={urllib.parse.quote(profile_id)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())["token"]


def _fetch_distribute_token(host: str, port: int, timeout: float = 2.0) -> str:
    """A session token meaning "spread this session's branches across
    accounts" — the main agent and each subagent get their own pin."""
    url = f"http://{host}:{port}/api/session-token?mode=distribute"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())["token"]


def _match_profile(profiles: list, needle: str):
    """Resolve --profile NAME_OR_ID against currently-enabled Profiles:
    exact id, then exact case-insensitive name, then a name substring. Only
    the substring form can be ambiguous, and an ambiguous one resolves to
    None."""
    for p in profiles:
        if p.id == needle:
            return p
    lowered = needle.lower()
    for p in profiles:
        if p.name.lower() == lowered:
            return p
    matches = [p for p in profiles if lowered in p.name.lower()]
    return matches[0] if len(matches) == 1 else None


def _prompt_profile_choice(profiles: list):
    """Interactive "[1] Rotated accounts / [2] <name> / ..." picker.

    Returns a Profile to pin to, or None for the normal unpinned behavior.
    Empty input defaults to option 1. KeyboardInterrupt and EOFError
    propagate: code() decides how Ctrl-C or Ctrl-D ends the process."""
    print("Which Profile should this session use?\n")
    print("  [1] Rotated accounts (default — automatic threshold/priority rotation)")
    for i, p in enumerate(profiles, start=2):
        print(f"  [{i}] {p.name}")
    print()
    raw = input(f"Choice [1-{len(profiles) + 1}, default 1]: ").strip()
    if not raw or raw == "1":
        return None
    try:
        idx = int(raw)
    except ValueError:
        print(f"Not a number: {raw!r} — using Rotated accounts.")
        return None
    if idx == 1:
        return None
    if 2 <= idx <= len(profiles) + 1:
        return profiles[idx - 2]
    print(f"{idx} isn't one of the choices — using Rotated accounts.")
    return None


# Claude Code's `/model` picker is built entirely client-side from these env
# vars and never asks the proxy what models exist, so a codex-pinned session
# has to relabel the picker itself or it keeps offering Claude names for
# models actually served by OpenAI.
#
# A tier needs its *_MODEL set for the entry to appear at all; _NAME and
# _DESCRIPTION are what show in the list. The ids stay Anthropic-shaped
# because openai_models.map_model() is keyed on them; the label is where the
# backing model is surfaced.
# These MUST be the same ids Claude Code uses as each tier's native default,
# or our override lands as an EXTRA picker entry beside the native one instead
# of replacing it (v2.1.263 showed both a relabelled Fable and a native "Fable
# 5.1"). Read out of the 2.1.263 binary's model table: fable->claude-fable-5-1,
# opus->claude-opus-5, sonnet->claude-sonnet-5, haiku->claude-haiku-4-5. This
# is upstream-coupled — see the "Claude Code upstream watch" note; a Claude
# Code release that moves a tier default can reintroduce the duplicate.
_MODEL_TIER_IDS = {
    "FABLE": "claude-fable-5-1",
    "OPUS": "claude-opus-5",
    "SONNET": "claude-sonnet-5",
    "HAIKU": "claude-haiku-4-5",
}

# Each tier's family prefix — a saved parity row is matched to a tier by family
# (not exact id), so a row saved with a dated or point-release id still labels
# the right picker slot instead of silently falling back to a stale literal.
_MODEL_TIER_FAMILY = {
    "FABLE": "claude-fable",
    "OPUS": "claude-opus",
    "SONNET": "claude-sonnet",
    "HAIKU": "claude-haiku",
}


def _tier_live_label(live: dict, tier_id: str, family: str):
    """The (name, effort) from the live parity list for a tier: exact id, then
    dated/undated base id, then family prefix. None if the list has no row for
    this family (the user removed it)."""
    from .model_catalogue import base_id
    if tier_id in live:
        return live[tier_id]
    base = base_id(tier_id)
    for claude_id, value in live.items():
        if base_id(claude_id) == base:
            return value
    for claude_id, value in live.items():
        if base_id(claude_id).lower().startswith(family):
            return value
    return None

# OFFLINE FALLBACK ONLY. The live labels are derived per launch from the
# daemon's own parity map (_fetch_parity_labels -> GET /v1/models), so they
# always name the model that will really serve and never go stale. These
# literals are used only when that fetch fails, and are deliberately in the
# same `<Claude tier> | <GPT model>` shape the live path produces so the
# picker reads identically either way.
#
# Pinned to a codex Profile: every request this session makes is served by
# OpenAI, so name the real backing model outright.
_CODEX_MODEL_LABELS = {
    "FABLE": ("Fable 5.1 | GPT-6 Astra", "Served by Codex · reasoning: high"),
    "OPUS": ("Opus 5 | GPT-5.6 Terra", "Served by Codex · reasoning: high"),
    "SONNET": ("Sonnet 5 | GPT-5.6 Terra", "Served by Codex · reasoning: medium"),
    "HAIKU": ("Haiku 4.5 | GPT-5.6 Luna", "Served by Codex · reasoning: low"),
}

# Rotated across a pool that mixes providers: which provider serves a given
# request is decided per request and shifts as quotas move, while the picker
# is read once at launch and never updated. Naming BOTH models a tier maps
# to stays accurate whichever one serves, and still says what is being
# picked; a provider-neutral tier word would name no model at all.
_MIXED_MODEL_LABELS = {
    "FABLE": ("Fable 5.1 | GPT-6 Astra", "Whichever account is active · Codex reasoning: high"),
    "OPUS": ("Opus 5 | GPT-5.6 Terra", "Whichever account is active · Codex reasoning: high"),
    "SONNET": ("Sonnet 5 | GPT-5.6 Terra", "Whichever account is active · Codex reasoning: medium"),
    "HAIKU": ("Haiku 4.5 | GPT-5.6 Luna", "Whichever account is active · Codex reasoning: low"),
}


def _fetch_parity_labels(host: str, port: int, token: str, timeout: float = 2.0) -> dict:
    """`{claude_id: (name, effort)}` from the daemon's own parity list.

    GET /v1/models returns exactly the `<Claude tier> | <GPT model> · <effort>`
    display names the Models table shows, computed from the live catalogue, so
    the picker names the model that will really serve and never goes stale (the
    hand-kept literals said GPT-5.6 Sol long after the map moved to GPT-6
    Astra). Best-effort: any failure returns {} and the caller falls back to
    the offline literals. `name` keeps the full `Claude X | GPT Y` — dropping
    the effort tail, which moves to the picker's description line."""
    out: dict = {}
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/models",
            headers={"Authorization": f"Bearer {token}", "x-api-key": token})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        for entry in data.get("data", []):
            mid = entry.get("id")
            disp = entry.get("display_name") or ""
            if not mid or "|" not in disp:
                continue
            name, _, effort = disp.rpartition(" · ")
            out[mid] = (name or disp, effort)
    except Exception:
        pass
    return out


def _apply_model_labels(forced_profile, enabled_profiles=None,
                        host=None, port=None, token=None) -> None:
    """Relabel Claude Code's `/model` picker to match what will really serve.

    The picker is built entirely client-side from these env vars, read once
    when claude starts, and the proxy is never consulted. The labels are
    therefore fixed for the life of the process and cannot track rotation.

    Three cases:
      * pinned to codex -> the `<Claude tier> | <GPT model>` the request will
        actually be translated to; the pin holds for the whole session (a
        pinned request that can't be served errors rather than rotating), so
        these stay true.
      * pinned to claude/api, or a pool with no codex Profile -> leave Claude
        Code's native labels alone.
      * rotated across a mixed pool -> name both the Claude tier and the GPT
        model it maps to, since either provider may serve a given request.

    Live values come from the daemon's parity map (GET /v1/models, now exactly
    the user's saved parity list) when reachable, so the picker matches the
    Models table; the module literals are the offline fallback. A tier the
    user has REMOVED from the parity list gets no label — its env stays unset
    so the picker doesn't resurrect a stale default. Never overrides a value
    already set in the environment.

    The env mechanism has only four tier slots (FABLE/OPUS/SONNET/HAIKU); a
    parity list with extra rows advertises them via /v1/models but Claude
    Code's picker can relabel at most these four — see the README note."""
    kind = getattr(forced_profile, "kind", None)
    if forced_profile is not None:
        labels = _CODEX_MODEL_LABELS if kind == "codex" else None
        codex_pinned = kind == "codex"
    else:
        has_codex = any(getattr(p, "kind", None) == "codex" for p in (enabled_profiles or []))
        # An all-Claude pool never mislabels anything; leave it native.
        labels = _MIXED_MODEL_LABELS if has_codex else None
        codex_pinned = False
    if labels is None:
        return

    live = _fetch_parity_labels(host, port, token) if (host and port and token) else {}
    for tier, (fallback_name, fallback_desc) in labels.items():
        tier_id = _MODEL_TIER_IDS[tier]
        match = _tier_live_label(live, tier_id, _MODEL_TIER_FAMILY[tier]) if live else None
        if match is not None:
            name, effort = match
            lead = "Served by Codex" if codex_pinned else "Whichever account is active · Codex"
            description = f"{lead} · reasoning: {effort}" if effort else lead
        elif live:
            # Fetch succeeded but the user's parity list has no row for this
            # family — they removed it; leave the tier unlabelled.
            continue
        else:
            name, description = fallback_name, fallback_desc  # daemon unreachable: offline literals
        for suffix, value in (("", tier_id), ("_NAME", name), ("_DESCRIPTION", description)):
            os.environ.setdefault(f"ANTHROPIC_DEFAULT_{tier}_MODEL{suffix}", value)


def _user_already_has_a_status_line() -> bool:
    """True if a settings file this project must not override already defines
    one. Checked so the Dashboard hint never replaces a status line the user
    configured themselves."""
    candidates = [
        Path.home() / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.local.json",
    ]
    for f in candidates:
        try:
            if "statusLine" in json.loads(f.read_text(encoding="utf-8")):
                return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


# Claude Code applies the `env` block from its settings files on top of the
# process environment, so a project that pins ANTHROPIC_BASE_URL or
# ANTHROPIC_AUTH_TOKEN in .claude/settings.json silently wins over the routing
# we just set up — requests bypass the daemon entirely and go wherever that
# file says, using whatever credential it carries.
_ROUTING_ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def _settings_files_pinning_routing() -> list:
    """Settings files whose `env` block would redirect this session's traffic.

    Only these three keys matter — everything else a project puts in `env` is
    its own business and is left alone."""
    candidates = [
        Path.home() / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.local.json",
    ]
    conflicting = []
    for f in candidates:
        try:
            env = (json.loads(f.read_text(encoding="utf-8")) or {}).get("env") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        hits = [k for k in _ROUTING_ENV_KEYS if k in env]
        if hits:
            conflicting.append((f, hits))
    return conflicting


def _status_line_args(port: int, claude_args: list[str]) -> list[str]:
    """`claude` arguments adding a status line that shows the Dashboard URL,
    so it stays visible for the whole session instead of scrolling away with
    the launch banner.

    Passed as an inline --settings JSON string, which `claude` merges on top
    of its normal settings files: nothing on disk is written or modified.
    Returns [] rather than overriding when the user passed their own
    --settings, or already configured a status line."""
    if any(a == "--settings" or a.startswith("--settings=") for a in claude_args):
        return []

    settings: dict = {}

    # Reassert routing over any settings file that pins it, so the session
    # actually goes through the pool it was launched for. Scoped to the three
    # routing keys and passed inline — nothing on disk is read differently or
    # written.
    conflicting = _settings_files_pinning_routing()
    if conflicting:
        settings["env"] = {
            "ANTHROPIC_BASE_URL": os.environ["ANTHROPIC_BASE_URL"],
            "ANTHROPIC_AUTH_TOKEN": os.environ["ANTHROPIC_AUTH_TOKEN"],
        }
        for path, keys in conflicting:
            print(f"Note: {path} sets {', '.join(keys)} — overriding it for this "
                  f"session so requests go through Claude Unlimited.")

    if not _user_already_has_a_status_line():
        label = f"\u26a1 Claude Unlimited \u00b7 http://{LOOPBACK_HOST}:{port}"
        settings["statusLine"] = {"type": "command", "command": f"printf %s {shlex.quote(label)}"}

    return ["--settings", json.dumps(settings)] if settings else []


def _remove_isolated_claude_logins(config_dirs: list) -> None:
    """purge's reporting wrapper around anthropic_oauth.remove_isolated_logins.

    The removal itself lives there because delete_profile() needs the same
    thing and cannot import this module."""
    removed = anthropic_oauth.remove_isolated_logins(config_dirs)
    print(f"Isolated Claude Code logins removed: {removed} "
          f"(your own `claude` login was not touched)")


def restart(port: int) -> int:
    """Stops and starts the daemon, whichever shape it is running in.

    `service-start`/`service-stop` only apply to a daemon the service manager
    owns. One started by install.sh or `claude-unlimited start` is detached and
    belongs to nobody, so it needs stopping directly and starting again — which
    is exactly what is needed after an update replaces the code on disk, since
    the running process keeps serving whatever it started with."""
    _banner()
    service = daemon_installer.status()

    if service["installed"]:
        try:
            daemon_installer.start()  # atomic stop+start
        except daemon_installer.DaemonInstallerError as exc:
            print(f"Could not restart the service: {exc}", file=sys.stderr)
            return 1
    else:
        _stop_running_daemon(port)
        _spawn_background_daemon(port)

    for _ in range(20):
        if _probe_health(LOOPBACK_HOST, port, timeout=0.5):
            print(f"Restarted — http://{LOOPBACK_HOST}:{port}/")
            return 0
        time.sleep(0.5)

    print(f"The daemon did not come back on {LOOPBACK_HOST}:{port}.", file=sys.stderr)
    print("Start it yourself with `claude-unlimited start`.", file=sys.stderr)
    return 1


def _pids_listening_on(port: int) -> list[int]:
    """PIDs listening on the port, or [] if that can't be determined.

    Best-effort and POSIX-only: Windows has its own service manager and is not
    the platform where detached daemons pile up."""
    if os.name == "nt":
        return []
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in result.stdout.split():
        try:
            pids.append(int(line))
        except ValueError:
            pass
    return pids


def _is_our_daemon(pid: int) -> bool:
    """Whether pid is one of ours, checked before signalling it.

    Holding the port is not enough to justify killing a process — it could be
    anything the user happens to be running."""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude_unlimited" in result.stdout


def _stop_running_daemon(port: int, timeout: float = 8.0) -> None:
    """Stops a daemon that the service manager does not own.

    install.sh starts one detached and `claude-unlimited start` runs one in a
    terminal; neither is a launchd/systemd job, so deregistering the service
    does not touch them and the dashboard keeps answering. Uses the pid the
    daemon records on every start, then confirms the port actually stopped
    responding rather than assuming the signal worked."""
    pid_file = Path.home() / ".claude-unlimited" / "daemon.pid"
    pid = None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass

    if pid:
        for sig in _STOP_SIGNALS:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            except OSError:
                break
            deadline = time.time() + timeout / 2
            while time.time() < deadline:
                if not _probe_health(LOOPBACK_HOST, port, timeout=0.5):
                    break
                time.sleep(0.25)
            if not _probe_health(LOOPBACK_HOST, port, timeout=0.5):
                break

    # The pid file names one daemon — the last to write it. A second detached
    # daemon (a different interpreter, an older install, a leftover from
    # before a service was registered) is invisible to it and keeps the port,
    # which is how an upgrade ends up leaving the OLD version serving. Fall
    # back to whoever actually holds the port, but only signal processes that
    # are demonstrably ours.
    if _probe_health(LOOPBACK_HOST, port, timeout=1.0):
        for stray in _pids_listening_on(port):
            if stray == os.getpid() or not _is_our_daemon(stray):
                continue
            print(f"Stopping a detached daemon still holding port {port} (pid {stray})…")
            for sig in _STOP_SIGNALS:
                try:
                    os.kill(stray, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    break
                deadline = time.time() + timeout / 2
                while time.time() < deadline:
                    if not _probe_health(LOOPBACK_HOST, port, timeout=0.5):
                        break
                    time.sleep(0.25)
                if not _probe_health(LOOPBACK_HOST, port, timeout=0.5):
                    break

    if _probe_health(LOOPBACK_HOST, port, timeout=1.0):
        print(f"WARNING: something is still serving {LOOPBACK_HOST}:{port}. "
              "Stop it before the files are removed, or it will keep running "
              "against deleted code.")
    else:
        print("Daemon: stopped")


def purge(port: int = DEFAULT_PORT, assume_yes: bool = False) -> int:
    """Removes everything this project created, including credentials.

    Deliberately more thorough than the uninstall script: it deletes each
    Profile's entry from the OS keystore first, while the config that names
    those Profiles still exists. Once the config directory is gone there is
    nothing left to enumerate them from, and the credentials would linger with
    no way to find them again except by hand.

    Never touches ~/.claude — the user's own Claude Code setup is not ours to
    delete."""
    from . import secret_store

    app_dir = Path.home() / ".claude-unlimited"
    install_root = Path.home() / ".local" / "share" / "claude-unlimited"
    bin_dir = Path.home() / ".local" / "bin"
    # Both names ship, so both have to go. Removing only `claude-unlimited`
    # left `cu` behind as a dangling symlink after every purge.
    cli_links = [bin_dir / name for name in updater.CLI_NAMES]

    _banner()
    print("This removes Claude Unlimited and everything it created:")
    print("  - stored credentials for every Profile (from your OS keystore)")
    print(f"  - {app_dir}  (config, usage history, activity log, isolated account sessions)")
    print(f"  - {install_root}  (the app and its virtualenv)")
    for link in cli_links:
        print(f"  - {link}")
    print("  - the background service registration, if installed")
    if hud_installer.is_supported():
        print(f"  - {hud_installer.BUNDLE_DIR / hud_installer.BUNDLE_NAME} and its login item")
    if (APP_DIR_PATH() / "claude-desktop-backup").is_dir():
        print("  - the Claude desktop app's routing through the pool (its own settings")
        print("    are restored to how they were before `claude-unlimited desktop`)")
    print()
    print("Your own Claude Code setup (~/.claude) is NOT touched.")
    print()

    if not assume_yes:
        if not sys.stdin.isatty():
            print("Refusing to purge without confirmation. Re-run with --yes.", file=sys.stderr)
            return 1
        try:
            answer = input("Type 'purge' to confirm: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 1
        if answer != "purge":
            print("Cancelled.")
            return 1

    # Deregister the login service first, so nothing brings the daemon back
    # between here and the file removal below.
    try:
        daemon_installer.uninstall()
        print("Background service: deregistered")
    except Exception as exc:
        print(f"Background service: not deregistered ({exc})")
    try:
        daemon_installer.stop()
    except Exception:
        pass

    # A daemon started outside the service manager — which is what install.sh
    # does, and `claude-unlimited start` — is not launchd's to stop, so
    # stopping the service leaves it serving the dashboard. Stop it directly.
    _stop_running_daemon(port)

    # Before the config goes: it is the only record of which Profiles exist.
    removed = 0
    isolated_dirs = []
    try:
        for profile in load_pool().profiles:
            try:
                secret_store.delete_token(profile.id)
                removed += 1
            except Exception:
                pass
            if profile.claude_config_dir:
                isolated_dirs.append(Path(profile.claude_config_dir))
    except Exception:
        pass
    print(f"Credentials removed from the keystore: {removed}")
    _remove_isolated_claude_logins(isolated_dirs)
    _revert_desktop_config_for_purge()

    if hud_installer.is_supported():
        # Before the install root goes: nothing here needs it, but a HUD left
        # running would keep polling a daemon that no longer exists.
        hud_installer.remove()
        print(f"Removed: {hud_installer.BUNDLE_DIR / hud_installer.BUNDLE_NAME}")

    for path in (app_dir, install_root):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"Removed: {path}")
    for link in cli_links:
        if link.exists() or link.is_symlink():
            link.unlink(missing_ok=True)
            print(f"Removed: {link}")

    print()
    print("Claude Unlimited is gone. ~/.claude was left untouched.")
    return 0


def _ensure_daemon(port: int) -> bool:
    """Starts the daemon if it is not already answering. Shared by `code` and
    `ui` so the two cannot drift apart on how routing is set up."""
    if _probe_health(LOOPBACK_HOST, port):
        return True
    print(f"Daemon isn't running on {LOOPBACK_HOST}:{port} yet — starting it now…")
    try:
        _spawn_background_daemon(port)
    except OSError as exc:
        print(f"Could not start the daemon: {exc}", file=sys.stderr)
        return False
    for _ in range(30):
        if _probe_health(LOOPBACK_HOST, port):
            print("Started (not installed for auto-start — run `claude-unlimited install` for that).")
            return True
        time.sleep(0.2)
    print(f"Daemon didn't come up on {LOOPBACK_HOST}:{port} within 6s — "
          "check ~/.claude-unlimited/logs/daemon.err.log.", file=sys.stderr)
    return False


def _pool_base_url(port: int) -> str:
    """THE one place the pool's address is constructed. Everything that points
    a client at the pool — env for the CLI, the desktop app's gateway config —
    goes through here, so they cannot drift apart."""
    return f"http://{LOOPBACK_HOST}:{port}"


def _routing_env(port: int, *, token: Optional[str] = None) -> dict[str, str]:
    """The two variables that point a Claude Code client at the pool.

    THE one definition, used by `code` (which passes a token it already
    fetched) and by `ui`. Restating the pair anywhere else is how the two
    commands would end up routing differently after a change to one — a test
    asserts this file names ANTHROPIC_BASE_URL exactly once outside the
    settings-scrubbing constants."""
    return {
        "ANTHROPIC_BASE_URL": _pool_base_url(port),
        "ANTHROPIC_AUTH_TOKEN": token if token is not None else _fetch_placeholder_token(LOOPBACK_HOST, port),
    }


# The 1M-context policy lives in context_window.py (pure, shared with the
# daemon's dashboard preview). These are the launch-time halves: reading the
# installed client's version, and actually applying the decision.
from .context_window import (  # noqa: E402  - kept beside its users
    ASSUME_FIRST_PARTY_ENV,
    _one_million_decision,
    _parse_client_version,
    codex_profiles_in_route,
)


# Where `claude` lives when PATH does not say. The daemon runs under launchd /
# systemd / Task Scheduler with a minimal PATH, so `shutil.which` finds
# nothing there and the dashboard's 1M preview reported
# "client_version_unverified" on a machine with a perfectly good 2.1.278
# installed — a wrong answer, not a missing one. On Windows the installed file
# is `claude.exe`, never the bare name (confirmed on real hardware: npm/the
# updater's alias-healing put it at %USERPROFILE%\.local\bin\claude.exe) —
# without the suffix these candidates never match and the same bug resurfaces.
_CLAUDE_NAME = "claude.exe" if os.name == "nt" else "claude"
_CLAUDE_FALLBACK_PATHS = (
    Path.home() / ".local" / "bin" / _CLAUDE_NAME,
    Path.home() / ".claude" / "local" / _CLAUDE_NAME,
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
)


def _claude_executable() -> Optional[str]:
    """The `claude` binary, PATH first and then the usual install locations.
    None when it genuinely cannot be found."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _CLAUDE_FALLBACK_PATHS:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def _installed_client_version() -> Optional[tuple]:
    """The installed Claude Code version, or None when it cannot be read —
    which the policy treats as unverified, never as new enough."""
    executable = _claude_executable()
    if executable is None:
        return None
    try:
        completed = subprocess.run([executable, "--version"], capture_output=True,
                                    text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _parse_client_version(completed.stdout)


def _apply_one_million_context(pool, enabled_profiles, forced_profile) -> None:
    """Decide and apply the 1M-context policy for this launch, and say what it
    decided. Only ever ADDS the variable: the user's own environment wins, and
    a refusal is explained rather than silent."""
    mode = getattr(getattr(pool, "settings", None), "context_1m", "auto")
    version = _installed_client_version() if mode == "auto" else None
    should_set, reason = _one_million_decision(
        mode, enabled_profiles, forced_profile, os.environ, version)
    if should_set:
        os.environ[ASSUME_FIRST_PARTY_ENV] = "1"
        # Deliberately "on models that support it": the variable lifts the
        # gateway cap, it does not give Haiku or Sonnet 4.6 a window they
        # never had. Claiming a flat "1M enabled" would be wrong on exactly
        # the models a user is most likely to be running.
        if reason == "forced_1m":
            print("Full context window forced for this session — 1M on the models that have it "
                  "(Sonnet 5, Opus 4.7/4.8/5, Fable 5/5.1, Mythos 5/5.1), whatever the route.")
        else:
            print("Full context window enabled for this session — 1M on the models that have it "
                  "(Sonnet 5, Opus 4.7/4.8/5, Fable 5/5.1, Mythos 5/5.1).")
        for line in _codex_guard_lines(pool, enabled_profiles, forced_profile):
            print(line)
        return
    note = _ONE_M_REASON_NOTES.get(reason)
    if note is not None:
        print(f"Context window stays at 200K: {note}")


def _codex_guard_lines(pool, enabled_profiles, forced_profile) -> list:
    """One line per ChatGPT/Codex account the session can reach, with the
    window the gateway's capacity guard holds it to (docs/adr/0009) — so a
    1M session on a mixed pool knows where its long turns will and will not
    go. Empty when the route has no codex account."""
    codex = codex_profiles_in_route(enabled_profiles, forced_profile)
    if not codex:
        return []
    from . import openai_models
    parity = getattr(getattr(pool, "settings", None), "model_parity", None)
    lines = []
    for p in codex:
        summary = openai_models.codex_profile_windows(p, parity)
        assumed = " (window assumed — not in gpt_windows.py)" if summary["assumed"] else ""
        lines.append(f"  {p.name}: up to ~{summary['budget'] // 1000}K estimated tokens per turn on "
                     f"{'/'.join(summary['models'])}{assumed}; longer turns go to a Claude account, "
                     f"or Claude Code is asked to compact when none can take them.")
    return lines


# Only the reasons worth interrupting a launch for. "client_default",
# "user_forced_200k" and "user_already_set" are the user's own doing and need
# no commentary; "no_profiles" already fails louder elsewhere.
_ONE_M_REASON_NOTES = {
    "codex_only_route": "only ChatGPT/Codex accounts can take this session, and a GPT backend "
                        "holds about 272K. Add a Claude account to the rotation (long turns then "
                        "go there) — or choose Force 1M in Settings to set it anyway.",
    "custom_gateway_unknown": "an API profile points somewhere other than Anthropic, and we "
                              "cannot verify what context window it serves.",
    "client_version_unverified": "this Claude Code version has not been verified for it.",
}


CLAUDE_APP_BUNDLE_ID = "com.anthropic.claudefordesktop"

# --- Claude desktop app, third-party inference mode -------------------------
#
# "3p" is the app's own term for third-party inference: pointing it at a
# gateway instead of Anthropic directly. In that mode it runs from a SEPARATE
# userData directory, `Claude-3p`, with its own settings, session and bundled
# Claude Code. That separation is why searching the normal `Claude` directory
# for these settings finds nothing.
#
# Schema below is not guessed — it was read back out of the app after
# configuring it by hand through Developer > Configure Third-Party Inference.
# Where the app keeps each profile's userData, per OS. Windows is NOT
# symmetrical with macOS and the difference is not a guess - it was read off a
# configured Windows 11 install: the normal profile lives in Roaming, the 3p
# profile in Local. On Windows the app is also shipped as an MSIX package, so
# its writes to %APPDATA% are redirected into the package's LocalCache; that
# redirection is transparent in both directions (verified by writing a probe
# file into %APPDATA%\Claude and seeing it appear in the package view), which
# is why writing to the plain paths below reaches the packaged app.
CLAUDE_APP_SUPPORT = Path.home() / "Library" / "Application Support"


def _desktop_userdata_dirs():
    if sys.platform == "darwin":
        return CLAUDE_APP_SUPPORT / "Claude", CLAUDE_APP_SUPPORT / "Claude-3p"
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return roaming / "Claude", local / "Claude-3p"
    # Linux: the app's layout there has not been verified, and desktop() refuses
    # before these are used. Named anyway so nothing has to guard against None.
    config = Path.home() / ".config"
    return config / "Claude", config / "Claude-3p"


CLAUDE_1P_DIR, CLAUDE_3P_DIR = _desktop_userdata_dirs()
CU_CONFIG_NAME = "Claude Unlimited"

# Windows: the desktop app and the Claude Code CLI are BOTH called claude.exe,
# so the app's processes are recognised by where they run from - never by name
# alone, or `desktop` would try to quit the person's terminal session.
_WINDOWS_APP_PATH_MARKERS = (r"\windowsapps\claude_", r"\anthropicclaude" "\\",
                            r"\app\claude.exe")
_WINDOWS_NOT_THE_APP = ("claude-code", r"\.local" "\\" "bin" "\\")


def _powershell(script: str, timeout: float = 20.0) -> str:
    """Runs a PowerShell snippet, returning stdout ('' on any failure).

    Windows has no pgrep/osascript; process paths and the Start-menu launch
    identity both come from here."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _windows_app_pids():
    """PIDs of the desktop app, excluding the identically-named CLI."""
    out = _powershell(
        "Get-CimInstance Win32_Process -Filter \"Name='Claude.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }")
    pids = []
    for line in out.splitlines():
        pid, _, path = line.partition("|")
        path = path.strip().lower()
        if not pid.strip().isdigit() or not path:
            continue
        if any(bad in path for bad in _WINDOWS_NOT_THE_APP):
            continue
        if any(marker in path for marker in _WINDOWS_APP_PATH_MARKERS):
            pids.append(int(pid))
    return pids


def _windows_launch_target():
    """How to start the app: its Start-menu identity, else an executable.

    An MSIX/Store package cannot be launched by running its .exe out of the
    protected WindowsApps directory - it has to go through the AppsFolder
    identity, which is what the Start menu itself uses."""
    aumid = _powershell(
        "$a = Get-StartApps | Where-Object { $_.AppID -like 'Claude_*!*' } | "
        "Select-Object -First 1 -ExpandProperty AppID; if ($a) { $a }")
    if aumid:
        return ("aumid", aumid.splitlines()[0].strip())

    # Non-packaged installs (the plain .exe installer) live in the usual spots.
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    candidates = [local / "AnthropicClaude" / "Claude.exe",
                  local / "Programs" / "Claude" / "Claude.exe",
                  Path(os.environ.get("PROGRAMFILES") or "C:/Program Files") / "Claude" / "Claude.exe"]
    candidates.extend(sorted(local.glob("AnthropicClaude/app-*/Claude.exe"), reverse=True))
    for exe in candidates:
        if exe.is_file():
            return ("exe", str(exe))
    return None


def _desktop_app_running() -> bool:
    """Whether the Claude desktop app has a live process.

    It loads this configuration at startup and rewrites parts of it on exit,
    so configuring a running instance is either ignored or clobbered."""
    if sys.platform == "win32":
        return bool(_windows_app_pids())
    try:
        result = subprocess.run(["pgrep", "-f", "Claude.app/Contents/MacOS/Claude"],
                                capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _quit_desktop_app(timeout: float = 20.0) -> bool:
    """Asks the Claude desktop app to quit, and waits until it really has.

    A graceful quit, never a kill: the app rewrites parts of its configuration
    as it exits, so it has to finish doing that BEFORE we write ours — a kill
    would either lose the person's in-progress work or leave a half-written
    config that then overwrites what we put there.

    Returns False if it is still running when the timeout expires, so the
    caller can stop rather than write a configuration the app is about to
    overwrite."""
    if sys.platform == "win32":
        pids = _windows_app_pids()
        if not pids:
            return True
        # CloseMainWindow posts WM_CLOSE - the same thing clicking the window's
        # X does - so the app runs its normal shutdown. Deliberately NOT
        # taskkill /F, which would skip the config rewrite this waits for.
        _powershell(
            "Get-Process -Id {} -ErrorAction SilentlyContinue | "
            "ForEach-Object {{ $_.CloseMainWindow() | Out-Null }}".format(
                ",".join(str(pid) for pid in pids)))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _desktop_app_running():
                time.sleep(1.0)   # let its exit-writes land
                return True
            time.sleep(0.4)
        return not _desktop_app_running()

    try:
        subprocess.run(["osascript", "-e", 'tell application id "%s" to quit' % CLAUDE_APP_BUNDLE_ID],
                        capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return not _desktop_app_running()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _desktop_app_running():
            # The app's own exit writes land after the process disappears on
            # some runs; a short settle avoids racing them.
            time.sleep(1.0)
            return True
        time.sleep(0.4)
    return not _desktop_app_running()


def _desktop_app_installed() -> bool:
    """Whether the desktop app is installed at all."""
    if sys.platform == "darwin":
        return Path("/Applications/Claude.app").exists()
    if sys.platform == "win32":
        return _windows_launch_target() is not None
    return False


def _launch_desktop_app():
    """Starts the app. Returns (ok, error_message)."""
    if sys.platform == "win32":
        target = _windows_launch_target()
        if target is None:
            return False, "the app is installed but could not be located to launch"
        kind, value = target
        # explorer.exe is the documented way to launch by AppsFolder identity,
        # and it reports success regardless of what it launched - so the app is
        # confirmed by looking for its process rather than by an exit code.
        cmd = ["explorer.exe", "shell:AppsFolder\\" + value] if kind == "aumid" else [value]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        deadline = time.time() + 20
        while time.time() < deadline:
            if _desktop_app_running():
                return True, ""
            time.sleep(0.5)
        return False, "it did not start within 20 seconds"

    try:
        result = subprocess.run(["open", "-b", CLAUDE_APP_BUNDLE_ID],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, "`open` failed: " + (result.stderr or result.stdout).strip()
    return True, ""


def _desktop_paths(base: Path) -> dict:
    return {
        "desktop_config": base / "claude_desktop_config.json",
        "developer": base / "developer_settings.json",
        "library": base / "configLibrary",
        "meta": base / "configLibrary" / "_meta.json",
    }


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".cu-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _backup_desktop_config() -> None:
    """One snapshot of everything we are about to touch, taken once.

    Not overwritten on later runs: the first backup is the only one taken
    before this tool ever modified anything, so it is the only one that can
    restore the app to how the person had it."""
    backup = APP_DIR_PATH() / "claude-desktop-backup"
    if backup.exists():
        return
    backup.mkdir(parents=True, exist_ok=True)
    for base in (CLAUDE_1P_DIR, CLAUDE_3P_DIR):
        for name, path in _desktop_paths(base).items():
            if name == "library" or not path.exists():
                continue
            target = backup / base.name / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        lib = _desktop_paths(base)["library"]
        if lib.is_dir():
            for entry in lib.glob("*.json"):
                target = backup / base.name / "configLibrary" / entry.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")


def APP_DIR_PATH() -> Path:
    from .config import APP_DIR
    return Path(APP_DIR)


def _configure_desktop_app(port: int, token: str) -> str:
    """Creates (or updates) a "Claude Unlimited" inference profile in the
    desktop app and makes it the applied one.

    Returns the entry id. Everything else in the app's config library is left
    alone — a person may have other gateways configured, and replacing their
    library would be destructive."""
    paths = _desktop_paths(CLAUDE_3P_DIR)
    paths["library"].mkdir(parents=True, exist_ok=True)

    meta = _read_json(paths["meta"], {})
    entries = meta.get("entries") or []

    existing = next((e for e in entries if e.get("name") == CU_CONFIG_NAME), None)
    entry_id = existing["id"] if existing else str(uuid.uuid4())
    if existing is None:
        entries.append({"id": entry_id, "name": CU_CONFIG_NAME})

    _write_json(paths["library"] / f"{entry_id}.json", {
        "inferenceGatewayBaseUrl": _pool_base_url(port),
        "inferenceGatewayApiKey": token,
        "inferenceProvider": "gateway",
        "inferenceCredentialKind": "static",
    })
    meta["entries"] = entries
    meta["appliedId"] = entry_id
    _write_json(paths["meta"], meta)

    # Developer mode, and the third-party deployment switch. Written to both
    # profiles: the app reads the mode from its config to decide which profile
    # to run from, so a first-time switch has to be visible to the 1p profile.
    for base in (CLAUDE_1P_DIR, CLAUDE_3P_DIR):
        p = _desktop_paths(base)
        # Created rather than skipped: on a fresh install neither directory
        # exists yet, and the 1p config is precisely where the deployment
        # switch has to land for the next launch to open the 3p profile.
        base.mkdir(parents=True, exist_ok=True)
        dev = _read_json(p["developer"], {})
        dev["allowDevTools"] = True
        _write_json(p["developer"], dev)
        cfg = _read_json(p["desktop_config"], {})
        cfg["deploymentMode"] = "3p"
        _write_json(p["desktop_config"], cfg)

    return entry_id


def desktop(port: int) -> int:
    """Configures the Claude desktop app to route through the pool, and starts it.

    The app calls this third-party ("3p") inference mode: pointing it at a
    gateway instead of Anthropic. In that mode it runs from its own userData
    directory with its own settings and bundled Claude Code.

    This writes the same configuration the app's own
    Developer > Configure Third-Party Inference dialog writes, so the app is
    ready on launch rather than needing a form filled in by hand."""
    _banner()

    if sys.platform not in ("darwin", "win32"):
        print("`desktop` is supported on macOS and Windows: the app's config layout has "
              "only been verified there.", file=sys.stderr)
        return 1

    if not _desktop_app_installed():
        where = ("/Applications/Claude.app" if sys.platform == "darwin"
                 else "the Start menu or the usual install locations")
        print(f"Claude desktop app not found ({where}).", file=sys.stderr)
        print("Install it from https://claude.ai/download, or use `claude-unlimited code` "
              "for the terminal.", file=sys.stderr)
        return 1

    # The app reads this configuration at startup and rewrites parts of it as
    # it exits, so it must be fully stopped before anything is written —
    # otherwise its shutdown overwrites what we just put there.
    if _desktop_app_running():
        print("Claude is running — asking it to quit so its settings can be updated…")
        print("(any unsaved work in the app should be saved first)")
        if not _quit_desktop_app():
            print("", file=sys.stderr)
            print("Claude is still running. It may be showing a dialog, or waiting on "
                  "unsaved work.", file=sys.stderr)
            quit_hint = "Cmd-Q" if sys.platform == "darwin" else "close its window"
            print(f"Quit it yourself ({quit_hint}) and run this again — configuring it "
                  "while it runs would be overwritten when it exits.", file=sys.stderr)
            return 1
        print("Stopped.")

    if not _ensure_daemon(port):
        return 1

    enabled = [p for p in load_pool().profiles if p.enabled]
    if not enabled:
        print("WARNING: no Profile is enabled, so every request will be refused with")
        print("         \"No eligible Profile available\". Enable one in the Dashboard.")
        print("")

    token = _fetch_placeholder_token(LOOPBACK_HOST, port)
    _backup_desktop_config()
    _configure_desktop_app(port, token)
    print(f"Configured the desktop app: inference profile {CU_CONFIG_NAME!r} -> "
          f"{_pool_base_url(port)}")
    print("(previous configuration backed up — `claude-unlimited desktop --revert` undoes this)")

    launched, why = _launch_desktop_app()
    if not launched:
        print(f"Could not launch the app: {why}", file=sys.stderr)
        return 1

    print("")
    print(f"Launched. Dashboard: {_pool_base_url(port)}/")
    print("Claude Code sessions inside the app now route through the pool; the app's own")
    print("chat talks to claude.ai and is unaffected. Watch the Activity page to confirm.")
    return 0


def _desktop_dir_named(name: str):
    """Maps a backup folder name back to the profile directory it came from.

    Not `CLAUDE_APP_SUPPORT / name`: on Windows the two profiles live under
    different roots (Roaming and Local), so there is no shared parent to
    rebuild the path from."""
    for base in (CLAUDE_1P_DIR, CLAUDE_3P_DIR):
        if base.name == name:
            return base
    return None


def _restore_desktop_backup(backup: Path) -> int:
    """Copies a snapshot back over the desktop app's configuration.

    Shared by `desktop --revert` and by purge, which must undo the same change
    for the same reason. Neither checks whether the app is running — that is
    the caller's job, because they handle a running app differently."""
    restored = 0
    for profile_dir in backup.iterdir():
        if not profile_dir.is_dir():
            continue
        target_base = _desktop_dir_named(profile_dir.name)
        if target_base is None:
            continue
        for item in profile_dir.rglob("*.json"):
            target = target_base / item.relative_to(profile_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.read_text(encoding="utf-8"), encoding="utf-8")
            restored += 1
    return restored


def _revert_desktop_config_for_purge() -> None:
    """Undoes `claude-unlimited desktop`, before purge deletes the app
    directory the snapshot lives in.

    Skipping this would leave the desktop app pointed at a gateway that is
    about to stop existing, with the only snapshot that could restore it
    deleted moments later. Best-effort throughout: purge must finish even when
    the app will not quit, so in that case the snapshot is moved somewhere it
    survives and the person is told where."""
    backup = APP_DIR_PATH() / "claude-desktop-backup"
    if not backup.is_dir():
        return

    if _desktop_app_running():
        print("Claude desktop app is running — asking it to quit so its "
              "configuration can be restored…")
        if not _quit_desktop_app():
            keep = Path.home() / "claude-unlimited-desktop-backup"
            try:
                if keep.exists():
                    shutil.rmtree(keep, ignore_errors=True)
                shutil.move(str(backup), str(keep))
            except OSError as exc:
                print(f"Desktop app: could not preserve its backup ({exc})", file=sys.stderr)
                return
            print("Desktop app: still running, so its configuration was left "
                  "pointing at Claude Unlimited.", file=sys.stderr)
            print(f"             Its original settings were saved to {keep} —", file=sys.stderr)
            # Both directories are named: on Windows they live under
            # different roots, so one path would send people to the wrong place.
            print(f"             quit Claude and copy '{CLAUDE_1P_DIR.name}' back into "
                  f"{CLAUDE_1P_DIR.parent}", file=sys.stderr)
            print(f"             and '{CLAUDE_3P_DIR.name}' into {CLAUDE_3P_DIR.parent} "
                  "to undo it.", file=sys.stderr)
            return

    try:
        restored = _restore_desktop_backup(backup)
    except OSError as exc:
        print(f"Desktop app: configuration not restored ({exc})", file=sys.stderr)
        return
    print(f"Desktop app: restored {restored} configuration file(s) to how they were")


def desktop_revert() -> int:
    """Restores the desktop app's configuration from the snapshot taken before
    this tool first changed it."""
    _banner()
    backup = APP_DIR_PATH() / "claude-desktop-backup"
    if not backup.is_dir():
        print("No backup found — nothing to restore.", file=sys.stderr)
        return 1
    if _desktop_app_running():
        print("Quit Claude (Cmd-Q) first, or it will rewrite these files on exit.",
              file=sys.stderr)
        return 1

    restored = _restore_desktop_backup(backup)
    print(f"Restored {restored} configuration file(s).")
    print("Note: an inference profile named "
          f"{CU_CONFIG_NAME!r} may remain in the app's list — remove it there if you "
          "no longer want it.")
    return 0


def code(port: int, claude_args: list[str], profile_arg: Optional[str] = None,
         distribute: bool = False) -> int:
    _banner()
    # Self-heal the CLI launchers on every `code` run. This is the RELIABLE
    # trigger: unlike the daemon-startup heal (which only fires if an update
    # actually restarts the daemon — older updaters didn't), this runs the
    # freshly-installed code via the ~/.local/bin/claude-unlimited -> venv
    # symlink, so simply running `claude-unlimited code` once after an update
    # creates the missing `cu` link. Best-effort; never blocks a launch.
    try:
        updater.ensure_cli_aliases()
    except Exception:
        pass
    # The HUD reaches an existing install the same way, and for the same
    # reason: it has to be the NEW code that installs it.
    hud_installer.ensure_installed_quietly(__version__)
    if not shutil.which("claude"):
        print("Claude Code CLI (`claude`) not found on PATH. Install/update Claude Code first.", file=sys.stderr)
        return 1

    if not _ensure_daemon(port):
        return 1

    # A daemon can run for weeks while providers ship models mid-day; a
    # session launch is the moment fresh names/prices actually matter.
    _request_models_refresh(LOOPBACK_HOST, port)

    # Picking a specific Profile pins THIS terminal session to it (see
    # session_tokens.py and gateway.py's forced_profile_id); other
    # concurrent sessions keep rotating normally. The picker only appears
    # when there is a real choice: zero or one enabled Profile has nothing
    # to pick between, and a non-interactive stdin must never block on a
    # prompt it cannot answer. Both fall through to "Rotated accounts".
    forced_profile = None
    try:
        pool = load_pool()
        enabled_profiles = pool.enabled_profiles()
        # Settings → "Balance sessions and subagents across accounts" makes --distribute the default.
        # The daemon enforces this on its own (gateway._branch_decision reads
        # the same setting), but the CLI has to know too: otherwise the picker
        # below would pin the session and quietly override it.
        distribute = distribute or pool.settings.distribute_sessions_default
    except Exception:
        # `pool` must still be bound: everything below this block reads it, and
        # a NameError here would turn "your config could not be read" into a
        # crash. None reads as "no settings", which every consumer treats as
        # the conservative default.
        pool = None
        enabled_profiles = []

    if profile_arg:
        forced_profile = _match_profile(enabled_profiles, profile_arg)
        if forced_profile is None:
            print(f"No enabled profile matches --profile {profile_arg!r}.", file=sys.stderr)
            if enabled_profiles:
                print("Available: " + ", ".join(p.name for p in enabled_profiles), file=sys.stderr)
            return 1
    elif distribute:
        # --distribute already answered "which account?" — with "all of them,
        # one per branch". Prompting would pin the session and silently undo
        # the flag.
        pass
    elif len(enabled_profiles) > 1 and sys.stdin.isatty():
        try:
            forced_profile = _prompt_profile_choice(enabled_profiles)
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.", file=sys.stderr)
            return 1

    try:
        if forced_profile is not None:
            token = _fetch_session_token(LOOPBACK_HOST, port, forced_profile.id)
        elif distribute:
            token = _fetch_distribute_token(LOOPBACK_HOST, port)
        else:
            token = _fetch_placeholder_token(LOOPBACK_HOST, port)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        print(f"Could not fetch the local credential from the daemon: {exc}", file=sys.stderr)
        return 1

    os.environ.update(_routing_env(port, token=token))
    # Lets Claude Code fetch GET /v1/models from the pool. NOTE this alone does
    # NOT surface our parity labels: v2.1.263's picker treats discovery as an
    # availability filter over its OWN hard-coded model table and renders the
    # built-in display_name for any id it recognises (all of ours), discarding
    # the `<Claude> | <GPT>` display_name we send. The picker labels are set by
    # _apply_model_labels below (the ANTHROPIC_DEFAULT_*_MODEL_NAME path, which
    # the same table DOES honour). We still enable discovery: it is harmless and
    # forward-compatible for any id the built-in table lacks. setdefault so a
    # user who exports 0 can opt out.
    os.environ.setdefault("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", "1")
    _apply_one_million_context(pool, enabled_profiles, forced_profile)
    _apply_model_labels(forced_profile, enabled_profiles,
                        host=LOOPBACK_HOST, port=port, token=token)
    if forced_profile is not None:
        print(f"Routing through Claude Unlimited at {LOOPBACK_HOST}:{port}, pinned to {forced_profile.name} "
              f"— launching claude…\n")
    elif distribute:
        print(f"Routing through Claude Unlimited at {LOOPBACK_HOST}:{port}, balancing across accounts "
              f"(each new agent starts on the least-busy one) — launching claude…\n")
    else:
        print(f"Routing through Claude Unlimited at {LOOPBACK_HOST}:{port} — launching claude…\n")
    # execvp replaces this process image outright: it never returns and
    # never runs Python's exit-time flush, so anything still sitting in
    # stdout's buffer (whenever stdout isn't a TTY) would vanish.
    sys.stdout.flush()
    sys.stderr.flush()
    argv = ["claude", *_status_line_args(port, claude_args), *claude_args]
    if os.name == "nt":
        # Windows has no real exec: os.execvp spawns a child and exits the
        # parent, so the shell regains the console while claude's TUI is still
        # attached — interleaved, broken I/O. And a `.cmd` shim can't be
        # exec'd at all. Run it as a child, wait, and return its exit code.
        return _run_tool(argv).returncode
    # POSIX: replace this process image outright — the TUI takes over the
    # terminal and there is no lingering parent. Never returns on success.
    os.execvp(_resolve_launcher("claude"), argv)
    return 0  # unreachable: execvp replaces this process on success


def install(port: int) -> int:
    _banner()

    # Whatever is already on the port has to go first, whichever shape it is.
    # Registering the service does not touch a daemon the service manager does
    # not own, so a detached one (install.sh's fallback, or `start` in a
    # terminal) keeps the port, the replacement cannot bind, and the upgrade
    # silently leaves the OLD version serving while every health check passes.
    running = _running_version(LOOPBACK_HOST, port)
    if running is not None and running != __version__:
        print(f"Stopping the running daemon (version {running})…")
    if running is not None:
        try:
            daemon_installer.stop()
        except daemon_installer.DaemonInstallerError:
            pass  # not service-managed, or not running under it
        _stop_running_daemon(port)

    try:
        daemon_installer.install(port)
    except daemon_installer.DaemonInstallerError as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 1

    # Verify rather than announce. "Installed" used to be printed on the
    # strength of the files having been written, which is exactly the claim
    # that was wrong when an older daemon still held the port.
    seen = _wait_for_version(LOOPBACK_HOST, port, __version__)
    if seen == __version__:
        print(f"Installed — the daemon will now start automatically on login, on port {port}.")
        print(f"Running version {__version__} at http://{LOOPBACK_HOST}:{port}/")
    elif seen is None:
        print(f"Installed, but nothing is answering on port {port} yet.", file=sys.stderr)
        print("Check `claude-unlimited status`, or start it with `claude-unlimited start`.", file=sys.stderr)
        return 1
    else:
        print(f"Installed {__version__}, but port {port} is still served by version {seen}.", file=sys.stderr)
        print("Something else is holding the port. Stop it, then run "
              "`claude-unlimited restart`.", file=sys.stderr)
        return 1
    print("Run `claude-unlimited status` to check it, or `claude-unlimited uninstall` to remove it.")
    return 0


def hud(action: str) -> int:
    """Install, inspect or remove the macOS HUD by hand.

    Everything here happens on its own anyway — on install, on update, and on
    the first `cu code` after either. This exists for the person who removed it
    and wants it back, or who wants it gone without purging the whole tool."""
    _banner()
    if not hud_installer.is_supported():
        print("HUD - Heads-Up Display is macOS only.")
        return 1

    bundle = hud_installer.BUNDLE_DIR / hud_installer.BUNDLE_NAME
    if action == "remove":
        hud_installer.remove()
        print(f"Removed {bundle} and its login item. It stays removed — "
              "`cu hud install` brings it back.")
        return 0

    if action == "status":
        print(f"Bundle: {bundle if bundle.is_dir() else 'not installed'}")
        print(f"Starts at login: {'yes' if hud_installer.LAUNCH_AGENT.is_file() else 'no'}")
        try:
            stamp = hud_installer.STAMP.read_text(encoding="utf-8").strip()
        except OSError:
            stamp = "unknown"
        print(f"Installed version: {stamp}")
        return 0 if bundle.is_dir() else 1

    # `install` re-installs even when the stamp says it is current: someone
    # typing this command is asking for the download, not for a status check.
    result = hud_installer.ensure_installed(version=__version__, force=True)
    messages = {
        "installed": f"Installed {bundle} — it starts with you from now on.",
        "unsupported": "HUD - Heads-Up Display is macOS only.",
        "no_digest": "This release does not publish a HUD build yet.",
        "download_failed": "Could not download the HUD. Check your connection and try again.",
        "digest_mismatch": "The download did not match the checksum this release ships. Nothing was installed.",
        "bad_archive": "The download was not a usable HUD bundle. Nothing was installed.",
        "failed": "Could not write into ~/Applications.",
    }
    print(messages.get(result, result))
    return 0 if result == "installed" else 1


def uninstall() -> int:
    _banner()
    try:
        daemon_installer.uninstall()
    except daemon_installer.DaemonInstallerError as exc:
        print(f"Uninstall failed: {exc}", file=sys.stderr)
        return 1
    print("Uninstalled — the daemon will no longer start automatically on login.")
    return 0


def service_start() -> int:
    _banner()
    try:
        daemon_installer.start()
    except daemon_installer.DaemonInstallerError as exc:
        print(f"Start failed: {exc}", file=sys.stderr)
        return 1
    print("Started.")
    return 0


def service_stop() -> int:
    _banner()
    try:
        daemon_installer.stop()
    except daemon_installer.DaemonInstallerError as exc:
        print(f"Stop failed: {exc}", file=sys.stderr)
        return 1
    print("Stopped.")
    return 0




def add_account() -> int:
    """`claude-unlimited add-account` (alias `ac`): logs Claude Code into an
    account under its own isolated CLAUDE_CONFIG_DIR.

    The isolated directory gives that login a separate session, so this
    never logs out or otherwise touches the account already signed into
    plain `claude` on this machine. The directory is remembered on the
    resulting Profile (Profile.claude_config_dir), so the same slot is
    reused for that account later instead of spawning a fresh one.

    This is the only supported way to add an OAuth Profile from a
    terminal."""
    _banner()

    if not shutil.which("claude"):
        print("`claude` was not found on PATH — this command drives the real Claude Code CLI directly.",
              file=sys.stderr)
        return 1

    config_dir = CLAUDE_ACCOUNTS_DIR / secrets.token_hex(8)
    config_dir.mkdir(parents=True, exist_ok=True)

    print("Opening your browser to log into the account you want to add — this uses an isolated")
    print("Claude Code session, so it will NOT log out or affect any other account already signed")
    print("into `claude` on this machine.\n")

    login_env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    login_proc = _run_tool(["claude", "auth", "login"], env=login_env)
    if login_proc.returncode != 0:
        print("\n`claude auth login` did not complete successfully.", file=sys.stderr)
        return 1

    print("\nResolving the newly logged-in account...")
    try:
        imported = anthropic_oauth.read_claude_code_credentials(config_dir=config_dir)
        account = anthropic_oauth.fetch_account_profile(imported.access_token)
    except (anthropic_oauth.CredentialImportError, anthropic_oauth.ProfileLookupError) as exc:
        print(f"Logged in, but could not read/resolve the new account: {exc}", file=sys.stderr)
        return 1

    name = account.email or "Imported Claude account"
    try:
        profile, reused = profile_repo.upsert_oauth_profile(
            name=name, account_uuid=account.account_uuid, credential=imported.access_token,
            plan=anthropic_oauth.plan_from_account(account),
            refresh_token=imported.refresh_token, expires_at=imported.expires_at,
            claude_config_dir=str(config_dir),
        )
    except (profile_repo.ValidationError, profile_repo.ProfileRepositoryError) as exc:
        print(f"Logged in, but could not save the profile: {exc}", file=sys.stderr)
        return 1

    if not reused:
        # So the Dashboard shows real usage for it straight away, rather than
        # a blank card until this account first serves a request.
        _prime_via_daemon(profile.id)

    print(f"\n{'Refreshed existing profile' if reused else 'Added profile'}: {profile.name}")
    if account.org_name:
        print(f"  Organization: {account.org_name}")
    tier = "Max" if account.has_claude_max else "Pro" if account.has_claude_pro else "unknown tier"
    print(f"  Plan: {tier}")
    print("\nManage priority, threshold, and everything else for it from the Dashboard.")
    return 0




def add_codex_account() -> int:
    """`claude-unlimited add-codex-account`: logs into a ChatGPT/Codex
    subscription via the `codex` CLI's browser OAuth flow, under an isolated
    CODEX_HOME so it never touches another Codex login on this machine.
    Mirrors add_account()'s isolated CLAUDE_CONFIG_DIR.

    After that one interactive step the daemon never shells out to `codex`
    again for this Profile: requests and token refreshes go through
    openai_bridge.py's direct HTTPS calls. The isolated CODEX_HOME only
    holds the auth.json this function reads once and re-encodes into
    secret_store via openai_credential.py, rather than leaving the
    credential in the plaintext file `codex login` writes."""
    _banner()
    if not shutil.which("codex"):
        print("Codex CLI (`codex`) was not found on PATH — this command drives it directly for the "
              "one-time login step. Install it first: https://github.com/openai/codex", file=sys.stderr)
        return 1

    config_dir = CODEX_ACCOUNTS_DIR / secrets.token_hex(8)
    config_dir.mkdir(parents=True, exist_ok=True)

    print("Opening your browser to log into the ChatGPT/Codex account you want to add — this uses")
    print("an isolated CODEX_HOME, so it will NOT affect any other Codex login on this machine.\n")

    # Full environment plus the CODEX_HOME override — matching the Claude login
    # flow. A stripped {PATH,HOME} env broke the browser handshake off macOS:
    # Linux `codex login` needs DISPLAY/WAYLAND_DISPLAY/DBUS to open a browser,
    # and Windows needs USERPROFILE/APPDATA/SystemRoot for TLS. CODEX_HOME is
    # what isolates the login; nothing else needs stripping.
    login_env = {**os.environ, "CODEX_HOME": str(config_dir)}
    login_proc = _run_tool(["codex", "login"], env=login_env)
    if login_proc.returncode != 0:
        print("\n`codex login` did not complete successfully.", file=sys.stderr)
        return 1

    print("\nResolving the newly logged-in account...")
    from . import openai_credential

    auth_json_path = config_dir / "auth.json"
    try:
        auth_data = json.loads(auth_json_path.read_text(encoding="utf-8"))
        tokens = auth_data["tokens"]
        access_token = tokens["access_token"]
        account_id = tokens["account_id"]
        refresh_token = tokens.get("refresh_token")
        id_token = tokens.get("id_token")
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"Logged in, but could not read the new account's credentials: {exc}", file=sys.stderr)
        return 1

    name = (openai_credential.chatgpt_email(id_token) if id_token else None) or "ChatGPT/Codex account"
    plan = openai_credential.chatgpt_plan_type(id_token) if id_token else None
    encoded = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token=access_token, refresh_token=refresh_token, account_id=account_id, id_token=id_token,
    ))

    try:
        profile, reused = profile_repo.upsert_codex_profile(
            name=name, account_id=account_id, encoded_credential=encoded, plan=plan, codex_home=str(config_dir),
        )
    except (profile_repo.ValidationError, profile_repo.ProfileRepositoryError) as exc:
        print(f"Logged in, but could not save the profile: {exc}", file=sys.stderr)
        return 1

    if not reused:
        _prime_via_daemon(profile.id)

    print(f"\n{'Refreshed existing profile' if reused else 'Added profile'}: {profile.name}")
    if plan:
        print(f"  Plan: {plan}")
    print("\nManage priority, threshold, and everything else for it from the Dashboard.")
    return 0


def _prime_via_daemon(profile_id: str, port: int = DEFAULT_PORT, timeout: float = 25.0) -> None:
    """Asks a running daemon to record usage for a just-added Profile.

    Usage comes from response headers, so a Profile created here shows blank
    in the Dashboard until the account happens to serve a request. The daemon
    owns that state and this process does not, so the work has to go through
    it. Silent and best-effort: no daemon running, or an unreachable account,
    just means the Dashboard fills in on first real use."""
    try:
        with urllib.request.urlopen(f"http://{LOOPBACK_HOST}:{port}/api/status", timeout=2.0) as resp:
            token = json.loads(resp.read()).get("csrf_token")
        if not token:
            return
        req = urllib.request.Request(
            f"http://{LOOPBACK_HOST}:{port}/api/profiles/{profile_id}/test",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "X-CSRF-Token": token},
        )
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:  # noqa: BLE001 - priming never blocks adding an account
        return


def _fetch_live_profiles(host: str, port: int, timeout: float = 2.0) -> Optional[list]:
    """Live Profile state from the running daemon's GET /api/profiles, or
    None if it isn't reachable, so reauth() can fall back to listing every
    OAuth Profile from config instead of blocking."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/profiles", timeout=timeout) as resp:
            return json.loads(resp.read())["profiles"]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return None


def reauth(port: int) -> int:
    """`claude-unlimited reauth`: re-authenticates an OAuth Profile,
    defaulting to whichever ones the running daemon reports as AUTH_INVALID,
    with the same interactive picker `code --profile` uses.

    Reuses the Profile's isolated claude_config_dir, set when it was added,
    so re-authenticating logs back into the same account rather than an
    ambiguous fresh session. upsert_oauth_profile() then matches the
    freshly-logged-in account back to this Profile by account_uuid and
    refuses to overwrite it if a different account was used by mistake."""
    _banner()
    if not shutil.which("claude"):
        print("`claude` was not found on PATH — this command drives the real Claude Code CLI directly.",
              file=sys.stderr)
        return 1

    try:
        pool = load_pool()
    except Exception as exc:
        print(f"Could not read the Profile pool: {exc}", file=sys.stderr)
        return 1

    oauth_profiles = [p for p in pool.profiles if p.kind == "oauth"]
    if not oauth_profiles:
        print("No OAuth (subscription) Profiles are configured yet — run `claude-unlimited add-account` first.")
        return 0

    live = _fetch_live_profiles(LOOPBACK_HOST, port)
    if live is not None:
        needs_reauth_ids = {item["id"] for item in live if item.get("state") == "auth_invalid"}
        candidates = [p for p in oauth_profiles if p.id in needs_reauth_ids]
        if not candidates:
            print("No OAuth Profile currently needs re-authentication.")
            return 0
    else:
        print("Could not reach the daemon to check which Profiles actually need re-auth — "
              "showing every OAuth Profile instead.\n")
        candidates = oauth_profiles

    if len(candidates) == 1:
        target = candidates[0]
        print(f"Re-authenticating {target.name}…\n")
    else:
        print("Which Profile needs re-authenticating?\n")
        for i, p in enumerate(candidates, start=1):
            print(f"  [{i}] {p.name}")
        print()
        try:
            raw = input(f"Choice [1-{len(candidates)}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.", file=sys.stderr)
            return 1
        try:
            idx = int(raw)
        except ValueError:
            print(f"Not a number: {raw!r}.", file=sys.stderr)
            return 1
        if not (1 <= idx <= len(candidates)):
            print(f"{idx} isn't one of the choices.", file=sys.stderr)
            return 1
        target = candidates[idx - 1]

    # A Profile added by manual paste or "Import current login" has no
    # isolated dir of its own, so give it a fresh one the way add-account
    # does for a new Profile.
    config_dir = (Path(target.claude_config_dir) if target.claude_config_dir
                  else CLAUDE_ACCOUNTS_DIR / secrets.token_hex(8))
    config_dir.mkdir(parents=True, exist_ok=True)

    print("Opening your browser to log back into this account — this uses an isolated")
    print("Claude Code session, so it will NOT log out or affect any other account already signed")
    print("into `claude` on this machine.\n")

    login_env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    login_proc = _run_tool(["claude", "auth", "login"], env=login_env)
    if login_proc.returncode != 0:
        print("\n`claude auth login` did not complete successfully.", file=sys.stderr)
        return 1

    print("\nResolving the account...")
    try:
        imported = anthropic_oauth.read_claude_code_credentials(config_dir=config_dir)
        account = anthropic_oauth.fetch_account_profile(imported.access_token)
    except (anthropic_oauth.CredentialImportError, anthropic_oauth.ProfileLookupError) as exc:
        print(f"Logged in, but could not read/resolve the account: {exc}", file=sys.stderr)
        return 1

    if target.account_uuid and account.account_uuid != target.account_uuid:
        print(
            f"\nYou logged into a DIFFERENT account than {target.name} was originally added with — "
            "refusing to overwrite it. Run `claude-unlimited add-account` instead if you meant to add "
            "this as a new Profile.",
            file=sys.stderr,
        )
        return 1

    try:
        profile, _reused = profile_repo.upsert_oauth_profile(
            name=target.name, account_uuid=account.account_uuid, credential=imported.access_token,
            plan=anthropic_oauth.plan_from_account(account),
            refresh_token=imported.refresh_token, expires_at=imported.expires_at,
            claude_config_dir=str(config_dir),
        )
    except (profile_repo.ValidationError, profile_repo.ProfileRepositoryError) as exc:
        print(f"Logged in, but could not save the profile: {exc}", file=sys.stderr)
        return 1

    print(f"\n{profile.name} is re-authenticated — the daemon will pick it back up automatically.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-unlimited", add_help=True,
        epilog="Also available as `cu` — every command works under both names "
               "(e.g. `cu code`, `cu status`, `cu doctor`).")
    sub = parser.add_subparsers(dest="cmd")
    start_p = sub.add_parser("start", help="run the daemon in the foreground")
    start_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("status", help="check whether the daemon is running")
    sub.add_parser("doctor", help="verify installation and configuration")
    sub.add_parser("add-account", aliases=["ac"],
                    help="log into a Claude account via an isolated Claude Code session "
                         "(doesn't affect other logged-in accounts) and add it as a Profile")
    sub.add_parser("add-codex-account", aliases=["aca"],
                    help="log into a ChatGPT/Codex subscription via an isolated "
                         "session and add it as a codex-kind Profile")
    reauth_p = sub.add_parser("reauth", help="re-authenticate an OAuth Profile that needs it "
                                              "(defaults to whichever ones the daemon reports as needing it)")
    reauth_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    desktop_p = sub.add_parser("desktop", help="configure the Claude desktop app to use the pool, then launch it")
    desktop_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    desktop_p.add_argument("--revert", action="store_true",
                            help="restore the desktop app's previous configuration and exit")

    code_p = sub.add_parser("code", help="start the daemon if needed, then launch `claude` routed through it")
    code_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    code_pin = code_p.add_mutually_exclusive_group()
    code_pin.add_argument("--profile", metavar="NAME_OR_ID", default=None,
                           help="pin this session to one Profile by name or id, skipping the interactive picker")
    code_pin.add_argument("--distribute", action="store_true",
                           help="balance this session across accounts: the main agent and each subagent start "
                                "on the least-busy Profile and stay there, each keeping its own prompt cache warm")
    # Deliberately no positional for claude's own args: nargs=REMAINDER
    # fails as soon as the first passthrough token looks like a flag (e.g.
    # `claude-unlimited code --model opus`, where argparse matches --model
    # against code_p's options first). parse_known_args() below is what
    # lets unrecognized arguments fall through to `claude` untouched.
    install_p = sub.add_parser("install", help="register the daemon to start automatically on login")
    install_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("uninstall", help="stop the daemon from starting automatically on login")
    sub.add_parser("service-start", help="start the installed background daemon now")
    sub.add_parser("service-stop", help="stop the installed background daemon")
    hud_p = sub.add_parser("hud", help="install, check or remove the macOS HUD (it installs itself by default)")
    hud_p.add_argument("action", nargs="?", default="status", choices=("install", "status", "remove"))
    restart_p = sub.add_parser("restart", help="stop and start the daemon, service-managed or not")
    restart_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    purge_parser = sub.add_parser(
        "purge",
        help="remove Claude Unlimited and everything it created, including stored credentials")
    purge_parser.add_argument("--yes", action="store_true",
                               help="skip the confirmation prompt")
    purge_parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    args, unknown = parser.parse_known_args(argv)
    if args.cmd == "start":
        return start(args.port)
    if args.cmd == "status":
        return status()
    if args.cmd == "doctor":
        return doctor()
    if args.cmd in ("add-account", "ac"):
        return add_account()
    if args.cmd in ("add-codex-account", "aca"):
        return add_codex_account()
    if args.cmd == "reauth":
        return reauth(args.port)
    if args.cmd == "code":
        return code(args.port, unknown, profile_arg=args.profile, distribute=args.distribute)
    if args.cmd == "desktop":
        return desktop_revert() if args.revert else desktop(args.port)
    if args.cmd == "install":
        return install(args.port)
    if args.cmd == "uninstall":
        return uninstall()
    if args.cmd == "service-start":
        return service_start()
    if args.cmd == "service-stop":
        return service_stop()
    if args.cmd == "hud":
        return hud(args.action)
    if args.cmd == "restart":
        return restart(args.port)
    if args.cmd == "purge":
        return purge(args.port, assume_yes=args.yes)

    parser.print_help()
    return 0
