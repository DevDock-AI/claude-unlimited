"""The HUD auto-installer.

Nothing here touches the network, the real ~/Applications, launchctl or the
user's own HUD: every path is redirected into tmp_path by the autouse fixture
in conftest, and both `opener` and `runner` are injected fakes.
"""

import hashlib
import plistlib

import pytest

from claude_unlimited import hud


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_asset_name_is_one_spelling():
    assert hud.asset_name("1.2.8") == "HUD-1.2.8-macos.zip"
    assert hud.asset_url("1.2.8").endswith("/releases/download/v1.2.8/HUD-1.2.8-macos.zip")
    assert hud.asset_url("1.2.8").startswith("https://github.com/")


def test_expected_digest_reads_the_line_for_this_asset():
    text = ("a" * 64 + "  HUD-1.2.7-macos.zip\n"
            + "b" * 64 + "  HUD-1.2.8-macos.zip\n")
    assert hud.expected_digest(text, "HUD-1.2.8-macos.zip") == "b" * 64


def test_expected_digest_ignores_another_releases_line():
    """Matching on the name is the whole point: the previous release's digest
    must never authorise this release's download."""
    text = "a" * 64 + "  HUD-1.2.7-macos.zip\n"
    assert hud.expected_digest(text, "HUD-1.2.8-macos.zip") is None


def test_expected_digest_accepts_the_binary_star_form():
    text = "c" * 64 + " *HUD-1.2.8-macos.zip\n"
    assert hud.expected_digest(text, "HUD-1.2.8-macos.zip") == "c" * 64


@pytest.mark.parametrize("text", [
    "",
    "not a digest file",
    "short  HUD-1.2.8-macos.zip",
    "z" * 64 + "  HUD-1.2.8-macos.zip",  # not hex
])
def test_expected_digest_refuses_anything_malformed(text):
    assert hud.expected_digest(text, "HUD-1.2.8-macos.zip") is None


def test_needs_install_covers_both_halves():
    # Same version, bundle present: nothing to do.
    assert hud.needs_install("1.2.8", "1.2.8", True) is False
    # Same version but the user trashed the app.
    assert hud.needs_install("1.2.8", "1.2.8", False) is True
    # Older stamp: an existing install that has never seen this HUD.
    assert hud.needs_install("1.2.7", "1.2.8", True) is True
    # No stamp at all: every install that predates this feature.
    assert hud.needs_install(None, "1.2.8", True) is True
    assert hud.needs_install("  1.2.8\n", "1.2.8", True) is False


def test_launch_agent_plist_runs_at_load_and_does_not_keep_alive(tmp_path):
    bundle = tmp_path / "HUD - Heads-Up Display.app"
    parsed = plistlib.loads(hud.launch_agent_plist(bundle))
    assert parsed["Label"] == "ai.devdock.claude-unlimited.hud"
    assert parsed["RunAtLoad"] is True
    assert parsed["ProgramArguments"] == [str(bundle / "Contents" / "MacOS" / "HUD")]
    # A user who quits the HUD means it; launchd must not bring it back.
    assert "KeepAlive" not in parsed


# --------------------------------------------------------------------------
# ensure_installed
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload: bytes, url: str = "https://github.com/x"):
        self._payload = payload
        self._url = url
        self._read = False

    def geturl(self):
        return self._url

    def read(self, _size=None):
        if self._read:
            return b""
        self._read = True
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def make_opener(payload: bytes, url: str = "https://github.com/x"):
    def opener(_request, timeout=None):
        return FakeResponse(payload, url)
    return opener


class FakeRunner:
    """Records every command and fakes the two that produce files."""

    def __init__(self, bundle_name: str, unpack_ok: bool = True, executable: bool = True,
                 bootstrap_ok: bool = True):
        self.commands = []
        self.bundle_name = bundle_name
        self.unpack_ok = unpack_ok
        self.executable = executable
        self.bootstrap_ok = bootstrap_ok

    def __call__(self, command, **_kwargs):
        from pathlib import Path
        import subprocess
        self.commands.append(list(command))
        if command[0] == "ditto" and self.unpack_ok:
            destination = Path(command[-1])
            binary = destination / self.bundle_name / "Contents" / "MacOS" / "HUD"
            if self.executable:
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text("#!/bin/sh\n", encoding="utf-8")
            else:
                destination.mkdir(parents=True, exist_ok=True)
        code = 0
        if command[0] == "ditto" and not self.unpack_ok:
            code = 1
        if command[:2] == ["launchctl", "bootstrap"] and not self.bootstrap_ok:
            code = 1
        return subprocess.CompletedProcess(command, code, "", "")

    def ran(self, program):
        return [c for c in self.commands if c[0] == program]


