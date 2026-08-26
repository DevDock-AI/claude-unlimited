

def test_codex_pinned_session_relabels_the_model_picker(monkeypatch):
    """Claude Code builds `/model` client-side from these env vars rather than
    asking the proxy, so a codex-pinned session must relabel the picker
    itself."""
    from claude_unlimited import cli
    from claude_unlimited.config import Profile

    for tier in ("FABLE", "OPUS", "SONNET", "HAIKU"):
        for suffix in ("", "_NAME", "_DESCRIPTION"):
            monkeypatch.delenv(f"ANTHROPIC_DEFAULT_{tier}_MODEL{suffix}", raising=False)

    cli._apply_model_labels(Profile(id="c", name="Codex", kind="codex", priority=1,
                                     automatic=True, enabled=True), [])

    import os
    # The id must stay Anthropic-shaped: openai_models.map_model is keyed on it.
    assert os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-5"
    # ...while the visible label names the backing model.
    assert os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"] == "GPT-5.6 Terra"
    desc = os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION"]
    assert "Codex" in desc and "high" in desc, desc


def test_non_codex_session_leaves_the_model_picker_alone(monkeypatch):
    """Without a pinned codex Profile the pool can rotate to any kind
    mid-session, so labelling every tier as a GPT model would be wrong."""
    from claude_unlimited import cli
    from claude_unlimited.config import Profile
    import os

    monkeypatch.delenv("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", raising=False)
    cli._apply_model_labels(None, [Profile(id="a", name="A", kind="oauth", priority=1,
                                            automatic=True, enabled=True, account_uuid="u")])
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME" not in os.environ

    cli._apply_model_labels(Profile(id="a", name="A", kind="oauth", priority=1,
                                     automatic=True, enabled=True, account_uuid="u"), [])
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME" not in os.environ


def test_user_set_model_labels_are_never_overridden(monkeypatch):
    from claude_unlimited import cli
    from claude_unlimited.config import Profile
    import os

    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "My Own Label")
    cli._apply_model_labels(Profile(id="c", name="Codex", kind="codex", priority=1,
                                     automatic=True, enabled=True), [])
    assert os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"] == "My Own Label"


def _p(kind, pid="x", **kw):
    from claude_unlimited.config import Profile
    if kind == "oauth":
        kw.setdefault("account_uuid", "u")
    return Profile(id=pid, name=pid, kind=kind, priority=1, automatic=True, enabled=True, **kw)


def _clear(monkeypatch):
    for tier in ("FABLE", "OPUS", "SONNET", "HAIKU"):
        for suffix in ("", "_NAME", "_DESCRIPTION"):
            monkeypatch.delenv(f"ANTHROPIC_DEFAULT_{tier}_MODEL{suffix}", raising=False)


def test_rotated_mixed_pool_uses_provider_neutral_labels(monkeypatch):
    """The picker is read once at launch and can never be updated, but a
    rotated session's provider changes per request — so no vendor-specific
    label can stay correct."""
    from claude_unlimited import cli
    import os
    _clear(monkeypatch)

    cli._apply_model_labels(None, [_p("oauth", "a"), _p("codex", "c")])

    # Names BOTH models the tier maps to: accurate whoever serves, and still
    # says what is being picked.
    assert os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"] == "Sonnet 5 / GPT-5.6 Terra"
    labels = [os.environ[f"ANTHROPIC_DEFAULT_{t}_MODEL_NAME"] for t in ("FABLE", "OPUS", "SONNET", "HAIKU")]
    assert len(set(labels)) == len(labels), labels  # every entry distinguishable
    assert all("GPT" in l for l in labels), labels  # both providers named
    # Every description must state the reasoning level that tier maps to.
    for tier, level in (("FABLE", "max"), ("OPUS", "high"), ("SONNET", "medium"), ("HAIKU", "low")):
        assert level in os.environ[f"ANTHROPIC_DEFAULT_{tier}_MODEL_DESCRIPTION"]
    # ...and the id still has to be one map_model() understands.
    assert os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-5"


def test_rotated_all_claude_pool_keeps_native_labels(monkeypatch):
    """Nothing can be mislabelled when every account is Claude, so don't
    replace Claude Code's own labels with worse generic ones."""
    from claude_unlimited import cli
    import os
    _clear(monkeypatch)

    cli._apply_model_labels(None, [_p("oauth", "a"), _p("api", "b")])
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME" not in os.environ


def test_status_line_shows_the_dashboard_url(monkeypatch, tmp_path):
    """The launch banner scrolls away; the status line keeps the Dashboard URL
    visible for the whole session."""
    from claude_unlimited import cli
    import json

    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.chdir(tmp_path)

    args = cli._status_line_args(4317, [])
    assert args[0] == "--settings"
    settings = json.loads(args[1])
    assert settings["statusLine"]["type"] == "command"
    assert "127.0.0.1:4317" in settings["statusLine"]["command"]


def test_status_line_never_overrides_one_the_user_configured(monkeypatch, tmp_path):
    from claude_unlimited import cli
    import json

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.chdir(tmp_path)

    assert cli._status_line_args(4317, []) == []


def test_status_line_yields_to_a_user_supplied_settings_flag(monkeypatch, tmp_path):
    from claude_unlimited import cli

    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.chdir(tmp_path)

    assert cli._status_line_args(4317, ["--settings", "x.json"]) == []
    assert cli._status_line_args(4317, ["--settings=x.json"]) == []


def _routing_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:4317")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-cu-local")


def test_a_project_pinning_the_base_url_is_overridden(monkeypatch, tmp_path):
    """Claude Code applies a settings file's env on top of the process
    environment, so a project that pins ANTHROPIC_BASE_URL sends every request
    somewhere else with whatever credential it carries — silently bypassing
    the pool the session was launched for."""
    import json as _json
    from claude_unlimited import cli

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(_json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "https://gateway.example", "ANTHROPIC_AUTH_TOKEN": "theirs"},
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    _routing_env(monkeypatch)

    args = cli._status_line_args(4317, [])
    settings = _json.loads(args[args.index("--settings") + 1])
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4317"
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-cu-local"


def test_only_the_routing_keys_are_touched(monkeypatch, tmp_path):
    """A project's own env is its business — only the three keys that decide
    where traffic goes are reasserted."""
    import json as _json
    from claude_unlimited import cli

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(_json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "https://gateway.example", "MY_PROJECT_FLAG": "keep me"},
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    _routing_env(monkeypatch)

    settings = _json.loads(cli._status_line_args(4317, [])[1])
    assert set(settings["env"]) == {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}
    assert "MY_PROJECT_FLAG" not in settings["env"]


def test_a_project_without_routing_env_is_left_alone(monkeypatch, tmp_path):
    import json as _json
    from claude_unlimited import cli

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(_json.dumps({"env": {"EDITOR": "vim"}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    _routing_env(monkeypatch)

    args = cli._status_line_args(4317, [])
    settings = _json.loads(args[1]) if args else {}
    assert "env" not in settings


def test_a_user_supplied_settings_flag_still_wins(monkeypatch, tmp_path):
    from claude_unlimited import cli
    _routing_env(monkeypatch)
    assert cli._status_line_args(4317, ["--settings", "mine.json"]) == []
