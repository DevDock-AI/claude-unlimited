import pytest

import claude_unlimited.cli as cli
import claude_unlimited.daemon_installer as daemon_installer
import claude_unlimited.i18n as i18n
import claude_unlimited.profiles as profile_repo


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")


def test_doctor_reports_notifications_availability(env, capsys):
    cli.doctor()
    out = capsys.readouterr().out
    assert "Desktop notifications:" in out


def test_doctor_reports_service_not_installed(env, monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    cli.doctor()
    out = capsys.readouterr().out
    assert "Background service: not installed" in out


def test_doctor_reports_service_installed_and_running(env, monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 42})
    cli.doctor()
    out = capsys.readouterr().out
    assert "Background service: installed — running (pid 42)" in out


def test_doctor_reports_available_languages(env, capsys):
    cli.doctor()
    out = capsys.readouterr().out
    langs = i18n.list_locales()
    for code in langs:
        assert code in out
    assert "current: en" in out