@pytest.fixture
def source_tree(tmp_path):
    """An installed app dir carrying the digest file, the way install.sh and
    the updater both leave it."""
    def build(version: str, payload: bytes):
        app = tmp_path / "app"
        (app / "macos-widget").mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        (app / "macos-widget" / "HUD.sha256").write_text(
            f"{digest}  {hud.asset_name(version)}\n", encoding="utf-8")
        return app
    return build


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(hud, "is_supported", lambda: True)
    monkeypatch.setattr(hud.os, "getuid", lambda: 501, raising=False)


def test_install_downloads_verifies_registers_and_launches(tmp_path, darwin, source_tree):
    payload = b"a HUD zip"
    app = source_tree("1.2.8", payload)
    runner = FakeRunner(hud.BUNDLE_NAME)
    applications = tmp_path / "Applications"
    stamp = tmp_path / "hud-version"

    result = hud.ensure_installed(version="1.2.8", app_dir=app, bundle_dir=applications,
                                  stamp_path=stamp, opener=make_opener(payload), runner=runner)

    assert result == "installed"
    assert (applications / hud.BUNDLE_NAME / "Contents" / "MacOS" / "HUD").is_file()
    assert stamp.read_text(encoding="utf-8") == "1.2.8"
    assert hud.LAUNCH_AGENT.is_file()
    # Quarantine is stripped before the bundle is moved into place, or an
    # ad-hoc-signed download refuses to launch at all.
    assert runner.ran("xattr")
    assert ["launchctl", "bootstrap", "gui/501", str(hud.LAUNCH_AGENT)] in runner.commands
    # The agent has RunAtLoad, so a successful bootstrap has already started
    # the HUD. Opening it as well launched a second copy the first time this
    # ran live — two floating docks, both polling.
    assert not runner.ran("open")


def test_hud_is_opened_when_the_login_item_could_not_be_loaded(tmp_path, darwin, source_tree):
    """launchctl refused, so nothing started it — open it directly instead."""
    payload = b"a HUD zip"
    app = source_tree("1.2.8", payload)
    runner = FakeRunner(hud.BUNDLE_NAME, bootstrap_ok=False)

    result = hud.ensure_installed(version="1.2.8", app_dir=app,
                                  bundle_dir=tmp_path / "Applications",
                                  stamp_path=tmp_path / "hud-version",
                                  opener=make_opener(payload), runner=runner)

    assert result == "installed"
    assert runner.ran("open")


def test_second_run_does_nothing(tmp_path, darwin, source_tree):
    payload = b"a HUD zip"
    app = source_tree("1.2.8", payload)
    applications = tmp_path / "Applications"
    stamp = tmp_path / "hud-version"
    runner = FakeRunner(hud.BUNDLE_NAME)
    hud.ensure_installed(version="1.2.8", app_dir=app, bundle_dir=applications,
                         stamp_path=stamp, opener=make_opener(payload), runner=runner)

    def refuse(_request, timeout=None):
        raise AssertionError("a current install must not hit the network")

    assert hud.ensure_installed(version="1.2.8", app_dir=app, bundle_dir=applications,
                                stamp_path=stamp, opener=refuse,
                                runner=FakeRunner(hud.BUNDLE_NAME)) == "current"


def test_a_wrong_digest_installs_nothing(tmp_path, darwin, source_tree):
    app = source_tree("1.2.8", b"the bytes we published")
    runner = FakeRunner(hud.BUNDLE_NAME)
    applications = tmp_path / "Applications"
    stamp = tmp_path / "hud-version"

    result = hud.ensure_installed(version="1.2.8", app_dir=app, bundle_dir=applications,
                                  stamp_path=stamp,
                                  opener=make_opener(b"something else entirely"), runner=runner)

    assert result == "digest_mismatch"
    assert not (applications / hud.BUNDLE_NAME).exists()
    assert not stamp.exists()
    # Nothing was unpacked: the check happens before any byte becomes a file.
    assert runner.ran("ditto") == []


