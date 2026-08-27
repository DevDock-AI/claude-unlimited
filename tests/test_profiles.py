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


def test_upsert_codex_profile_creates_new(fake_store, tmp_path):
    import claude_unlimited.openai_credential as openai_credential

    # A real one, under the isolated-accounts root: delete_profile() rmtree's
    # codex_home, so anything outside that root is refused on the way in.
    codex_home = str(tmp_path / "codex-accounts" / "abc123")
    encoded = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token="tok-a", refresh_token="ref-a", account_id="acct-1", id_token="idtok"))
    profile, reused = profiles.upsert_codex_profile(
        name="My ChatGPT", account_id="acct-1", encoded_credential=encoded, plan="plus", codex_home=codex_home)

    assert reused is False
    assert profile.kind == "codex"
    assert profile.auth_mode == "chatgpt_subscription"
    assert profile.account_uuid == "acct-1"  # reused field, holds the OpenAI account_id
    assert profile.plan == "plus"
    assert profile.codex_home == codex_home
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


# --- input validation: a bad value must never reach config.json -------------
#
# load_pool() COERCES on read — int(priority), float(switch_threshold) — so a
# value that is merely saved without checking becomes an exception on the next
# load. load_pool() is called by every API handler and the proxy path, so one
# unchecked PATCH could leave the daemon unable to serve anything until
# someone hand-edited config.json. These are reachable from
# PATCH /api/profiles/<id>, which passes the decoded JSON body straight in.


def _a_profile(fake_store):
    return profiles.create_profile(name="X", kind="api", credential="sk-ant-12345678")


@pytest.mark.parametrize("changes", [
    {"priority": None},
    {"priority": "abc"},
    {"priority": 0},
    {"switch_threshold": "abc"},
    {"switch_threshold": 101},
    {"switch_threshold": -1},
    {"token_threshold": "5000"},      # loads fine, then crashes on int >= str
    {"monthly_budget_cap": "10"},
    {"enabled": "false"},             # truthy string silently ENABLES
    {"automatic": 1},
    {"name": 42},
    {"codex_reasoning_effort": "turbo"},
])
def test_update_profile_refuses_a_value_that_would_break_the_next_load(fake_store, changes):
    p = _a_profile(fake_store)
    with pytest.raises(profiles.ValidationError):
        profiles.update_profile(p.id, **changes)

    # And the stored config is still loadable, which is the property that
    # actually matters.
    assert profiles.list_profiles()[0].id == p.id


def test_update_profile_still_accepts_the_real_values_the_dashboard_sends(fake_store):
    p = _a_profile(fake_store)
    updated = profiles.update_profile(
        p.id, priority=3, switch_threshold=80.5, enabled=False, automatic=True,
        token_threshold=5000, monthly_budget_cap=25.0, name="Renamed")
    assert (updated.priority, updated.switch_threshold) == (3, 80.5)
    assert updated.enabled is False and updated.automatic is True
    assert (updated.token_threshold, updated.monthly_budget_cap) == (5000, 25.0)
    # Ints where a float is expected are normal JSON and must keep working.
    assert profiles.update_profile(p.id, switch_threshold=90).switch_threshold == 90


def test_codex_home_cannot_be_pointed_outside_the_isolated_accounts_root(fake_store, tmp_path):
    """delete_profile() does rmtree(codex_home). Without this check, a PATCH
    could aim that at any directory and a later delete would erase it."""
    p = _a_profile(fake_store)
    for hostile in ("/Users", str(tmp_path / "Documents"),
                    str(tmp_path / "codex-accounts" / ".." / "Documents")):
        with pytest.raises(profiles.ValidationError):
            profiles.update_profile(p.id, codex_home=hostile)

    inside = str(tmp_path / "codex-accounts" / "deadbeef")
    assert profiles.update_profile(p.id, codex_home=inside).codex_home == inside


def test_claude_config_dir_is_constrained_the_same_way(fake_store, tmp_path):
    p = _a_profile(fake_store)
    with pytest.raises(profiles.ValidationError):
        profiles.update_profile(p.id, claude_config_dir=str(tmp_path / "elsewhere"))
    ok = str(tmp_path / "claude-accounts" / "cafe")
    assert profiles.update_profile(p.id, claude_config_dir=ok).claude_config_dir == ok


def test_create_profile_refuses_the_same_bad_values(fake_store):
    with pytest.raises(profiles.ValidationError):
        profiles.create_profile(name="X", kind="api", credential="sk-ant-12345678",
                                switch_threshold="abc")
    with pytest.raises(profiles.ValidationError):
        profiles.create_profile(name="X", kind="api", credential="sk-ant-12345678",
                                codex_home="/tmp/anywhere")


# --- deleting an account must not leave its credentials behind -------------


def test_delete_removes_both_isolated_login_directories(fake_store, tmp_path, monkeypatch):
    """Each holds a LIVE refresh token. Only codex_home used to be removed, so
    deleting a Claude account from the Dashboard left its credential on disk
    forever — only `purge` ever cleaned those up."""
    claude_dir = tmp_path / "claude-accounts" / "aaa"
    codex_dir = tmp_path / "codex-accounts" / "bbb"
    for d in (claude_dir, codex_dir):
        d.mkdir(parents=True)
    (claude_dir / ".credentials.json").write_text('{"refresh_token": "live"}')
    (codex_dir / "auth.json").write_text('{"refresh_token": "live"}')

    p = profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678",
                                account_uuid="u1", claude_config_dir=str(claude_dir))
    p = profiles.update_profile(p.id, codex_home=str(codex_dir))

    keychain = []
    monkeypatch.setattr("claude_unlimited.anthropic_oauth.remove_isolated_logins",
                        lambda dirs: keychain.extend(dirs) or len(dirs))

    profiles.delete_profile(p.id)

    assert not claude_dir.exists(), "the Claude login directory survived the delete"
    assert not codex_dir.exists()
    assert keychain == [str(claude_dir)], "the derived Keychain entry was not removed"


def test_reset_all_profiles_cleans_up_the_same_way(fake_store, tmp_path, monkeypatch):
    """"Remove everything" that leaves credentials behind has not removed
    everything."""
    claude_dir = tmp_path / "claude-accounts" / "ccc"
    claude_dir.mkdir(parents=True)
    (claude_dir / ".credentials.json").write_text('{"refresh_token": "live"}')
    profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678",
                            account_uuid="u1", claude_config_dir=str(claude_dir))

    keychain = []
    monkeypatch.setattr("claude_unlimited.anthropic_oauth.remove_isolated_logins",
                        lambda dirs: keychain.extend(dirs) or len(dirs))

    assert profiles.reset_all_profiles() == 1
    assert not claude_dir.exists()
    assert keychain == [str(claude_dir)]


def test_delete_still_succeeds_when_the_directory_is_already_gone(fake_store, tmp_path):
    """The Profile is out of config by this point; tidying up failing must not
    turn a completed delete into an error."""
    p = profiles.create_profile(name="X", kind="oauth", credential="sk-ant-12345678",
                                account_uuid="u1",
                                claude_config_dir=str(tmp_path / "claude-accounts" / "never-made"))
    profiles.delete_profile(p.id)
    assert profiles.list_profiles() == []
