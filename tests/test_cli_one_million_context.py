"""The 1M-context launch policy (`cu code`).

Claude Code budgets a native-1M model at 200K whenever ANTHROPIC_BASE_URL is
not api.anthropic.com, because it cannot verify that whatever is on that URL
serves 1M. For an oauth / Anthropic-API route through this daemon it does, and
_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL is what says so — measured live on
Claude Code 2.1.278: Opus 5 reports a 200000 window through the pool without
it and 1000000 with it.

The whole risk is asserting that on a route where it is NOT true, so that is
what most of these cover. Pure decision function: no environment is mutated
and nothing here launches anything.
"""

import os

import json

import pytest

from claude_unlimited.context_window import (
    ASSUME_FIRST_PARTY_ENV,
    DISABLE_1M_ENV,
    ONE_M_VERIFIED_MIN_CLIENT,
    _is_anthropic_base_url,
    _one_million_decision,
    _parse_client_version,
)
from claude_unlimited.config import Profile

VERIFIED = (2, 1, 278)


def decide(mode="auto", profiles=None, forced=None, env=None, version=VERIFIED):
    if profiles is None:
        profiles = [Profile(id="a", name="A", kind="oauth", automatic=True)]
    return _one_million_decision(mode, profiles, forced, env or {}, version)


# --- the happy path --------------------------------------------------------

def test_an_all_anthropic_pool_gets_1m():
    assert decide() == (True, "enabled_1m_verified")


def test_an_api_profile_on_anthropics_own_endpoint_qualifies():
    assert decide(profiles=[Profile(id="a", name="A", kind="api", base_url=None,
                                     automatic=True)]) == (True, "enabled_1m_verified")


# --- the route is what decides, not the plan -------------------------------

def test_a_mixed_pool_gets_1m_with_the_guard_active():
    """A codex account in the route no longer caps the window: the gateway's
    per-request capacity guard (docs/adr/0009) keeps every turn that would
    overflow a GPT backend off that account. The reason is distinct so the
    launch note and the dashboard can say the guard is what makes it safe."""
    profiles = [Profile(id="a", name="A", kind="oauth", automatic=True),
                Profile(id="c", name="C", kind="codex", automatic=True)]
    assert decide(profiles=profiles) == (True, "enabled_1m_guarded")


def test_a_codex_only_route_is_refused_with_its_own_reason():
    """Nothing in the route can hold more than a GPT window: 1M would only
    mean every long turn ends in a prompt-too-long at ~226K instead of Claude
    Code's own compaction at 200K."""
    profiles = [Profile(id="c", name="C", kind="codex", automatic=True),
                Profile(id="d", name="D", kind="codex", automatic=True)]
    assert decide(profiles=profiles) == (False, "codex_only_route")


def test_pinning_an_anthropic_profile_rescues_a_mixed_pool():
    """--profile is a guarantee of exactly one account, so the rest of the
    pool stops mattering."""
    anthropic = Profile(id="a", name="A", kind="oauth", automatic=True)
    profiles = [anthropic, Profile(id="c", name="C", kind="codex", automatic=True)]
    assert decide(profiles=profiles, forced=anthropic) == (True, "enabled_1m_verified")


def test_pinning_the_codex_profile_is_a_codex_only_route():
    codex = Profile(id="c", name="C", kind="codex", automatic=True)
    anthropic = Profile(id="a", name="A", kind="oauth", automatic=True)
    assert decide(profiles=[anthropic, codex], forced=codex) == (False, "codex_only_route")


def test_a_manual_only_codex_account_does_not_block_a_claude_pool():
    """`automatic` off means rotation never picks it — router.choose() and
    choose_for_new_branch() both filter on it — so a Codex account kept as a
    manual fallback must not cost the whole pool its context window."""
    profiles = [Profile(id="a", name="A", kind="oauth", automatic=True),
                Profile(id="c", name="C", kind="codex", automatic=False)]
    assert decide(profiles=profiles) == (True, "enabled_1m_verified")


