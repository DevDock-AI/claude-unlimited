from pathlib import Path

import pytest

import claude_unlimited.project_attribution as project_attribution


def test_session_id_from_headers_case_insensitive():
    headers = {"X-Claude-Code-Session-Id": "abc-123", "Other": "x"}
    assert project_attribution.session_id_from_headers(headers) == "abc-123"


def test_session_id_from_headers_missing():
    assert project_attribution.session_id_from_headers({"Other": "x"}) is None


def test_session_id_from_headers_lowercase_key():
    assert project_attribution.session_id_from_headers({"x-claude-code-session-id": "xyz"}) == "xyz"


@pytest.fixture
def fake_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(project_attribution, "PROJECTS_DIR", tmp_path)
    return tmp_path


def test_resolve_project_finds_matching_session(fake_projects_dir):
    proj_dir = fake_projects_dir / "-Users-alice-code-my-app"
    proj_dir.mkdir()
    (proj_dir / "session-1.jsonl").write_text("{}")

    assert project_attribution.resolve_project("session-1") == "-Users-alice-code-my-app"


def test_resolve_project_no_match_returns_none(fake_projects_dir):
    proj_dir = fake_projects_dir / "-Users-alice-code-my-app"
    proj_dir.mkdir()
    (proj_dir / "session-1.jsonl").write_text("{}")

    assert project_attribution.resolve_project("session-unknown") is None


def test_resolve_project_empty_session_id_returns_none(fake_projects_dir):
    assert project_attribution.resolve_project("") is None
    assert project_attribution.resolve_project(None) is None


def test_resolve_project_no_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(project_attribution, "PROJECTS_DIR", tmp_path / "does-not-exist")
    assert project_attribution.resolve_project("session-1") is None


def test_display_name_resolves_to_real_directory_basename():
    # "/Users" exists on macOS and has no embedded hyphen, so the naive
    # reversal is unambiguous: this exercises the "verify then trust" path,
    # distinct from the fallback tested below.
    assert project_attribution.display_name("-Users") == "Users"


def test_display_name_falls_back_to_sanitized_form_when_ambiguous():
    # A directory name containing a hyphen is indistinguishable from a slash
    # under naive reversal, so an unverifiable path must not be asserted.
    result = project_attribution.display_name("-Users-nobody-Work-claude-unlimited-definitely-not-real")
    assert result == "Users-nobody-Work-claude-unlimited-definitely-not-real"


def test_display_name_strips_leading_dash_in_fallback():
    result = project_attribution.display_name("-some-fake-project-xyz-abc")
    assert not result.startswith("-")


def test_display_name_correctly_reconstructs_hyphenated_directory_name():
    # A checkout directory whose name contains a hyphen is the ambiguous case a
    # naive full-reversal gets wrong, since "/".join()-then-"-".split() cannot
    # tell that hyphen from a slash. The filesystem walk must still recover the
    # single segment.
    repo_root = Path(__file__).resolve().parents[1]
    if "-" not in repo_root.name:
        pytest.skip("repo checkout directory has no hyphen in its name here")
    sanitized = "-" + str(repo_root).lstrip("/").replace("/", "-")
    assert project_attribution.display_name(sanitized) == repo_root.name