def test_a_mismatch_leaves_the_existing_bundle_alone(tmp_path, darwin, source_tree):
    payload = b"a HUD zip"
    app = source_tree("1.2.8", payload)
    applications = tmp_path / "Applications"
    stamp = tmp_path / "hud-version"
    hud.ensure_installed(version="1.2.8", app_dir=app, bundle_dir=applications,
                         stamp_path=stamp, opener=make_opener(payload),
                         runner=FakeRunner(hud.BUNDLE_NAME))

    newer = source_tree("1.2.9", b"the 1.2.9 bytes")
    result = hud.ensure_installed(version="1.2.9", app_dir=newer, bundle_dir=applications,
                                  stamp_path=stamp, opener=make_opener(b"tampered"),
                                  runner=FakeRunner(hud.BUNDLE_NAME))

    assert result == "digest_mismatch"
    assert (applications / hud.BUNDLE_NAME / "Contents" / "MacOS" / "HUD").is_file()
    assert stamp.read_text(encoding="utf-8") == "1.2.8"


def test_a_release_without_a_digest_file_is_skipped(tmp_path, darwin):
    app = tmp_path / "app"
    app.mkdir()

    def refuse(_request, timeout=None):
        raise AssertionError("no digest means no download")

    assert hud.ensure_installed(version="1.2.8", app_dir=app,
                                bundle_dir=tmp_path / "Applications",
                                stamp_path=tmp_path / "hud-version",
                                opener=refuse, runner=FakeRunner(hud.BUNDLE_NAME)) == "no_digest"


def test_a_download_failure_is_reported_not_raised(tmp_path, darwin, source_tree):
    app = source_tree("1.2.8", b"payload")

    def broken(_request, timeout=None):
        raise OSError("no network")

    assert hud.ensure_installed(version="1.2.8", app_dir=app,
                                bundle_dir=tmp_path / "Applications",
                                stamp_path=tmp_path / "hud-version",
                                opener=broken, runner=FakeRunner(hud.BUNDLE_NAME)) == "download_failed"


def test_a_non_https_redirect_is_refused(tmp_path, darwin, source_tree):
    payload = b"payload"
    app = source_tree("1.2.8", payload)
    assert hud.ensure_installed(version="1.2.8", app_dir=app,
                                bundle_dir=tmp_path / "Applications",
                                stamp_path=tmp_path / "hud-version",
                                opener=make_opener(payload, url="http://evil.example/x"),
                                runner=FakeRunner(hud.BUNDLE_NAME)) == "download_failed"


def test_an_archive_without_the_binary_is_refused(tmp_path, darwin, source_tree):
    payload = b"payload"
    app = source_tree("1.2.8", payload)
    runner = FakeRunner(hud.BUNDLE_NAME, executable=False)
    assert hud.ensure_installed(version="1.2.8", app_dir=app,
                                bundle_dir=tmp_path / "Applications",
                                stamp_path=tmp_path / "hud-version",
                                opener=make_opener(payload), runner=runner) == "bad_archive"
    assert not (tmp_path / "Applications" / hud.BUNDLE_NAME).exists()


def test_nothing_happens_off_macos(tmp_path, monkeypatch, source_tree):
    monkeypatch.setattr(hud, "is_supported", lambda: False)
    app = source_tree("1.2.8", b"payload")

    def refuse(_request, timeout=None):
        raise AssertionError("the HUD is macOS only")

    assert hud.ensure_installed(version="1.2.8", app_dir=app,
                                bundle_dir=tmp_path / "Applications",
                                stamp_path=tmp_path / "hud-version",
                                opener=refuse, runner=FakeRunner(hud.BUNDLE_NAME)) == "unsupported"


# --------------------------------------------------------------------------
# remove
# --------------------------------------------------------------------------

def test_remove_clears_everything_and_repeats_safely(tmp_path, darwin, source_tree):
    payload = b"a HUD zip"
    app = source_tree("1.2.8", payload)
    applications = tmp_path / "Applications"
    stamp = tmp_path / "hud-version"
    hud.ensure_installed(version="1.2.8", app_dir=app, bundle_dir=applications,
                         stamp_path=stamp, opener=make_opener(payload),
                         runner=FakeRunner(hud.BUNDLE_NAME))

    runner = FakeRunner(hud.BUNDLE_NAME)
    hud.remove(bundle_dir=applications, stamp_path=stamp, runner=runner)
    assert not (applications / hud.BUNDLE_NAME).exists()
    assert not hud.LAUNCH_AGENT.exists()
    assert not stamp.exists()
    assert ["launchctl", "bootout", f"gui/501/{hud.LABEL}"] in runner.commands

    hud.remove(bundle_dir=applications, stamp_path=stamp, runner=FakeRunner(hud.BUNDLE_NAME))


