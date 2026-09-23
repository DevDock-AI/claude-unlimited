import json

import pytest

import claude_unlimited.activity as activity
import claude_unlimited.export_import as ei
import claude_unlimited.profiles as profile_repo
from claude_unlimited.config import load_pool


class FakeSecretStore:
    def __init__(self):
        self.tokens = {}

    def set_token(self, profile_id, token):
        self.tokens[profile_id] = token

    def get_token(self, profile_id):
        return self.tokens[profile_id]

    def delete_token(self, profile_id):
        self.tokens.pop(profile_id, None)

    def has_token(self, profile_id):
        return profile_id in self.tokens


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr(ei, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(ei, "activity_module", activity)
    return store


def test_export_profiles_requires_passphrase(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="u1")
    with pytest.raises(ei.ExportImportError):
        ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False)


def test_export_settings_only_needs_no_passphrase_and_is_plaintext(env):
    bundle = ei.build_export_bundle(include_profiles=False, include_settings=True, include_activity=False)
    envelope = json.loads(bundle)
    assert envelope["encrypted"] is False
    assert "settings" in envelope["data"]


def test_export_import_roundtrip_with_correct_passphrase(env):
    profile_repo.create_profile(name="Personal Max", kind="oauth", credential="tok-real-long", account_uuid="u1")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=True, include_activity=False,
                                     passphrase="correct horse battery staple")

    envelope = json.loads(bundle)
    assert envelope["encrypted"] is True
    assert "profiles" not in envelope  # not visible in the clear anywhere in the envelope

    parsed = ei.import_bundle(bundle, passphrase="correct horse battery staple")
    assert len(parsed.profiles) == 1
    assert parsed.profiles[0]["credential"] == "tok-real-long"
    assert parsed.profiles[0]["name"] == "Personal Max"


def test_import_wrong_passphrase_raises_specific_error(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="u1")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="right-passphrase")
    with pytest.raises(ei.WrongPassphraseError):
        ei.import_bundle(bundle, passphrase="wrong-passphrase")


def test_import_encrypted_bundle_without_passphrase_raises_clear_error(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="u1")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="secret")
    with pytest.raises(ei.ExportImportError):
        ei.import_bundle(bundle)


def test_import_bundle_never_writes_anything(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="u1")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="secret")
    ei.import_bundle(bundle, passphrase="secret")
    # Still exactly the one original profile — import_bundle is read-only.
    assert len(load_pool().profiles) == 1


