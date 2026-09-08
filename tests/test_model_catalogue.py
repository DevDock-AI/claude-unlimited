"""model_catalogue: parsing, validation, offline chain, refresh safety.

Everything here is hermetic: the loader takes an injected source and the
only files touched live under tmp_path or inside the package (the vendored
snapshot). No test may ever construct a real opener or URL.
"""

import datetime
import json
import time

import pytest

import claude_unlimited.config as config
import claude_unlimited.model_catalogue as mc

TODAY = datetime.date(2026, 9, 7)


def entry(provider="anthropic", mode="chat", out_cost=1e-05, in_cost=1e-06,
          reasoning=True, deprecation=None):
    e = {
        "litellm_provider": provider,
        "mode": mode,
        "max_input_tokens": 200000,
        "input_cost_per_token": in_cost,
        "output_cost_per_token": out_cost,
        "supports_reasoning": reasoning,
    }
    if deprecation:
        e["deprecation_date"] = deprecation
    return e


def fixture_raw():
    """A miniature LiteLLM file: real schema, both providers, plus every
    kind of entry the parser must drop."""
    return {
        "claude-fable-5": entry(out_cost=5e-05),
        "claude-opus-5": entry(out_cost=2.5e-05),
        "claude-sonnet-5": entry(out_cost=1e-05),
        # Dated and undated spelling of the same model — must dedupe to one.
        "claude-haiku-4-5": entry(out_cost=5e-06),
        "claude-haiku-4-5-20251001": entry(out_cost=5e-06),
        # Provider-namespaced key: the namespace must be stripped.
        "anthropic.claude-mythos-5": entry(out_cost=5e-05),
        # Retired long ago: dropped by the deprecation filter.
        "claude-2-legacy": entry(out_cost=8e-05, deprecation="2024-01-01"),
        # Not chat: dropped.
        "claude-embed-1": entry(mode="embedding"),
        "gpt-5.6-sol": entry(provider="openai", out_cost=2e-05),
        "gpt-5.6-terra": entry(provider="openai", out_cost=1.2e-05),
        "gpt-5.6-luna": entry(provider="openai", out_cost=1.2e-06),
        "gpt-5.3-codex": entry(provider="openai", mode="responses", out_cost=1.4e-05),
        # Task-specific variants and other providers: dropped.
        "gpt-5.6-audio-preview": entry(provider="openai", out_cost=2e-05),
        "text-embedding-4": entry(provider="openai", mode="embedding"),
        "azure/gpt-5.6-terra": entry(provider="azure", out_cost=1.2e-05),
        "daybreak-red-latest": entry(provider="openai", out_cost=7.5e-05),
        "ft:gpt-4o-2024-08-06": entry(provider="openai", out_cost=1.5e-05),
        # LiteLLM's schema-documentation entry.
        "sample_spec": {"litellm_provider": "one of https://...", "mode": "one of ..."},
    }


def test_parser_produces_both_ordered_lineups():
    cat = mc.parse(fixture_raw(), today=TODAY)
    anth = [m.id for m in cat.anthropic]
    oai = [m.id for m in cat.openai]
    # Most-capable-first, deterministic. Anthropic is cost-ranked (price tracks
    # capability, no fringe outlier). OpenAI is GENERATION-ranked: all the 5.6
    # models sort above gpt-5.3-codex regardless of price, so a fringe-expensive
    # or older model can't outrank the current flagship (see _sort_key).
    assert anth == ["claude-fable-5", "claude-mythos-5", "claude-opus-5",
                    "claude-sonnet-5", "claude-haiku-4-5"]
    assert oai == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.3-codex"]
    assert [m.rank for m in cat.anthropic] == [0, 1, 2, 3, 4]
    sol = cat.openai[0]
    assert sol.supports_reasoning is True
    assert sol.output_cost == 2e-05 and sol.input_cost == 1e-06


