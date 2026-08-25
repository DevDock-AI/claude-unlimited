import pytest

import claude_unlimited.i18n as i18n


def test_list_locales_includes_en_and_es():
    codes = i18n.list_locales()
    assert "en" in codes
    assert "es" in codes


def test_load_locale_en_has_no_missing_keys_relative_to_itself():
    strings = i18n.load_locale("en")
    assert strings["nav.overview"] == "Overview"


def test_every_locale_file_covers_every_en_key():
    en_keys = set(i18n._read_locale_file("en").keys()) - {"_meta"}
    for code in i18n.list_locales():
        if code == "en":
            continue
        merged = i18n.load_locale(code)
        missing = en_keys - set(merged.keys())
        assert not missing, f"{code} is missing keys (should have fallen back): {missing}"


def test_load_locale_falls_back_to_english_for_missing_key(monkeypatch):
    monkeypatch.setattr(i18n, "_read_locale_file", lambda code: (
        {"_meta": {"language_name": "Test"}} if code == "xx" else
        {"_meta": {"language_name": "English"}, "nav.overview": "Overview"}
    ))
    monkeypatch.setattr(i18n, "list_locales", lambda: ["en", "xx"])
    merged = i18n.load_locale("xx")
    assert merged["nav.overview"] == "Overview"


def test_load_locale_unknown_code_raises():
    with pytest.raises(i18n.UnknownLocaleError):
        i18n.load_locale("zz")


def test_load_locale_rejects_path_traversal():
    with pytest.raises(i18n.UnknownLocaleError):
        i18n.load_locale("../../../etc/passwd")


def test_locale_display_names_has_entry_per_locale():
    names = i18n.locale_display_names()
    assert names["en"] == "English"
    assert names["es"] == "Español"