def test_a_forced_subagent_codex_account_is_in_the_route_and_guarded():
    """The exception to `automatic`: gateway._branch_decision routes every
    subagent to a forced profile whatever that flag says — so it is in the
    route, and the guard (not a 200K cap) is what covers it."""
    profiles = [Profile(id="a", name="A", kind="oauth", automatic=True),
                Profile(id="c", name="C", kind="codex", automatic=False,
                        forced_for_subagents=True)]
    assert decide(profiles=profiles) == (True, "enabled_1m_guarded")


def test_a_custom_gateway_beside_a_codex_account_is_still_unknown():
    """The guard vouches for codex windows, never for a foreign Anthropic-
    compatible endpoint's."""
    profiles = [Profile(id="x", name="X", kind="api", automatic=True,
                        base_url="https://llm.example.com/v1"),
                Profile(id="c", name="C", kind="codex", automatic=True)]
    assert decide(profiles=profiles) == (False, "custom_gateway_unknown")


def test_an_api_kind_codex_profile_is_still_codex_for_the_route():
    """An api_key codex Profile (OpenAI's public API) is a GPT backend all
    the same: guarded in a mixed pool, refused alone."""
    codex_api = Profile(id="c", name="C", kind="codex", auth_mode="api_key", automatic=True)
    anthropic = Profile(id="a", name="A", kind="oauth", automatic=True)
    assert decide(profiles=[anthropic, codex_api]) == (True, "enabled_1m_guarded")
    assert decide(profiles=[codex_api]) == (False, "codex_only_route")


def test_a_manual_only_foreign_api_profile_also_stops_blocking():
    profiles = [Profile(id="a", name="A", kind="oauth", automatic=True),
                Profile(id="x", name="X", kind="api", automatic=False,
                        base_url="https://llm.example.com/v1")]
    assert decide(profiles=profiles) == (True, "enabled_1m_verified")


def test_a_pool_of_only_manual_accounts_asserts_nothing():
    """Nothing is reachable by rotation, so there is no route to vouch for."""
    profiles = [Profile(id="a", name="A", kind="oauth", automatic=False)]
    assert decide(profiles=profiles) == (False, "no_profiles")


def test_an_api_profile_on_a_foreign_endpoint_is_unknown_not_assumed():
    profiles = [Profile(id="a", name="A", kind="api", automatic=True,
                        base_url="https://llm.example.com/v1")]
    assert decide(profiles=profiles) == (False, "custom_gateway_unknown")


def test_an_empty_pool_asserts_nothing():
    assert decide(profiles=[]) == (False, "no_profiles")


# --- the user's own choices always win -------------------------------------

@pytest.mark.parametrize("mode,reason", [("prefer_200k", "user_forced_200k"),
                                          ("client_default", "client_default")])
def test_the_setting_is_honoured(mode, reason):
    assert decide(mode=mode) == (False, reason)


def test_a_user_set_disable_flag_is_never_overridden():
    assert decide(env={DISABLE_1M_ENV: "1"}) == (False, "user_forced_200k")


def test_a_user_who_already_set_the_variable_is_left_alone():
    """Not "enabled": we did not do it, and saying we did would be a lie in
    the launch output."""
    assert decide(env={ASSUME_FIRST_PARTY_ENV: "1"}) == (False, "user_already_set")


def test_the_setting_is_checked_before_the_environment():
    """prefer_200k must win even with the variable already exported."""
    assert decide(mode="prefer_200k", env={ASSUME_FIRST_PARTY_ENV: "1"}) \
        == (False, "user_forced_200k")


# --- force_1m: every route, every client ------------------------------------

_CODEX = Profile(id="c", name="C", kind="codex", automatic=True)
_OAUTH = Profile(id="a", name="A", kind="oauth", automatic=True)
_API = Profile(id="k", name="K", kind="api", automatic=True)
_FOREIGN = Profile(id="x", name="X", kind="api", automatic=True, base_url="https://llm.example.com/v1")