def test_namespace_stripping_and_non_chat_filtering():
    cat = mc.parse(fixture_raw(), today=TODAY)
    anth = [m.id for m in cat.anthropic]
    oai = [m.id for m in cat.openai]
    assert "claude-mythos-5" in anth  # anthropic.claude-mythos-5, stripped
    assert not any("." in m.split("-")[0] or "/" in m for m in anth + oai)
    for dropped in ("claude-embed-1", "text-embedding-4", "claude-2-legacy",
                    "gpt-5.6-audio-preview", "daybreak-red-latest", "sample_spec"):
        assert dropped not in anth + oai
    assert not any("ft:" in m for m in oai)


def test_dated_and_undated_spellings_collapse_to_one_model():
    cat = mc.parse(fixture_raw(), today=TODAY)
    haikus = [m.id for m in cat.anthropic if "haiku" in m.id]
    assert haikus == ["claude-haiku-4-5"]  # undated spelling kept


def test_display_names_are_derived_cleanly():
    assert mc.display_name_for("claude-haiku-4-5-20251001") == "Claude Haiku 4.5"
    assert mc.display_name_for("claude-fable-5") == "Claude Fable 5"
    assert mc.display_name_for("gpt-5.6-terra") == "GPT-5.6 Terra"
    assert mc.display_name_for("gpt-5.4-2026-03-05") == "GPT-5.4"
    assert mc.display_name_for("o3-mini") == "O3 Mini"


def test_load_returns_none_on_corrupt_or_schema_changed_input():
    assert mc.load(source=lambda: (_ for _ in ()).throw(ValueError("corrupt")), today=TODAY) is None
    assert mc.load(source=lambda: "not a dict", today=TODAY) is None
    assert mc.load(source=lambda: {}, today=TODAY) is None
    # Schema drift: entries exist but nothing is recognizable any more.
    assert mc.load(source=lambda: {"m1": {"provider": "anthropic"}}, today=TODAY) is None
    # One whole lineup missing -> reject; a half-empty catalogue must not
    # replace a complete one.
    only_claude = {k: v for k, v in fixture_raw().items()
                   if isinstance(v, dict) and v.get("litellm_provider") == "anthropic"}
    assert mc.load(source=lambda: only_claude, today=TODAY) is None


def test_load_succeeds_on_the_fixture():
    cat = mc.load(source=fixture_raw, today=TODAY)
    assert cat is not None and cat.anthropic and cat.openai


def test_vendored_snapshot_parses_and_carries_the_anchor_models():
    raw = json.loads(mc._VENDORED_FILE.read_text(encoding="utf-8"))
    cat = mc.parse(raw, today=TODAY)
    anth = [m.id for m in cat.anthropic]
    oai = [m.id for m in cat.openai]
    assert "claude-opus-5" in anth and "claude-fable-5" in anth
    for anchor in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        assert anchor in oai


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """Redirect the app dir and reset module state so no test can read or
    write the real user's cache."""
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "appdir")
    mc._reset_for_tests()
    yield tmp_path
    mc._reset_for_tests()


def test_initialize_loads_the_vendored_catalogue_without_network(isolated_state):
    cat = mc.initialize()
    assert cat is not None
    assert mc.current() is cat
    assert any(m.id.startswith("claude-") for m in cat.anthropic)


def test_initialize_prefers_a_valid_disk_cache_over_vendored(isolated_state):
    (config.APP_DIR).mkdir(parents=True)
    cached = mc.trim_raw(fixture_raw(), today=TODAY)
    (config.APP_DIR / "model_catalogue_cache.json").write_text(
        json.dumps({"meta": {"fetched_at": 1}, "entries": cached}))
    cat = mc.initialize()
    assert [m.id for m in cat.anthropic][0] == "claude-fable-5"
    assert len(cat.anthropic) == 5  # the fixture's lineup, not the vendored one


def test_a_corrupt_cache_falls_back_to_vendored(isolated_state):
    (config.APP_DIR).mkdir(parents=True)
    (config.APP_DIR / "model_catalogue_cache.json").write_text("{not json")
    cat = mc.initialize()
    assert cat is not None  # vendored still loaded
    assert len(cat.anthropic) > 5


