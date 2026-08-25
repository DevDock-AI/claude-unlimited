from claude_unlimited import placeholder_token as pt


def test_get_or_create_is_stable_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "APP_DIR", tmp_path)
    monkeypatch.setattr(pt, "TOKEN_FILE", tmp_path / "placeholder_token")
    a = pt.get_or_create()
    b = pt.get_or_create()
    assert a == b
    assert len(a) > 20


def test_matches_true_for_real_token_false_for_wrong_one(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "APP_DIR", tmp_path)
    monkeypatch.setattr(pt, "TOKEN_FILE", tmp_path / "placeholder_token")
    real = pt.get_or_create()
    assert pt.matches(real) is True
    assert pt.matches("definitely-wrong") is False


def test_regenerate_changes_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "APP_DIR", tmp_path)
    monkeypatch.setattr(pt, "TOKEN_FILE", tmp_path / "placeholder_token")
    first = pt.get_or_create()
    second = pt.regenerate()
    assert first != second
    assert pt.matches(second) is True
    assert pt.matches(first) is False