@pytest.mark.parametrize("profiles,forced", [
    ([_OAUTH], None),                    # all-Anthropic
    ([_OAUTH, _CODEX], None),            # mixed
    ([_CODEX], None),                    # codex only
    ([_OAUTH, _CODEX], _CODEX),          # --profile codex
    ([_FOREIGN], None),                  # non-Anthropic gateway
    ([_FOREIGN, _CODEX], None),          # gateway beside codex
    ([_API], None),                      # Anthropic API key
    ([Profile(id="m", name="M", kind="oauth", automatic=False)], None),  # nothing reachable
    ([], None),                          # empty pool
])
def test_force_1m_sets_it_on_every_route(profiles, forced):
    assert decide(mode="force_1m", profiles=profiles, forced=forced) == (True, "forced_1m")


@pytest.mark.parametrize("version", [None, (2, 0, 0), VERIFIED])
def test_force_1m_does_not_gate_on_the_client_version(version):
    """The variable is harmless on a client that does not read it."""
    assert decide(mode="force_1m", version=version) == (True, "forced_1m")


def test_force_1m_still_yields_to_the_users_own_environment():
    assert decide(mode="force_1m", env={DISABLE_1M_ENV: "1"}) == (False, "user_forced_200k")
    assert decide(mode="force_1m", env={ASSUME_FIRST_PARTY_ENV: "1"}) == (False, "user_already_set")


def test_force_1m_is_a_valid_setting_value():
    from claude_unlimited.config import CONTEXT_1M_MODES
    assert "force_1m" in CONTEXT_1M_MODES


# --- version gating --------------------------------------------------------

def test_an_unreadable_client_version_is_unverified_not_new_enough():
    assert decide(version=None) == (False, "client_version_unverified")


def test_an_older_client_is_refused():
    older = (ONE_M_VERIFIED_MIN_CLIENT[0], ONE_M_VERIFIED_MIN_CLIENT[1],
             ONE_M_VERIFIED_MIN_CLIENT[2] - 1)
    assert decide(version=older) == (False, "client_version_unverified")


def test_the_minimum_verified_version_itself_passes():
    assert decide(version=ONE_M_VERIFIED_MIN_CLIENT)[0] is True


@pytest.mark.parametrize("raw,expected", [
    ("2.1.278 (Claude Code)", (2, 1, 278)),
    ("  2.1.229\n", (2, 1, 229)),
    ("10.0.3 (Claude Code)", (10, 0, 3)),
    ("", None),
    (None, None),
    ("not a version", None),
])
def test_client_version_parsing(raw, expected):
    assert _parse_client_version(raw) == expected


# --- base-url classification ----------------------------------------------

@pytest.mark.parametrize("url,expected", [
    (None, True),
    ("", True),
    ("   ", True),
    ("https://api.anthropic.com", True),
    ("https://api.anthropic.com/v1", True),
    ("https://llm.example.com", False),
    # A lookalike host must not pass: this decides whether we assert 1M.
    ("https://api.anthropic.com.evil.example", False),
    ("https://notapi.anthropic.com", False),
])
def test_anthropic_base_url_detection(url, expected):
    assert _is_anthropic_base_url(url) is expected


# --- the dashboard preview -------------------------------------------------

def test_the_settings_endpoint_publishes_the_decision_reason(monkeypatch, tmp_path):
    """The brief requires a published reason. The decision is made inside
    `cu code`, in another process, so the dashboard gets the same pure
    function run over the same pool."""
    import claude_unlimited.daemon as daemon
    from claude_unlimited.config import Pool, Settings

    monkeypatch.setattr(daemon, "_cached_client_version", lambda: VERIFIED)

    mixed = Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True),
                            Profile(id="c", name="C", kind="codex", automatic=True,
                                    auth_mode="chatgpt_subscription")],
                  settings=Settings(context_1m="auto"))
    preview = daemon._context_1m_preview(mixed)
    assert (preview["enabled"], preview["reason"]) == (True, "enabled_1m_guarded")
    # The guard block names the codex account and the window it is held to.
    [entry] = preview["guard"]["profiles"]
    assert entry["id"] == "c" and entry["name"] == "C"
    assert entry["window"] == 272_000 and entry["budget"] == 226_400
    assert entry["assumed"] is False and preview["guard"]["assumed"] is False
    assert "gpt-5.6-terra" in entry["models"]

    claude_only = Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True)],
                        settings=Settings(context_1m="auto"))
    assert daemon._context_1m_preview(claude_only) == {"enabled": True,
                                                        "reason": "enabled_1m_verified"}

    off = Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True)],
                settings=Settings(context_1m="prefer_200k"))
    assert daemon._context_1m_preview(off) == {"enabled": False, "reason": "user_forced_200k"}


