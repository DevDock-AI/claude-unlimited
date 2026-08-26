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
from . import i18n
from . import profiles as profile_repo
from .config import ensure_app_dir, load_pool
from .daemon import DEFAULT_PORT, LOOPBACK_HOST, run_foreground


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
        import claude_unlimited.secret_store  # noqa: F401

        print("Secret store: OK — macOS Keychain backend loaded")
    except Exception as exc:
        print(f"Secret store: MISSING — {exc}")
        ok = False

    ensure_app_dir()
    pool = load_pool()
    print(f"Config dir: OK — {pool.shared_claude_dir}")
    print(f"Profiles configured: {len(pool.profiles)}")
    if not pool.profiles:
        print("  (none yet — run `claude-unlimited add-account`, or add one from the Dashboard)")

    print("Live proxy: ready — rotation, credential substitution, and usage tracking active.")

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
    out_log = open(log_dir / "daemon.out.log", "a")
    err_log = open(log_dir / "daemon.err.log", "a")
    subprocess.Popen(
        [sys.executable, "-m", "claude_unlimited", "start", "--port", str(port)],
        stdout=out_log, stderr=err_log, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def _fetch_session_token(host: str, port: int, profile_id: str, timeout: float = 2.0) -> str:
    url = f"http://{host}:{port}/api/session-token?profile_id={urllib.parse.quote(profile_id)}"
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
_MODEL_TIER_IDS = {
    "FABLE": "claude-fable-5",
    "OPUS": "claude-opus-5",
    "SONNET": "claude-sonnet-5",
    "HAIKU": "claude-haiku-4-5-20251001",
}

# Pinned to a codex Profile: every request this session makes is served by
# OpenAI, so name the real backing model outright.
_CODEX_MODEL_LABELS = {
    "FABLE": ("GPT-5.6 Sol", "Served by Codex · reasoning: max"),
    "OPUS": ("GPT-5.6 Sol", "Served by Codex · reasoning: high"),
    "SONNET": ("GPT-5.6 Terra", "Served by Codex · reasoning: medium"),
    "HAIKU": ("GPT-5.6 Luna", "Served by Codex · reasoning: low"),
}

# Rotated across a pool that mixes providers: which provider serves a given
# request is decided per request and shifts as quotas move, while the picker
# is read once at launch and never updated. Naming BOTH models a tier maps
# to stays accurate whichever one serves, and still says what is being
# picked; a provider-neutral tier word would name no model at all.
_MIXED_MODEL_LABELS = {
    "FABLE": ("Fable 5 / GPT-5.6 Sol", "Whichever account is active · Codex reasoning: max"),
    "OPUS": ("Opus 5 / GPT-5.6 Sol", "Whichever account is active · Codex reasoning: high"),
    "SONNET": ("Sonnet 5 / GPT-5.6 Terra", "Whichever account is active · Codex reasoning: medium"),
    "HAIKU": ("Haiku 4.5 / GPT-5.6 Luna", "Whichever account is active · Codex reasoning: low"),
}


def _apply_model_labels(forced_profile, enabled_profiles=None) -> None:
    """Relabel Claude Code's `/model` picker to match what will really serve.

    The picker is built entirely client-side from these env vars, read once
    when claude starts, and the proxy is never consulted. The labels are
    therefore fixed for the life of the process and cannot track rotation.

    Three cases:
      * pinned to codex -> real GPT model names; the pin holds for the whole
        session (a pinned request that can't be served errors rather than
        rotating), so these stay true.
      * pinned to claude/api, or a pool with no codex Profile -> leave Claude
        Code's native labels alone.
      * rotated across a mixed pool -> label both models a tier maps to,
        since no single vendor name stays correct across a rotation.

    Never overrides a value already set in the environment."""
    kind = getattr(forced_profile, "kind", None)
    if forced_profile is not None:
        labels = _CODEX_MODEL_LABELS if kind == "codex" else None
    else:
        has_codex = any(getattr(p, "kind", None) == "codex" for p in (enabled_profiles or []))
        # An all-Claude pool never mislabels anything; leave it native.
        labels = _MIXED_MODEL_LABELS if has_codex else None
    if labels is None:
        return
    for tier, (name, description) in labels.items():
        for suffix, value in (("", _MODEL_TIER_IDS[tier]), ("_NAME", name), ("_DESCRIPTION", description)):
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
            if "statusLine" in json.loads(f.read_text()):
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
            env = (json.loads(f.read_text()) or {}).get("env") or {}
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
    """Removes the Keychain entries created by `add-account`'s isolated logins.

    Those are written by Claude Code itself, under a service name derived from
    the isolated directory we handed it, so they are ours to clean up and would
    otherwise outlive everything else.

    The derivation is what makes this safe: each name is
    "Claude Code-credentials-<hash of one of our own directories>". The user's
    real login is the un-suffixed "Claude Code-credentials", which no directory
    of ours can hash to, so it can never be selected here."""
    if sys.platform != "darwin" or not config_dirs:
        return
    from .anthropic_oauth import MACOS_KEYCHAIN_SERVICE, isolated_macos_keychain_service

    removed = 0
    for config_dir in config_dirs:
        service = isolated_macos_keychain_service(config_dir)
        if service == MACOS_KEYCHAIN_SERVICE:
            continue  # unreachable by construction; refuse anyway
        try:
            result = subprocess.run(
                ["security", "delete-generic-password", "-s", service],
                capture_output=True, timeout=10, check=False)
            if result.returncode == 0:
                removed += 1
        except (OSError, subprocess.SubprocessError):
            pass
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
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pass

    if pid:
        for sig in (signal.SIGTERM, signal.SIGKILL):
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
            for sig in (signal.SIGTERM, signal.SIGKILL):
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
    cli_link = Path.home() / ".local" / "bin" / "claude-unlimited"

    _banner()
    print("This removes Claude Unlimited and everything it created:")
    print(f"  - stored credentials for every Profile (from your OS keystore)")
    print(f"  - {app_dir}  (config, usage history, activity log, isolated account sessions)")
    print(f"  - {install_root}  (the app and its virtualenv)")
    print(f"  - {cli_link}")
    print("  - the background service registration, if installed")
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

    for path in (app_dir, install_root):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"Removed: {path}")
    if cli_link.exists() or cli_link.is_symlink():
        cli_link.unlink(missing_ok=True)
        print(f"Removed: {cli_link}")

    print()
    print("Claude Unlimited is gone. ~/.claude was left untouched.")
    return 0