def test_failed_refresh_keeps_the_previous_catalogue_and_backs_off(isolated_state):
    before = mc.initialize()
    calls = []

    def failing_source():
        calls.append(1)
        raise OSError("network down")

    assert mc.refresh(source=failing_source) is False
    assert mc.current() is before  # untouched
    meta = json.loads((config.APP_DIR / "model_catalogue_cache.json").read_text())["meta"]
    assert meta["backoff_until"] > meta["last_attempt"]
    # The recorded backoff must actually suppress the next due-check.
    assert mc.refresh_if_due(source=failing_source) is False
    assert len(calls) == 1


def test_rate_limited_refresh_backs_off_for_a_day(isolated_state):
    mc.initialize()

    def limited_source():
        raise mc._RateLimited("HTTP 429")

    assert mc.refresh(source=limited_source) is False
    meta = json.loads((config.APP_DIR / "model_catalogue_cache.json").read_text())["meta"]
    assert meta["backoff_until"] - meta["last_attempt"] == pytest.approx(
        mc.RATE_LIMIT_BACKOFF_SECONDS, abs=5)


def test_successful_refresh_replaces_current_and_rewrites_the_cache(isolated_state):
    mc.initialize()
    assert mc.refresh(source=fixture_raw) is True
    assert [m.id for m in mc.current().anthropic][0] == "claude-fable-5"
    stored = json.loads((config.APP_DIR / "model_catalogue_cache.json").read_text())
    assert "claude-fable-5" in stored["entries"]
    assert stored["meta"]["fetched_at"] > 0
    # A fresh fetch means the periodic check has nothing to do.
    assert mc.refresh_if_due(source=fixture_raw) is False


def test_a_corrupt_download_never_replaces_a_good_catalogue(isolated_state):
    mc.initialize()
    assert mc.refresh(source=fixture_raw) is True
    good = mc.current()
    assert mc.refresh(source=lambda: {"schema": "changed"}) is False
    assert mc.current() is good
    stored = json.loads((config.APP_DIR / "model_catalogue_cache.json").read_text())
    assert "claude-fable-5" in stored["entries"]  # last-good entries kept


# ---- sha-gated refresh + session-launch throttle ----


class FakeSource:
    """Injected stand-in for GitHubSource: counts the cheap sha probe and
    the expensive blob download separately."""

    def __init__(self, sha, raw):
        self._sha = sha
        self._raw = raw
        self.sha_calls = 0
        self.fetch_calls = 0

    def sha(self):
        self.sha_calls += 1
        return self._sha

    def fetch(self, sha):
        self.fetch_calls += 1
        return self._raw


SHA_A = "a" * 40
SHA_B = "b" * 40


def test_unchanged_sha_skips_the_blob_download(isolated_state):
    mc.initialize()
    src = FakeSource(SHA_A, fixture_raw())
    assert mc.refresh(source=src) is True   # no stored sha yet: downloads
    assert (src.sha_calls, src.fetch_calls) == (1, 1)
    before = mc.current()

    assert mc.refresh(source=src) is True   # same sha: cheap check only
    assert src.sha_calls == 2
    assert src.fetch_calls == 1             # the 2.3 MB blob was NOT fetched
    assert mc.current() is before           # catalogue untouched
    meta = json.loads((config.APP_DIR / "model_catalogue_cache.json").read_text())["meta"]
    assert meta["sha"] == SHA_A
    assert meta["checked_at"] >= meta["fetched_at"]  # the check was recorded


def test_changed_sha_downloads_and_reloads(isolated_state):
    mc.initialize()
    assert mc.refresh(source=FakeSource(SHA_A, fixture_raw())) is True
    before = mc.current()

    changed = fixture_raw()
    changed["claude-nova-6"] = entry(out_cost=9e-05)
    src = FakeSource(SHA_B, changed)
    assert mc.refresh(source=src) is True
    assert (src.sha_calls, src.fetch_calls) == (1, 1)
    assert mc.current() is not before
    assert "claude-nova-6" in [m.id for m in mc.current().anthropic]
    meta = json.loads((config.APP_DIR / "model_catalogue_cache.json").read_text())["meta"]
    assert meta["sha"] == SHA_B