def test_apply_import_adds_new_profile(env):
    bundle_profiles = [{
        "name": "Imported", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "new-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False)
    assert result["profiles_added"] == 1
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert env.get_token(pool.profiles[0].id) == "imported-tok-long"


def test_apply_import_keep_existing_skips_conflicting_profile(env):
    profile_repo.create_profile(name="Existing", kind="oauth", credential="original-tok-long", account_uuid="dup-uuid")
    bundle_profiles = [{
        "name": "Imported version", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "dup-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="keep_existing")
    assert result["profiles_skipped"] == 1
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert pool.profiles[0].name == "Existing"


def test_apply_import_use_imported_updates_conflicting_credential(env):
    existing = profile_repo.create_profile(name="Existing", kind="oauth", credential="original-tok-long",
                                            account_uuid="dup-uuid")
    bundle_profiles = [{
        "name": "Imported version", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "dup-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")
    assert result["profiles_updated"] == 1
    assert env.get_token(existing.id) == "imported-tok-long"
    pool = load_pool()
    assert len(pool.profiles) == 1  # still no duplicate row


def test_apply_import_use_imported_updates_the_whole_profile_not_just_credential(env):
    # "Use imported version" must apply every bundle field (name, priority,
    # threshold, enabled, automatic, tag_color, base_url, default_model,
    # budget cap), not just the stored credential.
    existing = profile_repo.create_profile(name="Existing", kind="oauth", credential="original-tok-long",
                                            account_uuid="dup-uuid", priority=1, switch_threshold=98.0)
    bundle_profiles = [{
        "name": "Renamed on the other machine", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 3, "switch_threshold": 90.0, "enabled": False, "automatic": False,
        "default_model": "claude-opus-5", "monthly_budget_cap": 50.0, "tag_color": "#FFB020",
        "account_uuid": "dup-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")
    assert result["profiles_updated"] == 1
    pool = load_pool()
    assert len(pool.profiles) == 1
    updated = pool.profiles[0]
    assert updated.id == existing.id  # same row, not a duplicate
    assert updated.name == "Renamed on the other machine"
    assert updated.priority == 3
    assert updated.switch_threshold == 90.0
    assert updated.enabled is False
    assert updated.automatic is False
    assert updated.default_model == "claude-opus-5"
    assert updated.monthly_budget_cap == 50.0
    assert updated.tag_color == "#FFB020"


def test_export_then_apply_import_roundtrips_token_threshold(env):
    # A field added to ExportedProfile/build_export_bundle/apply_import in only
    # two of those three spots is silently dropped.
    profile_repo.create_profile(name="X", kind="api", credential="tok-long-enough-key", token_threshold=250000)
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="correct horse battery staple")
    parsed = ei.import_bundle(bundle, passphrase="correct horse battery staple")
    assert parsed.profiles[0]["token_threshold"] == 250000

    # No account_uuid on an api-kind Profile — nothing to match against, so
    # this exercises apply_import's ADD path (a fresh new_profile row).
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False)
    assert result["profiles_added"] == 1
    imported = [p for p in load_pool().profiles if p.name == "X"][-1]
    assert imported.token_threshold == 250000


def test_export_then_apply_import_roundtrips_both_model_fields(env):
    # force_model and default_model together, through export, import's ADD
    # path, and its "use imported" UPDATE path.
    profile_repo.create_profile(name="Local", kind="api", credential="tok-long-enough-key",
                                base_url="http://127.0.0.1:5566",
                                force_model="local-coder-model", default_model="local-fallback-model")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="correct horse battery staple")
    parsed = ei.import_bundle(bundle, passphrase="correct horse battery staple")
    assert parsed.profiles[0]["force_model"] == "local-coder-model"
    assert parsed.profiles[0]["default_model"] == "local-fallback-model"

    ei.apply_import(parsed, import_profiles=True, import_settings=False)
    imported = [p for p in load_pool().profiles if p.name == "Local"][-1]
    assert (imported.force_model, imported.default_model) == ("local-coder-model", "local-fallback-model")


def test_apply_import_settings(env):
    parsed = ei.ParsedBundle(profiles=[], settings={"update_mode": "manual"}, activity=None)
    result = ei.apply_import(parsed, import_profiles=False, import_settings=True)
    assert result["settings_applied"] is True
    assert load_pool().settings.update_mode == "manual"


def test_unsupported_bundle_version_rejected(env):
    bad = json.dumps({"bundle_version": 999, "encrypted": False, "data": {}}).encode()
    with pytest.raises(ei.ExportImportError):
        ei.import_bundle(bad)


def test_not_json_raises_clear_error(env):
    with pytest.raises(ei.ExportImportError):
        ei.import_bundle(b"not json at all")


def test_the_account_tier_survives_an_export_import_round_trip(env):
    """`plan` ("max"/"pro") is discovered only by the add-account and
    import-login flows, and nothing recomputes it afterwards — so a Profile
    restored from a bundle used to show no tier in the Dashboard forever."""
    profile_repo.create_profile(name="Max account", kind="oauth", credential="sk-ant-12345678",
                            account_uuid="acct-1", plan="max")
    bundle = ei.build_export_bundle(
        include_profiles=True, include_settings=False, include_activity=False,
        passphrase="hunter22")

    profile_repo.reset_all_profiles()
    parsed = ei.import_bundle(bundle, passphrase="hunter22")
    ei.apply_import(parsed, import_profiles=True, import_settings=False)

    assert [p.plan for p in profile_repo.list_profiles()] == ["max"]


def test_importing_settings_does_not_reset_fields_the_bundle_never_carried(env):
    """A bundle exported by a version predating a field simply has no key for
    it. Rebuilding Settings from scratch turned that into "reset it to the
    default", silently changing preferences of whoever imported."""
    from claude_unlimited.config import update_settings

    update_settings(language="ro", notifications_enabled=False)
    parsed = ei.ParsedBundle(
        profiles=[], settings={"update_mode": "manual"}, activity=None)

    ei.apply_import(parsed, import_profiles=False, import_settings=True)

    settings = load_pool().settings
    assert settings.update_mode == "manual"     # what the bundle asked for
    assert settings.language == "ro"            # untouched, not reset to "en"
    assert settings.notifications_enabled is False


def _round_trip_bundle():
    return ei.build_export_bundle(include_profiles=True, include_settings=False,
                                  include_activity=False, passphrase="hunter22")


def test_forced_in_subagents_survives_an_export_import_round_trip(env):
    profile_repo.create_profile(name="GPT", kind="oauth", credential="sk-ant-12345678",
                                account_uuid="acct-1", forced_for_subagents=True)
    bundle = _round_trip_bundle()
    profile_repo.reset_all_profiles()
    ei.apply_import(ei.import_bundle(bundle, passphrase="hunter22"), import_profiles=True, import_settings=False)
    assert [p.forced_for_subagents for p in profile_repo.list_profiles()] == [True]


def test_importing_a_second_forced_profile_keeps_the_holder_already_routing(env):
    # Used to fail inside save_pool() with two holders, aborting the import.
    profile_repo.create_profile(name="Old", kind="oauth", credential="sk-ant-12345678",
                                account_uuid="acct-1", forced_for_subagents=True)
    bundle = _round_trip_bundle()
    profile_repo.reset_all_profiles()
    profile_repo.create_profile(name="Current", kind="oauth", credential="sk-ant-87654321",
                                account_uuid="acct-2", forced_for_subagents=True)

    ei.apply_import(ei.import_bundle(bundle, passphrase="hunter22"), import_profiles=True, import_settings=False)
    assert {p.name: p.forced_for_subagents for p in profile_repo.list_profiles()} == {"Current": True, "Old": False}



def test_leave_on_fable_limit_survives_an_export_import_round_trip(env):
    profile_repo.create_profile(name="Leaver", kind="oauth", credential="sk-ant-12345678",
                                account_uuid="acct-1", leave_on_fable_limit=True)
    profile_repo.create_profile(name="Stayer", kind="oauth", credential="sk-ant-87654321",
                                account_uuid="acct-2")
    bundle = _round_trip_bundle()
    profile_repo.reset_all_profiles()
    ei.apply_import(ei.import_bundle(bundle, passphrase="hunter22"), import_profiles=True, import_settings=False)
    assert {p.name: p.leave_on_fable_limit for p in profile_repo.list_profiles()} == {"Leaver": True, "Stayer": False}

    # "Use imported version" applies the field to an existing profile too.
    profile_repo.update_profile(profile_repo.list_profiles()[0].id, leave_on_fable_limit=False)
    ei.apply_import(ei.import_bundle(bundle, passphrase="hunter22"), import_profiles=True,
                    import_settings=False, conflict_strategy="use_imported")
    assert {p.name: p.leave_on_fable_limit for p in profile_repo.list_profiles()} == {"Leaver": True, "Stayer": False}
