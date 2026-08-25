import dataclasses

import claude_unlimited.connectors as connectors
from claude_unlimited.config import Profile


def test_every_registered_kind_matches_its_own_spec_kind_field():
    for name, spec in connectors.CONNECTORS.items():
        assert spec.kind == name


def test_every_declared_profile_field_actually_exists_on_the_profile_dataclass():
    real_fields = {f.name for f in dataclasses.fields(Profile)}
    for spec in connectors.CONNECTORS.values():
        unknown = set(spec.profile_fields) - real_fields
        assert not unknown, f"{spec.kind} declares nonexistent Profile fields: {unknown}"


def test_quota_style_is_one_of_the_known_values():
    for spec in connectors.CONNECTORS.values():
        assert spec.quota_style in ("percent", "token_budget", "none")


def test_get_returns_the_right_spec():
    assert connectors.get("codex").kind == "codex"


def test_get_unknown_kind_raises_clear_error():
    try:
        connectors.get("nonexistent")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "nonexistent" in str(exc)


def test_all_auth_modes_is_the_union_with_no_duplicates():
    modes = connectors.all_auth_modes()
    assert len(modes) == len(set(modes))
    assert "api_key" in modes
    assert "chatgpt_subscription" in modes
    assert "bearer" in modes


def test_only_oauth_requires_account_uuid():
    assert connectors.CONNECTORS["oauth"].requires_account_uuid is True
    assert connectors.CONNECTORS["api"].requires_account_uuid is False
    assert connectors.CONNECTORS["codex"].requires_account_uuid is False


def test_existing_three_kinds_are_all_registered():
    assert set(connectors.CONNECTORS) == {"oauth", "api", "codex"}