def test_session_trigger_coalesces_rapid_launches(isolated_state):
    """Any number of `code` launches inside the window produce at most one
    sha check (unauthenticated GitHub allows 60 requests/hour)."""
    mc.initialize()
    src = FakeSource(SHA_A, fixture_raw())
    assert mc.refresh_if_allowed(source=src) is True
    for _ in range(5):
        assert mc.refresh_if_allowed(source=src) is False
    assert src.sha_calls == 1

    # Once the window has passed, a launch may check again — and an
    # unchanged sha still skips the download.
    then = time.time() - mc.SHA_CHECK_MIN_INTERVAL_SECONDS - 1
    cache_file = config.APP_DIR / "model_catalogue_cache.json"
    stored = json.loads(cache_file.read_text())
    stored["meta"]["last_attempt"] = then
    cache_file.write_text(json.dumps(stored))
    mc._mem_last_attempt = then
    assert mc.refresh_if_allowed(source=src) is True
    assert (src.sha_calls, src.fetch_calls) == (2, 1)


def test_session_trigger_honors_a_recorded_backoff(isolated_state):
    mc.initialize()

    def failing():
        raise OSError("network down")

    assert mc.refresh(source=failing) is False  # records a backoff
    src = FakeSource(SHA_A, fixture_raw())
    assert mc.refresh_if_allowed(source=src) is False
    assert src.sha_calls == 0  # backoff suppressed even the cheap probe


def test_hourly_baseline_uses_the_last_check_not_the_last_download(isolated_state):
    mc.initialize()
    src = FakeSource(SHA_A, fixture_raw())
    assert mc.refresh(source=src) is True
    # A fresh sha CHECK (even without a download) resets the hourly clock.
    then = time.time() - mc.SHA_CHECK_MIN_INTERVAL_SECONDS - 1
    mc._mem_last_attempt = then
    assert mc.refresh(source=src) is True  # sha unchanged: check only
    assert mc.refresh_if_due(source=src) is False  # checked seconds ago
    assert src.fetch_calls == 1


def test_the_baseline_cadence_is_hourly():
    # Ticket: models appear mid-day; the sha check itself is ~1 KB, so an
    # hourly baseline is safe (well under GitHub's 60/hr unauthenticated
    # cap even together with the 5-minute session-launch floor).
    assert mc.REFRESH_INTERVAL_SECONDS == 60 * 60
    assert mc.SHA_CHECK_MIN_INTERVAL_SECONDS == 5 * 60


def test_openai_lineup_excludes_non_codex_and_ranks_by_generation():
    # Regression: the OpenAI lineup must be Codex-relevant and generation-ranked.
    # o-series + legacy (<gen 5) are dropped, and a newer-but-cheaper flagship
    # (gpt-6-astra) must outrank an older pricier model (gpt-5.6-sol) so a
    # fringe $600/M reasoning model can never become the mapping's top tier.
    raw = {
        "gpt-6-astra": entry(provider="openai", out_cost=1e-05),   # newest, cheaper
        "gpt-5.6-sol": entry(provider="openai", out_cost=5e-05),   # older, pricier
        "o1-pro": entry(provider="openai", out_cost=6e-04),        # fringe reasoning
        "gpt-4o": entry(provider="openai", out_cost=1e-05),        # legacy gen < 5
        "gpt-3.5-turbo": entry(provider="openai", out_cost=1e-06), # legacy
        "gpt-5.3-codex": entry(provider="openai", out_cost=9e-06), # codex kept
        "claude-opus-5": entry(provider="anthropic", out_cost=7e-05),
    }
    oai = [m.id for m in mc.parse(raw, today=TODAY).openai]
    assert "o1-pro" not in oai
    assert "gpt-4o" not in oai
    assert "gpt-3.5-turbo" not in oai
    assert oai == ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.3-codex"]
