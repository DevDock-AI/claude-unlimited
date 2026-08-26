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


def test_token_is_shaped_like_an_api_key(tmp_path, monkeypatch):
    """Gateways in front of this daemon validate key shape before anything
    else — LiteLLM rejects a key that does not start with 'sk-'."""
    monkeypatch.setattr(pt, "TOKEN_FILE", tmp_path / "placeholder_token")
    monkeypatch.setattr(pt, "APP_DIR", tmp_path)
    token = pt.get_or_create()
    assert token.startswith("sk-")
    assert len(token) > 20  # the prefix must not be the whole story


def test_regenerated_token_keeps_the_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "TOKEN_FILE", tmp_path / "placeholder_token")
    monkeypatch.setattr(pt, "APP_DIR", tmp_path)
    first = pt.get_or_create()
    second = pt.regenerate()
    assert second.startswith("sk-") and second != first


def test_a_token_from_before_the_prefix_is_upgraded_in_place(tmp_path, monkeypatch):
    """An existing install must not keep handing out a key that gateways
    reject, and must not be forced to re-run setup to fix it."""
    token_file = tmp_path / "placeholder_token"
    token_file.write_text("legacyTokenWithoutAPrefix")
    monkeypatch.setattr(pt, "TOKEN_FILE", token_file)
    monkeypatch.setattr(pt, "APP_DIR", tmp_path)

    upgraded = pt.get_or_create()
    assert upgraded == "sk-cu-legacyTokenWithoutAPrefix"
    assert token_file.read_text().strip() == upgraded      # persisted
    assert pt.get_or_create() == upgraded   # stable afterwards
    assert pt.matches(upgraded)
