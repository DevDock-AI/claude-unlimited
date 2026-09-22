"""Installs, registers and launches the macOS HUD without the user asking.

The HUD used to be a thing you built yourself out of a checkout, which in
practice meant almost nobody had it. This module makes it arrive with Claude
Unlimited: fresh installs get it from install.sh, and existing installs get it
the first time the new code runs — the same self-heal shape as
`updater.ensure_cli_aliases`, and for the same reason. An update is applied by
the OLD updater, so anything new that must reach an older install has to run
from the new code at a point that is guaranteed to execute.

TRUST — what a downloaded binary has to clear before it is allowed to run:

  1. The URL is built from `updater.GITHUB_OWNER`/`GITHUB_REPO`, constants in
     this repository. Nothing in config or the environment can redirect it.
  2. HTTPS with the stdlib's certificate verification; a redirect to a
     non-HTTPS URL is refused, exactly as the updater does.
  3. The SHA-256 must match a digest read out of the INSTALLED SOURCE TREE
     (`macos-widget/HUD.sha256`), not out of the release page. That tree was
     either cloned by install.sh or verified commit-for-commit by
     `updater.stage_release`, so the digest inherits that check instead of
     appealing to the same transport twice.
  4. Only then is the archive unpacked. A mismatch deletes the download and
     leaves whatever is installed exactly as it was.

Best-effort throughout: a HUD that cannot be installed is a missing ornament,
never a reason for the daemon or the CLI to fail.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .updater import GITHUB_OWNER, GITHUB_REPO, INSTALL_ROOT, NETWORK_TIMEOUT_SECONDS

# Kept in step with daemon.HUD_NAME; that module imports this one's constants
# rather than the other way round, so the name lives here.
HUD_NAME = "HUD - Heads-Up Display"
BUNDLE_NAME = f"{HUD_NAME}.app"
BUNDLE_DIR = Path.home() / "Applications"
LEGACY_BUNDLE = BUNDLE_DIR / "CapacityWidget.app"
LABEL = "ai.devdock.claude-unlimited.hud"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
STAMP = INSTALL_ROOT / "hud-version"
# Written by `cu hud remove`. Without it the daemon put the HUD straight back
# at its next start, so "remove it for good" lasted until the next login.
OPT_OUT = INSTALL_ROOT / "hud-removed"
APP_DIR = INSTALL_ROOT / "app"
DIGEST_FILE = Path("macos-widget") / "HUD.sha256"

RELEASE_ASSET_URL = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/download/v{{version}}/{{asset}}"
)

# A HUD bundle is a couple of megabytes. The cap is not a security boundary —
# the digest is — it just stops a wrong URL streaming forever into a temp file.
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
SUBPROCESS_TIMEOUT_SECONDS = 60


# --------------------------------------------------------------------------
# Pure helpers. No I/O, no clock, no platform — so the rules below are tested
# rather than inferred from a successful install on one machine.
# --------------------------------------------------------------------------

def asset_name(version: str) -> str:
    """The release asset for a version. One spelling, used by the builder that
    produces it and the installer that fetches it."""
    return f"HUD-{version}-macos.zip"


def asset_url(version: str) -> str:
    return RELEASE_ASSET_URL.format(version=version, asset=asset_name(version))


def expected_digest(text: str, asset: str) -> Optional[str]:
    """The SHA-256 for `asset` out of a `shasum`-format file.

    Returns None rather than raising for anything unreadable: a malformed or
    truncated digest file must mean "do not install", not "crash the daemon".
    A digest for a DIFFERENT asset is also None — matching on the name is the
    point, otherwise last release's line would authorise this release's file.
    """
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0].strip(), parts[1].lstrip("*").strip()
        if name != asset:
            continue
        if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            return None
        return digest.lower()
    return None


def needs_install(stamp: Optional[str], version: str, bundle_exists: bool) -> bool:
    """Whether to (re)install.

    Both halves matter. Version alone would skip a user who dragged the app to
    the Trash and now wonders where it went; presence alone would leave every
    existing install on the first HUD ever shipped.
    """
    if not bundle_exists:
        return True
    return (stamp or "").strip() != version.strip()


def launch_agent_plist(bundle: Path) -> bytes:
    """The login item, as plist bytes.

    `RunAtLoad` and nothing else: no KeepAlive, because a user who quits the
    HUD from its own menu means it, and launchd restarting it two seconds
    later would be a bug with no off switch.
    """
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(bundle / "Contents" / "MacOS" / "HUD")],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    })


def is_supported() -> bool:
    return sys.platform == "darwin"


# --------------------------------------------------------------------------
# The install itself.
# --------------------------------------------------------------------------

def _run(command: list, runner: Callable) -> subprocess.CompletedProcess:
    return runner(command, capture_output=True, text=True,
                  timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False)


def _download(url: str, destination: Path, opener: Callable) -> None:
    request = urllib.request.Request(url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": f"{GITHUB_REPO}-hud",
    })
    with opener(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        if not response.geturl().startswith("https://"):
            raise OSError("Refusing a non-HTTPS redirect while downloading the HUD.")
        written = 0
        with destination.open("wb") as out:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise OSError("The HUD download is larger than it should ever be.")
                out.write(chunk)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _register_login_item(bundle: Path, runner: Callable) -> bool:
    """Writes the LaunchAgent and loads it, replacing any previous copy.

    `bootout` before `bootstrap`: bootstrapping a label that is already loaded
    fails, so an upgrade would otherwise keep launching the old path.

    Returns whether the bootstrap succeeded. The caller needs to know, because
    the agent carries `RunAtLoad` — a successful bootstrap has already started
    the HUD, and opening it again on top of that leaves the user with two
    floating docks (which is exactly what happened the first time this ran).
    """
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_bytes(launch_agent_plist(bundle))
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{LABEL}"], runner)
    return _run(["launchctl", "bootstrap", domain, str(LAUNCH_AGENT)], runner).returncode == 0


def ensure_installed(*, version: str, app_dir: Optional[Path] = None,
                     bundle_dir: Optional[Path] = None, stamp_path: Optional[Path] = None,
                     opener: Callable = urllib.request.urlopen,
                     runner: Callable = subprocess.run,
                     force: bool = False,
                     opt_out_path: Optional[Path] = None) -> str:
    """Make the HUD present, registered and running. Returns a status word.

    One of: unsupported, opted_out, current, no_digest, download_failed,
    digest_mismatch, bad_archive, failed, installed.

    `force` is someone asking for it by hand (`cu hud install`): it installs
    even over a current stamp, and clears an earlier `cu hud remove`.

    The paths default to None and are resolved HERE, not in the signature: a
    default bound at import time keeps pointing at the real ~/Applications
    however the caller redirects the module's constants, which is how the test
    suite deleted the developer's installed HUD.
    """
    if not is_supported():
        return "unsupported"
    app_dir = APP_DIR if app_dir is None else app_dir
    bundle_dir = BUNDLE_DIR if bundle_dir is None else bundle_dir
    stamp_path = STAMP if stamp_path is None else stamp_path
    opt_out_path = OPT_OUT if opt_out_path is None else opt_out_path
    if not force and opt_out_path.exists():
        return "opted_out"

    bundle = bundle_dir / BUNDLE_NAME
    stamp = None
    try:
        stamp = stamp_path.read_text(encoding="utf-8")
    except OSError:
        pass
    if not force and not needs_install(stamp, version, bundle.is_dir()):
        return "current"

    asset = asset_name(version)
    try:
        digest_text = (app_dir / DIGEST_FILE).read_text(encoding="utf-8")
    except OSError:
        return "no_digest"
    wanted = expected_digest(digest_text, asset)
    if wanted is None:
        return "no_digest"

    with tempfile.TemporaryDirectory(prefix="cu-hud-") as scratch_name:
        scratch = Path(scratch_name)
        archive = scratch / asset
        try:
            _download(asset_url(version), archive, opener)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return "download_failed"

        if _sha256(archive) != wanted:
            # Nothing is unpacked and nothing installed is touched: the check
            # happens here, before any of this archive's bytes become a file
            # the system could be asked to execute.
            return "digest_mismatch"

        extracted = scratch / "unpacked"
        unpack = _run(["ditto", "-x", "-k", str(archive), str(extracted)], runner)
        if unpack.returncode != 0:
            return "bad_archive"
        staged = extracted / BUNDLE_NAME
        if not (staged / "Contents" / "MacOS" / "HUD").is_file():
            return "bad_archive"

        # The bundle is ad-hoc signed, so the quarantine flag a download
        # carries turns first launch into "the app is damaged". Strip it here
        # rather than asking every user to right-click-Open once.
        _run(["xattr", "-dr", "com.apple.quarantine", str(staged)], runner)

        try:
            bundle_dir.mkdir(parents=True, exist_ok=True)
            # Stop the running copy before the bundle under it is replaced,
            # or the old process keeps running against deleted files.
            _run(["pkill", "-f", f"{bundle}/Contents/MacOS/"], runner)
            previous = scratch / "previous.app"
            if bundle.exists():
                shutil.move(str(bundle), str(previous))
            try:
                shutil.move(str(staged), str(bundle))
            except OSError:
                if previous.exists():
                    shutil.move(str(previous), str(bundle))
                raise
        except OSError:
            return "failed"

    started = False
    try:
        started = _register_login_item(bundle, runner)
    except OSError:
        pass  # installed but not a login item is still better than nothing

    # Show up now rather than at the next login — but only if RunAtLoad did
    # not already do it, or the user gets two of everything.
    if not started:
        _run(["open", "-a", str(bundle)], runner)
    # The pre-rename bundle would otherwise sit there as a second floating dock.
    if LEGACY_BUNDLE.is_dir():
        _run(["pkill", "-f", f"{LEGACY_BUNDLE}/Contents/MacOS/"], runner)
        shutil.rmtree(LEGACY_BUNDLE, ignore_errors=True)

    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(version, encoding="utf-8")
    except OSError:
        pass
    try:
        opt_out_path.unlink()
    except OSError:
        pass
    return "installed"


def remove(*, bundle_dir: Optional[Path] = None, stamp_path: Optional[Path] = None,
           runner: Callable = subprocess.run,
           opt_out_path: Optional[Path] = None) -> None:
    """Undo everything `ensure_installed` created, and remember that the user
    did not want it, so nothing automatic puts it back. Idempotent, never raises.

    Paths resolved at call time — see ensure_installed's docstring."""
    bundle_dir = BUNDLE_DIR if bundle_dir is None else bundle_dir
    stamp_path = STAMP if stamp_path is None else stamp_path
    opt_out_path = OPT_OUT if opt_out_path is None else opt_out_path
    try:
        opt_out_path.parent.mkdir(parents=True, exist_ok=True)
        opt_out_path.write_text("removed by the user\n", encoding="utf-8")
    except OSError:
        pass
    if is_supported():
        try:
            _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], runner)
        except (OSError, subprocess.SubprocessError):
            pass
    for bundle in (bundle_dir / BUNDLE_NAME, LEGACY_BUNDLE):
        try:
            _run(["pkill", "-f", f"{bundle}/Contents/MacOS/"], runner)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(bundle, ignore_errors=True)
    for path in (LAUNCH_AGENT, stamp_path):
        try:
            path.unlink()
        except OSError:
            pass


