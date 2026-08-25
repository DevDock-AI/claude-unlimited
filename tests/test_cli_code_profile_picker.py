import pytest

import claude_unlimited.cli as cli
from claude_unlimited.config import Profile


def _profiles():
    return [
        Profile(id="id-a", name="Alice", kind="oauth"),
        Profile(id="id-b", name="Bob", kind="oauth"),
        Profile(id="id-c", name="API LLM", kind="api"),
    ]


# ---- _match_profile ----

def test_match_profile_by_exact_id():
    assert cli._match_profile(_profiles(), "id-b").name == "Bob"


def test_match_profile_by_exact_name_case_insensitive():
    assert cli._match_profile(_profiles(), "bob").id == "id-b"


def test_match_profile_by_unique_substring():
    assert cli._match_profile(_profiles(), "llm").id == "id-c"


def test_match_profile_no_match_returns_none():
    assert cli._match_profile(_profiles(), "nonexistent") is None


def test_match_profile_ambiguous_substring_returns_none():
    profiles = [Profile(id="1", name="Work A", kind="oauth"), Profile(id="2", name="Work B", kind="oauth")]
    assert cli._match_profile(profiles, "work") is None


# ---- _prompt_profile_choice ----

def test_prompt_default_empty_input_means_rotated_accounts(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert cli._prompt_profile_choice(_profiles()) is None


def test_prompt_explicit_1_means_rotated_accounts(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert cli._prompt_profile_choice(_profiles()) is None


def test_prompt_2_picks_the_first_profile(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    assert cli._prompt_profile_choice(_profiles()).name == "Alice"


def test_prompt_last_option_picks_the_last_profile(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "4")
    assert cli._prompt_profile_choice(_profiles()).name == "API LLM"


def test_prompt_out_of_range_falls_back_to_rotated_accounts(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "99")
    assert cli._prompt_profile_choice(_profiles()) is None
    assert "Rotated accounts" in capsys.readouterr().out


def test_prompt_non_numeric_falls_back_to_rotated_accounts(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "banana")
    assert cli._prompt_profile_choice(_profiles()) is None
    assert "Rotated accounts" in capsys.readouterr().out


# ---- code() wiring ----

@pytest.fixture
def code_env(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)
    execs = []
    monkeypatch.setattr(cli.os, "execvp", lambda file, args: execs.append((file, args)))
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    return execs


def test_code_with_profile_flag_fetches_a_session_token_not_the_placeholder_token(monkeypatch, code_env):
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[Profile(id="a", name="Alice", kind="oauth", enabled=True)]))

    monkeypatch.setattr(cli, "_fetch_session_token", lambda host, port, profile_id, timeout=2.0: f"session-tok-for-{profile_id}")

    def boom(*a, **kw):
        raise AssertionError("must not fetch the shared placeholder token when --profile is given")

    monkeypatch.setattr(cli, "_fetch_placeholder_token", boom)

    assert cli.code(4317, [], profile_arg="Alice") == 0
    assert cli.os.environ["ANTHROPIC_AUTH_TOKEN"] == "session-tok-for-a"
    (binary, argv), = code_env
    assert binary == "claude"
    assert argv[0] == "claude"


def test_code_with_unknown_profile_flag_errors_without_launching(monkeypatch, code_env, capsys):
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[Profile(id="a", name="Alice", kind="oauth", enabled=True)]))

    assert cli.code(4317, [], profile_arg="does-not-exist") == 1
    assert code_env == []  # never launched claude
    assert "does-not-exist" in capsys.readouterr().err


def test_code_with_one_profile_never_prompts(monkeypatch, code_env):
    # Nothing to pick between, so the picker must not block on a single choice.
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[Profile(id="a", name="Alice", kind="oauth", enabled=True)]))

    def boom(prompt):
        raise AssertionError("must not prompt when there's only one enabled profile")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    assert cli.code(4317, [], profile_arg=None) == 0
    assert cli.os.environ["ANTHROPIC_AUTH_TOKEN"] == "placeholder-tok"


def test_code_non_interactive_stdin_never_prompts_even_with_multiple_profiles(monkeypatch, code_env):
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[
        Profile(id="a", name="Alice", kind="oauth", enabled=True),
        Profile(id="b", name="Bob", kind="oauth", enabled=True),
    ]))

    def boom(prompt):
        raise AssertionError("must not prompt when stdin isn't a tty")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")

    assert cli.code(4317, [], profile_arg=None) == 0
    assert cli.os.environ["ANTHROPIC_AUTH_TOKEN"] == "placeholder-tok"
