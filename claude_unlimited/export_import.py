"""Export/Import bundles.

`cryptography` is the single exception to the dependency-free-backend rule,
and its use is confined to this file. A bundle containing Profiles (and
therefore credentials) is ALWAYS passphrase-encrypted; Settings- or
Activity-only bundles are plain JSON, since they hold no secrets.

Two-step by design: import_bundle() only parses and never writes, so the
caller can preview the result and then call apply_import() explicitly. A bad
passphrase or a stale file therefore cannot silently mutate local state.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import activity as activity_module
from . import secret_store
from .config import CONFIG_LOCK, Profile, load_pool, save_pool

BUNDLE_VERSION = 1
PBKDF2_ITERATIONS = 600_000  # OWASP 2023 recommendation floor for PBKDF2-HMAC-SHA256


class ExportImportError(RuntimeError):
    pass


class WrongPassphraseError(ExportImportError):
    pass


def _derive_key(passphrase: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


@dataclass(frozen=True)
class ExportedProfile:
    name: str
    kind: str
    base_url: Optional[str]
    auth_mode: str
    priority: int
    switch_threshold: float
    enabled: bool
    automatic: bool
    default_model: Optional[str]
    monthly_budget_cap: Optional[float]
    token_threshold: Optional[int]
    tag_color: Optional[str]
    account_uuid: Optional[str]
    credential: str  # the one place a secret is ever put in a serializable structure
    # The account's tier ("max"/"pro"), which only the add-account and
    # import-login flows ever discover. Nothing recomputes it for an oauth
    # Profile afterwards, so leaving it out of the bundle meant a restored
    # account showed no tier in the Dashboard forever.
    plan: Optional[str] = None
    # codex_home is deliberately NOT exported: it is a local path from the
    # one-time `codex login` handshake, meaningless on another machine, and
    # the credential that a re-import actually needs is already carried
    # above.
    codex_model: Optional[str] = None
    codex_reasoning_effort: Optional[str] = None


def build_export_bundle(
    *,
    include_profiles: bool,
    include_settings: bool,
    include_activity: bool,
    passphrase: Optional[str] = None,
) -> bytes:
    if include_profiles and not passphrase:
        raise ExportImportError("A passphrase is required to export Profiles (credentials never leave in the clear).")

    pool = load_pool()
    payload: dict = {"bundle_version": BUNDLE_VERSION, "exported_at": datetime.now(timezone.utc).isoformat()}

    if include_profiles:
        exported = []
        for p in pool.profiles:
            try:
                cred = secret_store.get_token(p.id)
            except Exception as exc:
                raise ExportImportError(f"Could not read credential for {p.name!r} from Keychain: {exc}") from exc
            exported.append(asdict(ExportedProfile(
                name=p.name, kind=p.kind, base_url=p.base_url, auth_mode=p.auth_mode,
                priority=p.priority, switch_threshold=p.switch_threshold, enabled=p.enabled,
                automatic=p.automatic, default_model=p.default_model, monthly_budget_cap=p.monthly_budget_cap,
                token_threshold=p.token_threshold,
                tag_color=p.tag_color, account_uuid=p.account_uuid, credential=cred,
                plan=p.plan, codex_model=p.codex_model, codex_reasoning_effort=p.codex_reasoning_effort,
            )))
        payload["profiles"] = exported

    if include_settings:
        payload["settings"] = asdict(pool.settings)

    if include_activity:
        payload["activity"] = [asdict(e) for e in activity_module.list_events(limit=activity_module.MAX_EVENTS)]

    if "profiles" in payload:
        # Once a passphrase is required at all, the whole payload is sealed
        # together, so settings and activity can't be read out of an
        # unencrypted sibling section.
        plaintext = json.dumps(payload).encode("utf-8")
        salt = os.urandom(16)
        key = _derive_key(passphrase, salt)
        ciphertext = Fernet(key).encrypt(plaintext)
        envelope = {
            "bundle_version": BUNDLE_VERSION,
            "encrypted": True,
            "kdf": "pbkdf2-sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        return json.dumps(envelope, indent=2).encode("utf-8")

    envelope = {"bundle_version": BUNDLE_VERSION, "encrypted": False, "data": payload}
    return json.dumps(envelope, indent=2).encode("utf-8")


@dataclass(frozen=True)
class ParsedBundle:
    profiles: list  # list[ExportedProfile]-shaped dicts, or [] if not included
    settings: Optional[dict]
    activity: Optional[list]


def import_bundle(data: bytes, passphrase: Optional[str] = None) -> ParsedBundle:
    """Read-only: decrypts and parses, never writes. Raises
    WrongPassphraseError specifically, so the caller can show a clear
    message rather than a generic parse failure."""

    try:
        envelope = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ExportImportError(f"Not a valid export bundle: {exc}") from exc

    if envelope.get("bundle_version") != BUNDLE_VERSION:
        raise ExportImportError(f"Unsupported bundle version {envelope.get('bundle_version')!r}.")

    if envelope.get("encrypted"):
        if not passphrase:
            raise ExportImportError("This bundle is encrypted — a passphrase is required.")
        try:
            salt = base64.b64decode(envelope["salt"])
            ciphertext = base64.b64decode(envelope["ciphertext"])
            key = _derive_key(passphrase, salt, int(envelope.get("iterations", PBKDF2_ITERATIONS)))
            plaintext = Fernet(key).decrypt(ciphertext)
        except InvalidToken as exc:
            raise WrongPassphraseError("Wrong passphrase, or the file is corrupted.") from exc
        except (KeyError, ValueError) as exc:
            raise ExportImportError(f"Malformed encrypted bundle: {exc}") from exc
        payload = json.loads(plaintext)
    else:
        payload = envelope.get("data", {})

    return ParsedBundle(
        profiles=payload.get("profiles", []),
        settings=payload.get("settings"),
        activity=payload.get("activity"),
    )


def apply_import(
    parsed: ParsedBundle,
    *,
    import_profiles: bool,
    import_settings: bool,
    conflict_strategy: str = "keep_existing",  # "keep_existing" | "use_imported"
) -> dict:
    """Writes. Call only after `parsed` has been previewed and confirmed.

    Never touches Activity: a machine's history is its own, and importing
    another machine's activity log would be meaningless."""

    if conflict_strategy not in ("keep_existing", "use_imported"):
        raise ExportImportError(f"Unknown conflict_strategy {conflict_strategy!r}.")

    result = {"profiles_added": 0, "profiles_updated": 0, "profiles_skipped": 0, "settings_applied": False}

    if import_profiles:
        with CONFIG_LOCK:
            pool = load_pool()
            by_uuid = {p.account_uuid: p for p in pool.profiles if p.account_uuid}
            for item in parsed.profiles:
                existing = by_uuid.get(item.get("account_uuid")) if item.get("account_uuid") else None
                if existing is not None:
                    if conflict_strategy == "keep_existing":
                        result["profiles_skipped"] += 1
                        continue
                    # "Use imported version" means the whole Profile, not
                    # only its credential: every bundle field below is
                    # applied, not just the stored token.
                    secret_store.set_token(existing.id, item["credential"])
                    updated = replace(
                        existing, name=item["name"], base_url=item.get("base_url"),
                        auth_mode=item.get("auth_mode", "api_key"), priority=item.get("priority", 1),
                        switch_threshold=item.get("switch_threshold", 98.0), enabled=item.get("enabled", True),
                        automatic=item.get("automatic", True), default_model=item.get("default_model"),
                        monthly_budget_cap=item.get("monthly_budget_cap"), token_threshold=item.get("token_threshold"),
                        tag_color=item.get("tag_color"), plan=item.get("plan"),
                        codex_model=item.get("codex_model"), codex_reasoning_effort=item.get("codex_reasoning_effort"),
                    )
                    pool.profiles = [updated if p.id == existing.id else p for p in pool.profiles]
                    result["profiles_updated"] += 1
                    continue

                import secrets as _secrets

                new_profile = Profile(
                    id=_secrets.token_hex(8), name=item["name"], kind=item["kind"], base_url=item.get("base_url"),
                    auth_mode=item.get("auth_mode", "api_key"), priority=item.get("priority", 1),
                    switch_threshold=item.get("switch_threshold", 98.0), enabled=item.get("enabled", True),
                    automatic=item.get("automatic", True), default_model=item.get("default_model"),
                    monthly_budget_cap=item.get("monthly_budget_cap"), token_threshold=item.get("token_threshold"),
                    tag_color=item.get("tag_color"),
                    account_uuid=item.get("account_uuid"), plan=item.get("plan"),
                    codex_model=item.get("codex_model"), codex_reasoning_effort=item.get("codex_reasoning_effort"),
                )
                secret_store.set_token(new_profile.id, item["credential"])
                pool.profiles.append(new_profile)
                result["profiles_added"] += 1
            save_pool(pool)

    if import_settings and parsed.settings:
        from .config import Settings, validated_settings_changes

        # Validated, not trusted. A bundle is a file from somewhere else, and
        # settings now carry decisions with real consequences — model_parity
        # picks which model every codex request runs on, which is what Codex
        # quota is spent on (docs/adr/0007). Importing that unchecked would let
        # a bundle silently change someone's spending.
        incoming = {k: v for k, v in parsed.settings.items() if k in Settings.__dataclass_fields__}
        incoming = validated_settings_changes(incoming)

        with CONFIG_LOCK:
            pool = load_pool()
            # Merged onto the current settings, not built fresh from the
            # bundle. Settings(**incoming) reverted every field the bundle did
            # not mention back to its dataclass default — and a bundle exported
            # by a version that predates a field simply has no key for it. So
            # importing an older bundle silently reset the language and
            # notification preferences of whoever imported it. This is also
            # what update_settings() does, and the two should not disagree
            # about what writing settings means.
            pool.settings = replace(pool.settings, **incoming)
            save_pool(pool)
        result["settings_applied"] = True

    activity_module.record("config", "Imported an export bundle", meta=str(result))
    return result