def code(port: int, claude_args: list[str], profile_arg: Optional[str] = None) -> int:
    _banner()
    if not shutil.which("claude"):
        print("Claude Code CLI (`claude`) not found on PATH. Install/update Claude Code first.", file=sys.stderr)
        return 1

    if not _probe_health(LOOPBACK_HOST, port):
        print(f"Daemon isn't running on {LOOPBACK_HOST}:{port} yet — starting it now…")
        try:
            _spawn_background_daemon(port)
        except OSError as exc:
            print(f"Could not start the daemon: {exc}", file=sys.stderr)
            return 1
        for _ in range(30):
            if _probe_health(LOOPBACK_HOST, port):
                break
            time.sleep(0.2)
        else:
            print(
                f"Daemon didn't come up on {LOOPBACK_HOST}:{port} within 6s — "
                "check ~/.claude-unlimited/logs/daemon.err.log.",
                file=sys.stderr,
            )
            return 1
        print(f"Started (not installed for auto-start — run `claude-unlimited install` for that).")

    # Picking a specific Profile pins THIS terminal session to it (see
    # session_tokens.py and gateway.py's forced_profile_id); other
    # concurrent sessions keep rotating normally. The picker only appears
    # when there is a real choice: zero or one enabled Profile has nothing
    # to pick between, and a non-interactive stdin must never block on a
    # prompt it cannot answer. Both fall through to "Rotated accounts".
    forced_profile = None
    try:
        enabled_profiles = load_pool().enabled_profiles()
    except Exception:
        enabled_profiles = []

    if profile_arg:
        forced_profile = _match_profile(enabled_profiles, profile_arg)
        if forced_profile is None:
            print(f"No enabled profile matches --profile {profile_arg!r}.", file=sys.stderr)
            if enabled_profiles:
                print("Available: " + ", ".join(p.name for p in enabled_profiles), file=sys.stderr)
            return 1
    elif len(enabled_profiles) > 1 and sys.stdin.isatty():
        try:
            forced_profile = _prompt_profile_choice(enabled_profiles)
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.", file=sys.stderr)
            return 1

    try:
        if forced_profile is not None:
            token = _fetch_session_token(LOOPBACK_HOST, port, forced_profile.id)
        else:
            token = _fetch_placeholder_token(LOOPBACK_HOST, port)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        print(f"Could not fetch the local credential from the daemon: {exc}", file=sys.stderr)
        return 1

    os.environ["ANTHROPIC_BASE_URL"] = f"http://{LOOPBACK_HOST}:{port}"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = token
    _apply_model_labels(forced_profile, enabled_profiles)
    if forced_profile is not None:
        print(f"Routing through Claude Unlimited at {LOOPBACK_HOST}:{port}, pinned to {forced_profile.name} "
              f"— launching claude…\n")
    else:
        print(f"Routing through Claude Unlimited at {LOOPBACK_HOST}:{port} — launching claude…\n")
    # execvp replaces this process image outright: it never returns and
    # never runs Python's exit-time flush, so anything still sitting in
    # stdout's buffer (whenever stdout isn't a TTY) would vanish.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("claude", ["claude", *_status_line_args(port, claude_args), *claude_args])
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