def test_the_preview_flags_an_assumed_window_and_a_codex_only_pool(monkeypatch):
    import claude_unlimited.daemon as daemon
    from claude_unlimited.config import Pool, Settings

    monkeypatch.setattr(daemon, "_cached_client_version", lambda: VERIFIED)
    codex_only = Pool(profiles=[Profile(id="c", name="C", kind="codex", automatic=True,
                                        auth_mode="chatgpt_subscription", codex_model="gpt-99-nova")],
                      settings=Settings(context_1m="auto"))
    preview = daemon._context_1m_preview(codex_only)
    assert (preview["enabled"], preview["reason"]) == (False, "codex_only_route")
    [entry] = preview["guard"]["profiles"]
    assert entry["models"] == ["gpt-99-nova"]
    assert entry["assumed"] is True and preview["guard"]["assumed"] is True
    assert entry["window"] == 272_000   # the honest floor, never an exclusion

    forced = Pool(profiles=[Profile(id="c", name="C", kind="codex", automatic=True,
                                    auth_mode="chatgpt_subscription")],
                  settings=Settings(context_1m="force_1m"))
    preview = daemon._context_1m_preview(forced)
    assert (preview["enabled"], preview["reason"]) == (True, "forced_1m")
    assert preview["guard"]["profiles"][0]["budget"] == 226_400   # the guard stays on


def test_the_launch_note_names_each_codex_account_and_its_window():
    import claude_unlimited.cli as cli
    from claude_unlimited.config import Pool, Settings

    codex = Profile(id="c", name="Work ChatGPT", kind="codex", automatic=True,
                    auth_mode="chatgpt_subscription")
    pool = Pool(profiles=[_OAUTH, codex], settings=Settings())
    [line] = cli._codex_guard_lines(pool, pool.profiles, None)
    assert "Work ChatGPT" in line and "~226K" in line and "gpt-5.6-terra" in line
    assert "assumed" not in line
    assert cli._codex_guard_lines(Pool(profiles=[_OAUTH], settings=Settings()), [_OAUTH], None) == []

    unknown = Profile(id="u", name="U", kind="codex", automatic=True, codex_model="gpt-99-nova",
                      auth_mode="chatgpt_subscription")
    [line] = cli._codex_guard_lines(Pool(profiles=[unknown], settings=Settings()), [unknown], None)
    assert "assumed" in line and "gpt-99-nova" in line

    # An api_key codex Profile talks to api.openai.com: the public API's window.
    public = Profile(id="k", name="K", kind="codex", automatic=True, auth_mode="api_key")
    [line] = cli._codex_guard_lines(Pool(profiles=[public], settings=Settings()), [public], None)
    assert "~843K" in line

    unknown = Profile(id="u2", name="U2", kind="codex", automatic=True, codex_model="gpt-99-nova",
                      auth_mode="chatgpt_subscription")
    [line] = cli._codex_guard_lines(Pool(profiles=[unknown], settings=Settings()), [unknown], None)
    assert "assumed" in line and "gpt-99-nova" in line


