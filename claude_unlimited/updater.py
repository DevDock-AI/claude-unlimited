"""Checks for, downloads, verifies and installs a new release.

TRUST ROOT — the decision this feature was blocked on, made explicit:

  1. The source is hardcoded. GITHUB_OWNER/GITHUB_REPO below are constants,
     never read from config, never from a response body, never from an
     environment variable in a normal run. Nothing an attacker can write to
     `~/.claude-unlimited/config.json` can point the updater somewhere else.
  2. Every network call is HTTPS with certificate verification (the stdlib
     default). Redirects to a non-HTTPS URL are rejected.
  3. The downloaded tree is verified by content, not by trusting the
     transport twice. The release API names a commit SHA; git clones the tag
     and reports the SHA it actually got. Git objects are content-addressed,
     so a tree whose bytes were altered in flight cannot produce the expected
     SHA. The install only proceeds when the two agree.
  4. The previous installation is kept and restored if the new one fails to
     import, so a bad release degrades to "still on the old version" rather
     than a broken daemon.

What this deliberately does NOT claim: it is not a signature check. It
proves the code came from this repository's history as GitHub reports it;
it cannot prove GitHub itself, or an account with push access, is honest.
A detached-signature check can layer on top of step 3 later without
changing anything else here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

GITHUB_OWNER = "DevDock-AI"
GITHUB_REPO = "claude-unlimited"
RELEASES_LATEST_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
COMMIT_REF_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{{ref}}"
CLONE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}.git"

INSTALL_ROOT = Path.home() / ".local" / "share" / "claude-unlimited"
APP_DIR = INSTALL_ROOT / "app"
PREVIOUS_APP_DIR = INSTALL_ROOT / "app.previous"
VENV_PYTHON = INSTALL_ROOT / "venv" / "bin" / "python"

NETWORK_TIMEOUT_SECONDS = 20
SUBPROCESS_TIMEOUT_SECONDS = 300


class UpdateError(RuntimeError):
    """Any failure that leaves the current installation untouched."""


@dataclass(frozen=True)
class Release:
    version: str  # normalized, no leading "v"
    tag: str  # the tag exactly as GitHub names it
    commit_sha: str
    notes: str


def parse_version(raw: str) -> tuple:
    """(1, 2, 3) from "v1.2.3". Non-numeric trailing parts are dropped rather
    than guessed at, so a pre-release tag compares as its base version."""
    cleaned = raw.strip().lstrip("vV")
    parts = re.split(r"[.\-+]", cleaned)
    numbers = []
    for part in parts:
        if not part.isdigit():
            break
        numbers.append(int(part))
    return tuple(numbers) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


class NoReleasesYet(UpdateError):
    """The repository has no published release. A normal state for a project
    before its first tag, not a failure worth reporting to the user."""


def _get_json(url: str, opener: Callable) -> dict:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{GITHUB_REPO}-updater",
    })
    try:
        with opener(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            if not response.geturl().startswith("https://"):
                raise UpdateError("Refusing a non-HTTPS redirect while checking for updates.")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NoReleasesYet("No releases published yet.") from exc
        raise UpdateError(f"GitHub returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not reach GitHub: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateError(f"GitHub returned something that isn't JSON: {exc}") from exc


def check_for_update(current_version: str, *, opener: Callable = urllib.request.urlopen) -> Optional[Release]:
    """The newest release if it is newer than `current_version`, else None.

    Resolves the tag to a commit SHA in the same pass, so the verification
    step later has something to compare against that was fetched before any
    code was downloaded."""
    payload = _get_json(RELEASES_LATEST_URL, opener)
    tag = (payload.get("tag_name") or "").strip()
    if not tag:
        raise UpdateError("The latest release has no tag name.")
    version = tag.lstrip("vV")
    if not is_newer(version, current_version):
        return None

    commit = _get_json(COMMIT_REF_URL.format(ref=tag), opener)
    sha = (commit.get("sha") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise UpdateError(f"GitHub reported an unusable commit SHA for {tag!r}.")
    return Release(version=version, tag=tag, commit_sha=sha,
                    notes=(payload.get("body") or "").strip())


def _run(command: list, runner: Callable) -> subprocess.CompletedProcess:
    try:
        return runner(command, capture_output=True, text=True,
                      timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"Command failed to run ({command[0]}): {exc}") from exc


def stage_release(release: Release, destination: Path, *, runner: Callable = subprocess.run) -> Path:
    """Clones the release's tag and refuses unless the commit git actually
    checked out is the one the API named. Git objects are content-addressed,
    so this is a content check, not a second appeal to the transport."""
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    clone = _run(["git", "clone", "--depth", "1", "--branch", release.tag,
                  CLONE_URL, str(destination)], runner)
    if clone.returncode != 0:
        raise UpdateError(f"Could not download {release.tag}: {(clone.stderr or '').strip()[:200]}")

    head = _run(["git", "-C", str(destination), "rev-parse", "HEAD"], runner)
    got = (head.stdout or "").strip()
    if got != release.commit_sha:
        shutil.rmtree(destination, ignore_errors=True)
        raise UpdateError(
            f"Refusing to install {release.tag}: downloaded commit {got[:12] or '?'} "
            f"does not match the {release.commit_sha[:12]} GitHub named for that tag.")
    return destination


def install_staged(staged: Path, *, runner: Callable = subprocess.run,
                   app_dir: Path = APP_DIR, previous_dir: Path = PREVIOUS_APP_DIR,
                   venv_python: Path = VENV_PYTHON) -> None:
    """Installs an already-verified tree, keeping the old one to roll back to.

    The new code is proven importable before the old copy is released, so a
    release that cannot even load leaves the daemon on the version that
    works."""
    if not staged.joinpath("pyproject.toml").exists():
        raise UpdateError("The downloaded tree does not look like this project.")
    if not venv_python.exists():
        raise UpdateError(f"No virtual environment at {venv_python}. Re-run install.sh instead.")

    shutil.rmtree(staged / ".git", ignore_errors=True)
    if previous_dir.exists():
        shutil.rmtree(previous_dir, ignore_errors=True)
    if app_dir.exists():
        shutil.move(str(app_dir), str(previous_dir))
    shutil.move(str(staged), str(app_dir))

    def _roll_back(reason: str):
        shutil.rmtree(app_dir, ignore_errors=True)
        if previous_dir.exists():
            shutil.move(str(previous_dir), str(app_dir))
            _run([str(venv_python), "-m", "pip", "install", "--force-reinstall",
                  "--no-deps", "-q", str(app_dir)], runner)
        raise UpdateError(reason)

    installed = _run([str(venv_python), "-m", "pip", "install", "--force-reinstall",
                      "--no-deps", "-q", str(app_dir)], runner)
    if installed.returncode != 0:
        _roll_back(f"Install failed, rolled back: {(installed.stderr or '').strip()[:200]}")

    check = _run([str(venv_python), "-c", "import claude_unlimited"], runner)
    if check.returncode != 0:
        _roll_back(f"The new version could not be imported, rolled back: "
                    f"{(check.stderr or '').strip()[:200]}")


STAGING_DIR = INSTALL_ROOT / "staged-update"

# What a mode does when a newer release exists:
#   manual         -> report it, touch nothing
#   auto_download  -> download and verify, leave it staged for a click
#   auto_install   -> download, verify and install
MODE_DOWNLOADS = ("auto_download", "auto_install")
MODE_INSTALLS = ("auto_install",)


@dataclass(frozen=True)
class UpdateOutcome:
    release: Optional[Release]
    action: str  # "none" | "available" | "downloaded" | "installed"
    error: Optional[str] = None

    @property
    def needs_restart(self) -> bool:
        return self.action == "installed"


def run_update_cycle(current_version: str, mode: str, *,
                     opener: Callable = urllib.request.urlopen,
                     runner: Callable = subprocess.run,
                     staging_dir: Path = STAGING_DIR) -> UpdateOutcome:
    """One full check-and-act pass, doing only what `mode` allows.

    Never raises: a failed update must not take the daemon down with it, and
    the caller is a background loop that should simply try again later."""
    try:
        release = check_for_update(current_version, opener=opener)
    except NoReleasesYet:
        return UpdateOutcome(release=None, action="none")
    except UpdateError as exc:
        return UpdateOutcome(release=None, action="none", error=str(exc))
    if release is None:
        return UpdateOutcome(release=None, action="none")

    if mode not in MODE_DOWNLOADS:
        return UpdateOutcome(release=release, action="available")

    try:
        staged = stage_release(release, staging_dir, runner=runner)
    except UpdateError as exc:
        return UpdateOutcome(release=release, action="available", error=str(exc))

    if mode not in MODE_INSTALLS:
        return UpdateOutcome(release=release, action="downloaded")

    try:
        install_staged(staged, runner=runner)
    except UpdateError as exc:
        return UpdateOutcome(release=release, action="downloaded", error=str(exc))
    return UpdateOutcome(release=release, action="installed")