CLAUDE_ACCOUNTS_DIR = Path.home() / ".claude-unlimited" / "claude-accounts"


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
    login_proc = subprocess.run(["claude", "auth", "login"], env=login_env)
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


CODEX_ACCOUNTS_DIR = Path.home() / ".claude-unlimited" / "codex-accounts"


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

    login_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
                 "CODEX_HOME": str(config_dir)}
    login_proc = subprocess.run(["codex", "login"], env=login_env)
    if login_proc.returncode != 0:
        print("\n`codex login` did not complete successfully.", file=sys.stderr)
        return 1

    print("\nResolving the newly logged-in account...")
    from . import openai_credential

    auth_json_path = config_dir / "auth.json"
    try:
        auth_data = json.loads(auth_json_path.read_text())
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
    login_proc = subprocess.run(["claude", "auth", "login"], env=login_env)
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
    parser = argparse.ArgumentParser(prog="claude-unlimited", add_help=True)
    sub = parser.add_subparsers(dest="cmd")
    start_p = sub.add_parser("start", help="run the daemon in the foreground")
    start_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("status", help="check whether the daemon is running")
    sub.add_parser("doctor", help="verify installation and configuration")
    sub.add_parser("add-account", aliases=["ac"],
                    help="log into a Claude account via an isolated Claude Code session "
                         "(doesn't affect other logged-in accounts) and add it as a Profile")
    sub.add_parser("add-codex-account", help="log into a ChatGPT/Codex subscription via an isolated "
                                              "session and add it as a codex-kind Profile")
    reauth_p = sub.add_parser("reauth", help="re-authenticate an OAuth Profile that needs it "
                                              "(defaults to whichever ones the daemon reports as needing it)")
    reauth_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    code_p = sub.add_parser("code", help="start the daemon if needed, then launch `claude` routed through it")
    code_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    code_p.add_argument("--profile", metavar="NAME_OR_ID", default=None,
                         help="pin this session to one Profile by name or id, skipping the interactive picker")
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
    if args.cmd == "add-codex-account":
        return add_codex_account()
    if args.cmd == "reauth":
        return reauth(args.port)
    if args.cmd == "code":
        return code(args.port, unknown, profile_arg=args.profile)
    if args.cmd == "install":
        return install(args.port)
    if args.cmd == "uninstall":
        return uninstall()
    if args.cmd == "service-start":
        return service_start()
    if args.cmd == "service-stop":
        return service_stop()
    if args.cmd == "restart":
        return restart(args.port)
    if args.cmd == "purge":
        return purge(args.port, assume_yes=args.yes)

    parser.print_help()
    return 0