def ensure_installed_quietly(version: str) -> str:
    """The form the daemon and CLI call: swallows everything, including the
    failures `ensure_installed` reports by returning rather than raising."""
    try:
        return ensure_installed(version=version)
    except Exception:
        return "failed"


# Worth trying again later: the network was down, the disk was busy. Anything
# else is an answer — installed, current, not wanted, not published for this
# version, or a download that failed its checksum (which a retry of the same
# immutable release asset cannot fix).
TRANSIENT = frozenset({"download_failed", "failed", "bad_archive"})
RETRY_DELAYS_SECONDS = (600, 1800, 3600, 3 * 3600)
RETRY_CEILING_SECONDS = 6 * 3600


def retry_delay(attempt: int) -> float:
    """Seconds to wait after the `attempt`-th failure (0-based). Pure."""
    if attempt < len(RETRY_DELAYS_SECONDS):
        return RETRY_DELAYS_SECONDS[attempt]
    return RETRY_CEILING_SECONDS


def keep_installed(version: str, *, attempt_install: Optional[Callable] = None,
                   sleep: Callable = time.sleep, max_attempts: Optional[int] = None) -> str:
    """The daemon's HUD thread. One try at startup was not enough: a Mac that
    updated while offline, or asleep on a hotel network, went without the HUD
    until the next daemon restart — which, with a login item, can be weeks.
    Retries transient failures on a slow, growing backoff (one small HTTPS
    request each, to GitHub's release CDN); stops at the first real answer.
    Returns that answer (for the tests)."""
    attempt_install = attempt_install or ensure_installed_quietly
    attempt = 0
    while True:
        result = attempt_install(version)
        if result not in TRANSIENT:
            return result
        if max_attempts is not None and attempt + 1 >= max_attempts:
            return result
        sleep(retry_delay(attempt))
        attempt += 1