def test_the_client_version_is_not_reread_on_every_poll(monkeypatch):
    """An open dashboard polls /api/settings once a second; forking `claude
    --version` that often would be absurd."""
    import claude_unlimited.daemon as daemon

    calls = []
    monkeypatch.setattr(daemon, "_CLIENT_VERSION_CACHE", {})
    monkeypatch.setattr("claude_unlimited.cli._installed_client_version",
                        lambda: calls.append(1) or VERIFIED)
    for _ in range(5):
        assert daemon._cached_client_version() == VERIFIED
    assert len(calls) == 1


def test_a_missing_client_is_cached_too(monkeypatch):
    """None is a real answer ("not installed"), not a cache miss — otherwise
    every poll re-forks for a client that is not there."""
    import claude_unlimited.daemon as daemon

    calls = []
    monkeypatch.setattr(daemon, "_CLIENT_VERSION_CACHE", {})
    monkeypatch.setattr("claude_unlimited.cli._installed_client_version",
                        lambda: calls.append(1) or None)
    for _ in range(4):
        assert daemon._cached_client_version() is None
    assert len(calls) == 1


# --- finding the client at all ---------------------------------------------

def test_the_client_is_found_outside_PATH(monkeypatch, tmp_path):
    """The daemon runs under launchd/systemd/Task Scheduler with a minimal
    PATH. Without this the dashboard's preview reported
    client_version_unverified on a machine with a perfectly good client
    installed — a wrong answer, not a missing one."""
    import os
    import types
    import claude_unlimited.cli as cli

    # A shebang script is only directly executable on POSIX; Windows needs a
    # real PE binary to run one natively. Since _parse_client_version has its
    # own dedicated coverage above, fake the subprocess call here rather than
    # depend on the OS being able to execute a text file as a program — this
    # test's job is that the fallback path is found and fed to it, not that a
    # shell script can stand in for `claude` on every platform.
    fake = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    fake.write_text("placeholder — never actually executed, see fake_run below")
    if os.name != "nt":
        fake.chmod(0o755)

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "_CLAUDE_FALLBACK_PATHS", (tmp_path / "nope", fake))
    assert cli._claude_executable() == str(fake)

    def fake_run(argv, **kw):
        assert argv == [str(fake), "--version"]
        return types.SimpleNamespace(returncode=0, stdout="2.1.278 (Claude Code)")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._installed_client_version() == (2, 1, 278)


def test_fallback_paths_use_the_real_installed_filename():
    """On Windows the installed file is claude.exe — never the bare name.
    Confirmed on real hardware: shutil.which('claude') resolves to
    %USERPROFILE%\\.local\\bin\\claude.exe, and a bare 'claude' at that path
    does not exist. Without the suffix, _CLAUDE_FALLBACK_PATHS never matches
    on Windows and the client silently reads as unverified whenever PATH is
    minimal (e.g. running under Task Scheduler) — the exact bug this fallback
    list exists to prevent."""
    import claude_unlimited.cli as cli

    suffix = ".exe" if os.name == "nt" else ""
    home_based = cli._CLAUDE_FALLBACK_PATHS[:2]
    assert all(p.name == f"claude{suffix}" for p in home_based)


def test_no_client_anywhere_reads_as_unverified(monkeypatch, tmp_path):
    import claude_unlimited.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "_CLAUDE_FALLBACK_PATHS", (tmp_path / "nope",))
    assert cli._claude_executable() is None
    assert cli._installed_client_version() is None


def test_force_1m_is_the_default_and_the_fallback(tmp_path, monkeypatch):
    """A fresh install, a config that never chose, and a config holding a
    value this build does not know all mean Force 1M."""
    from claude_unlimited import config
    from claude_unlimited.config import Settings

    assert Settings().context_1m == "force_1m"
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    for saved in ({}, {"context_1m": "something-new"}):
        (tmp_path / "config.json").write_text(json.dumps({"profiles": [], "settings": saved}), encoding="utf-8")
        assert config.load_pool().settings.context_1m == "force_1m"
    (tmp_path / "config.json").write_text(json.dumps({"profiles": [], "settings": {"context_1m": "auto"}}),
                                          encoding="utf-8")
    assert config.load_pool().settings.context_1m == "auto"   # a saved choice always wins
