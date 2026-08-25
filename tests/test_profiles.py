import pytest

import claude_unlimited.activity as activity
import claude_unlimited.profiles as profiles


class FakeSecretStore:
    """In-memory stand-in for the Keychain backend, so tests never touch the
    OS credential store."""

    def __init__(self):
        self.tokens: dict[str, str] = {}
        self.fail_set = False

    def set_token(self, profile_id, token):
        if self.fail_set:
            raise RuntimeError("simulated Keychain failure")
        self.tokens[profile_id] = token

    def get_token(self, profile_id):
        return self.tokens[profile_id]

    def delete_token(self, profile_id):
        self.tokens.pop(profile_id, None)

    def has_token(self, profile_id):
        return profile_id in self.tokens


@pytest.fixture
def fake_store(monkeypatch, tmp_path):
    store = FakeSecretStore()
    monkeypatch.setattr(profiles, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    return store


def test_create_profile_with_no_explicit_priority_appends_after_existing_ones(fake_store):
    # The CLI flows (add_account, add_codex_account) pass no priority, so a
    # new Profile must slot in after the existing ones rather than tying with
    # whatever already holds top priority.
    profiles.create_profile(name="First", kind="oauth", credential="sk-ant-12345678", account_uuid="u1")
    profiles.create_profile(name="Second", kind="oauth", credential="sk-ant-87654321", account_uuid="u2")
    third = profiles.create_profile(name="Third", kind="api", credential="sk-ant-abcdefgh")
    assert third.priority == 3


def test_create_profile_explicit_priority_is_still_honored(fake_store):
    # The Dashboard's Add-Profile form computes a priority client-side, which
    # must not be overridden.
    p = profiles.create_profile(name="X", kind="api", credential="sk-ant-12345678", priority=7)
    assert p.priority == 7


def test_create_oauth_profile(fake_store):
    p = profiles.create_profile(name="Personal Max", kind="oauth", credential="sk-ant-oat-12345678",
                                 account_uuid="acct-test")
    assert p.kind == "oauth"
    assert p.account_uuid == "acct-test"
    assert fake_store.get_token(p.id) == "sk-ant-oat-12345678"
    assert [x.id for x in profiles.list_profiles()] == [p.id]


def test_create_oauth_profile_without_account_uuid_is_rejected(fake_store):
    with pytest.raises(profiles.ValidationError):
        profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678")


def test_create_api_profile_requires_https_base_url(fake_store):
    with pytest.raises(profiles.ValidationError):
        profiles.create_profile(
            name="Bad Gateway", kind="api", credential="sk-ant-12345678",
            base_url="http://insecure.example",
        )


def test_create_api_profile_accepts_https_base_url(fake_store):
    p = profiles.create_profile(
        name="Team Gateway", kind="api", credential="sk-ant-12345678",
        base_url="https://gateway.example/v1", auth_mode="bearer",
    )
    assert p.base_url == "https://gateway.example/v1"


def test_create_rejects_empty_name(fake_store):
    with pytest.raises(profiles.ValidationError):
        profiles.create_profile(name="  ", kind="oauth", credential="sk-ant-12345678")


def test_create_rejects_short_credential(fake_store):
    with pytest.raises(profiles.ValidationError):
        profiles.create_profile(name="X", kind="oauth", credential="short")


def test_create_rejects_unknown_kind(fake_store):
    with pytest.raises(profiles.ValidationError):
        profiles.create_profile(name="X", kind="gateway", credential="sk-ant-12345678")


def test_upsert_codex_profile_creates_new(fake_store):
    import claude_unlimited.openai_credential as openai_credential

    encoded = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token="tok-a", refresh_token="ref-a", account_id="acct-1", id_token="idtok"))
    profile, reused = profiles.upsert_codex_profile(
        name="My ChatGPT", account_id="acct-1", encoded_credential=encoded, plan="plus", codex_home="/tmp/codex-home-1")

    assert reused is False
    assert profile.kind == "codex"
    assert profile.auth_mode == "chatgpt_subscription"
    assert profile.account_uuid == "acct-1"  # reused field, holds the OpenAI account_id
    assert profile.plan == "plus"
    assert profile.codex_home == "/tmp/codex-home-1"
    # Stored exactly as passed, not re-wrapped in oauth_credential's shape, so
    # openai_credential.decode() can read it straight back.
    assert openai_credential.decode(fake_store.get_token(profile.id)).access_token == "tok-a"