def test_the_pre_rename_bundle_is_cleaned_up(tmp_path, darwin, source_tree):
    (hud.LEGACY_BUNDLE / "Contents" / "MacOS").mkdir(parents=True)
    payload = b"a HUD zip"
    app = source_tree("1.2.8", payload)
    hud.ensure_installed(version="1.2.8", app_dir=app, bundle_dir=tmp_path / "Applications",
                         stamp_path=tmp_path / "hud-version", opener=make_opener(payload),
                         runner=FakeRunner(hud.BUNDLE_NAME))
    assert not hud.LEGACY_BUNDLE.exists()


def test_the_real_hud_can_never_be_removed_by_a_test(tmp_path, monkeypatch):
    """What actually happened: `remove()` and `ensure_installed()` bound their
    path defaults at import, so redirecting the module's constants (what the
    conftest fixture does) did not reach them — and a purge test deleted the
    developer's installed HUD from the real ~/Applications, twice."""
    import inspect
    from pathlib import Path

    for fn in (hud.remove, hud.ensure_installed):
        for name, param in inspect.signature(fn).parameters.items():
            if name.endswith(("_dir", "_path")):
                assert param.default is None, f"{fn.__name__}({name}=) binds a path at import time"

    real_applications = Path.home() / "Applications"
    monkeypatch.setattr(hud, "BUNDLE_DIR", tmp_path / "Applications")
    monkeypatch.setattr(hud, "LEGACY_BUNDLE", tmp_path / "Applications" / "CapacityWidget.app")
    monkeypatch.setattr(hud, "LAUNCH_AGENT", tmp_path / "LaunchAgents" / f"{hud.LABEL}.plist")
    monkeypatch.setattr(hud, "STAMP", tmp_path / "hud-version")
    removed = []
    monkeypatch.setattr(hud.shutil, "rmtree", lambda path, **kw: removed.append(Path(path)))
    hud.remove(runner=lambda *a, **k: None)          # no paths passed: the dangerous shape
    assert removed and all(real_applications not in p.parents for p in removed), removed


# ---- "remove it for good" and trying again later -----------------------------

def test_a_removed_hud_is_not_put_back_by_the_daemon(tmp_path, darwin, source_tree):
    # `cu hud remove` promised "for good"; the daemon's startup install used to
    # put it straight back at the next login.
    payload = b"a HUD zip"
    app = source_tree("1.2.8", payload)
    runner = FakeRunner(hud.BUNDLE_NAME)
    applications = tmp_path / "Applications"
    kw = dict(version="1.2.8", app_dir=app, bundle_dir=applications,
              stamp_path=tmp_path / "hud-version", opener=make_opener(payload), runner=runner)
    assert hud.ensure_installed(**kw) == "installed"

    hud.remove(bundle_dir=applications, stamp_path=tmp_path / "hud-version", runner=runner)
    assert hud.OPT_OUT.exists()
    assert hud.ensure_installed(**kw) == "opted_out"
    assert not (applications / hud.BUNDLE_NAME).exists()

    # Asking for it by hand brings it back and forgets the opt-out.
    assert hud.ensure_installed(force=True, **kw) == "installed"
    assert not hud.OPT_OUT.exists()
    assert hud.ensure_installed(**kw) == "current"


def test_retry_delays_grow_and_cap():
    assert [hud.retry_delay(i) for i in range(6)] == [600, 1800, 3600, 10800, 21600, 21600]


def test_keep_installed_retries_only_transient_failures():
    results = iter(["download_failed", "failed", "installed", "never reached"])
    slept = []
    assert hud.keep_installed("1.2.8", attempt_install=lambda v: next(results),
                              sleep=slept.append) == "installed"
    assert slept == [600, 1800]

    for answer in ("current", "opted_out", "no_digest", "digest_mismatch", "unsupported"):
        slept.clear()
        assert hud.keep_installed("1.2.8", attempt_install=lambda v: answer,
                                  sleep=slept.append) == answer
        assert slept == [], answer   # a real answer is final: no loop, no network


def test_the_daemon_uses_the_retrying_installer():
    import inspect
    from claude_unlimited import daemon
    assert "hud.keep_installed" in inspect.getsource(daemon)