def test_upsert_codex_profile_refreshes_existing_by_account_id(fake_store):
    import claude_unlimited.openai_credential as openai_credential

    first = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token="tok-old", refresh_token="ref-old", account_id="acct-1", id_token=None))
    created, _ = profiles.upsert_codex_profile(name="A", account_id="acct-1", encoded_credential=first)

    second = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token="tok-new", refresh_token="ref-new", account_id="acct-1", id_token=None))
    updated, reused = profiles.upsert_codex_profile(name="A", account_id="acct-1", encoded_credential=second, plan="pro")

    assert reused is True
    assert updated.id == created.id  # same Profile, not a duplicate
    assert len(profiles.list_profiles()) == 1
    assert updated.plan == "pro"
    assert openai_credential.decode(fake_store.get_token(created.id)).access_token == "tok-new"


def test_update_credential_raw_stores_the_blob_as_is_and_stamps_credential_updated_at(fake_store):
    import claude_unlimited.openai_credential as openai_credential

    encoded = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token="tok-a", refresh_token=None, account_id="acct-1", id_token=None))
    profile, _ = profiles.upsert_codex_profile(name="A", account_id="acct-1", encoded_credential=encoded)
    assert profiles.list_profiles()[0].credential_updated_at is None

    refreshed = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token="tok-b", refresh_token=None, account_id="acct-1", id_token=None))
    profiles.update_credential_raw(profile.id, refreshed)

    assert openai_credential.decode(fake_store.get_token(profile.id)).access_token == "tok-b"
    assert profiles.list_profiles()[0].credential_updated_at is not None


def test_create_rolls_back_keychain_if_config_save_fails(fake_store, monkeypatch):
    def boom(pool):
        raise OSError("disk full")

    monkeypatch.setattr(profiles, "save_pool", boom)
    with pytest.raises(profiles.ProfileRepositoryError):
        profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678", account_uuid="acct-x")
    assert fake_store.tokens == {}


def test_create_raises_when_keychain_write_fails_before_any_config_write(fake_store):
    fake_store.fail_set = True
    with pytest.raises(profiles.ProfileRepositoryError):
        profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678", account_uuid="acct-x")
    assert profiles.list_profiles() == []


def test_update_profile_changes_allowed_fields(fake_store):
    p = profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678", account_uuid="acct-x")
    updated = profiles.update_profile(p.id, priority=2, switch_threshold=95.0, enabled=False)
    assert updated.priority == 2
    assert updated.switch_threshold == 95.0
    assert updated.enabled is False


def test_update_profile_rejects_unknown_field(fake_store):
    p = profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678", account_uuid="acct-x")
    with pytest.raises(profiles.ValidationError):
        profiles.update_profile(p.id, kind="api")


def test_update_missing_profile_raises(fake_store):
    with pytest.raises(profiles.ProfileRepositoryError):
        profiles.update_profile("nonexistent", priority=1)


def test_delete_profile_removes_config_and_credential(fake_store):
    p = profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678", account_uuid="acct-x")
    profiles.delete_profile(p.id)
    assert profiles.list_profiles() == []
    assert not fake_store.has_token(p.id)


def test_delete_missing_profile_raises(fake_store):
    with pytest.raises(profiles.ProfileRepositoryError):
        profiles.delete_profile("nonexistent")


def test_deleting_a_non_last_profile_renumbers_priorities_with_no_gap(fake_store):
    # Deleting priority 4 out of {1,2,3,4,5} must compact to {1,2,3,4}. Left as
    # {1,2,3,5}, the next add-profile flow's max+1 slot builds on the gap
    # forever and never reclaims it.
    p1 = profiles.create_profile(name="A", kind="api", credential="sk-ant-11111111", priority=1)
    p2 = profiles.create_profile(name="B", kind="api", credential="sk-ant-22222222", priority=2)
    p3 = profiles.create_profile(name="C", kind="api", credential="sk-ant-33333333", priority=3)
    p4 = profiles.create_profile(name="D", kind="api", credential="sk-ant-44444444", priority=4)
    p5 = profiles.create_profile(name="E", kind="api", credential="sk-ant-55555555", priority=5)

    profiles.delete_profile(p4.id)

    remaining = {p.name: p.priority for p in profiles.list_profiles()}
    assert remaining == {"A": 1, "B": 2, "C": 3, "E": 4}

    # And the next Profile reclaims the compacted slot instead of continuing
    # from the old high-water mark.
    next_one = profiles.create_profile(name="F", kind="api", credential="sk-ant-66666666")
    assert next_one.priority == 5


def test_deleting_the_last_profile_by_priority_needs_no_renumbering(fake_store):
    p1 = profiles.create_profile(name="A", kind="api", credential="sk-ant-11111111", priority=1)
    p2 = profiles.create_profile(name="B", kind="api", credential="sk-ant-22222222", priority=2)
    profiles.delete_profile(p2.id)
    assert [p.priority for p in profiles.list_profiles()] == [1]
