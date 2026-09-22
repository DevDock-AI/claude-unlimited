"""ECO's five invariants, as property tests.

These are the gate: a filter that compresses brilliantly but violates one of
these is not shippable. Determinism in particular is load-bearing — Anthropic
caches a prefix and Claude Code re-sends history every turn, so unstable output
means a cache miss per turn, which costs far more than ECO saves.
"""

import subprocess
import sys

import pytest

from claude_unlimited import eco


# A spread of shapes ECO actually meets: repetitive build spam, log lines with
# blank runs, something already compacted, and text too short to touch.
SAMPLES = [
    "\n".join(["Compiling frobnicator v0.1.0"] * 40),
    "\n".join([f"line {i % 3}" for i in range(400)]),
    "warning: unused variable\n" * 60 + "\n\n\n" + "done\n",
    "\n\n\n".join(["paragraph one", "paragraph two", "paragraph three"] * 40),
    "x" * 600,
    "short output",
    "",
    "\n".join([f"{i}: unique content here that never repeats at all" for i in range(300)]),
]

TIERS = ["light", "aggressive"]


def _big(text: str) -> str:
    """Pad past MIN_COMPACT_SIZE so the filter actually engages."""
    if len(text) >= eco.MIN_COMPACT_SIZE:
        return text
    return text + "\n" + "\n".join(["filler line that repeats"] * 40)


# ---- invariant 1: deterministic ----

@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("tier", TIERS)
def test_compaction_is_deterministic_in_process(sample, tier):
    first, name1 = eco.compact_text(_big(sample), tier)
    second, name2 = eco.compact_text(_big(sample), tier)
    assert first == second
    assert name1 == name2


def test_compaction_is_deterministic_across_processes():
    # A fresh interpreter with a different PYTHONHASHSEED catches the classic
    # leak: set iteration or hash() order reaching the output. Same-process
    # repetition cannot catch that.
    script = (
        "import json;from claude_unlimited import eco;"
        "t='\\n'.join(['dup line']*50)+'\\n'+'\\n'.join('u%d'%i for i in range(200));"
        "print(json.dumps(eco.compact_text(t,'light')))"
    )
    runs = []
    for seed in ("0", "1", "random"):
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"})
        assert result.returncode == 0, result.stderr
        runs.append(result.stdout)
    assert len(set(runs)) == 1, "output changed with PYTHONHASHSEED"


def test_stats_hits_do_not_depend_on_dict_order():
    body = _body("\n".join(["repeated"] * 80))
    _, stats1 = eco.compact_request(body, "light")
    _, stats2 = eco.compact_request(body, "light")
    assert stats1.hits == stats2.hits
    assert list(stats1.hits) == sorted(stats1.hits)


# ---- invariant 2: idempotent ----

@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("tier", TIERS)
def test_compaction_is_idempotent(sample, tier):
    once, _ = eco.compact_text(_big(sample), tier)
    twice, _ = eco.compact_text(once, tier)
    assert twice == once, "second pass changed already-compacted text"


def test_markers_do_not_stack_on_repeated_passes():
    text = "\n".join(["same"] * 200)
    out = text
    for _ in range(5):
        out, _ = eco.compact_text(out, "light")
    assert out.count(eco.MARKER) <= 1


# ---- invariant 3: never grows, never empties ----

@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("tier", TIERS)
def test_compaction_never_grows_and_never_empties(sample, tier):
    padded = _big(sample)
    out, _ = eco.compact_text(padded, tier)
    assert len(out) <= len(padded)
    if padded:
        assert out, "compaction emptied a non-empty tool result"


def test_never_worse_keeps_the_original_when_a_filter_would_expand_it():
    assert eco.never_worse("abc", "abcdef") == "abc"
    assert eco.never_worse("abc", "") == "abc"
    assert eco.never_worse("abcdef", "abc") == "abc"


def test_text_below_the_threshold_is_left_alone():
    small = "line\n" * 5
    assert len(small) < eco.MIN_COMPACT_SIZE
    out, name = eco.compact_text(small, "light")
    assert out == small and name is None


def test_text_above_the_raw_cap_is_left_alone():
    huge = "dup\n" * (eco.RAW_CAP // 4 + 10)
    assert len(huge) > eco.RAW_CAP
    out, name = eco.compact_text(huge, "light")
    assert out == huge and name is None


# ---- invariant 4: never touches errors ----

def _body(tool_text, is_error=False):
    block = {"type": "tool_result", "tool_use_id": "t1", "content": tool_text}
    if is_error:
        block["is_error"] = True
    return {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": [block]}]}


def test_error_blocks_are_never_compacted():
    text = "\n".join(["Traceback repeated line"] * 200)
    body = _body(text, is_error=True)
    out, stats = eco.compact_request(body, "light")
    assert out["messages"][0]["content"][0]["content"] == text
    assert not stats.touched


def test_non_tool_result_content_is_never_touched():
    # System prompts, user text and tool INPUTS are off limits; only what a
    # tool printed may be rewritten.
    text = "\n".join(["user typed this"] * 200)
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
            "system": text}
    out, stats = eco.compact_request(body, "light")
    assert out == body
    assert not stats.touched


# ---- invariant 5: fails open ----

def test_a_raising_filter_returns_the_input(monkeypatch):
    def boom(_):
        raise RuntimeError("filter bug")
    monkeypatch.setitem(eco.FILTERS, "dedup-log", boom)
    text = "\n".join(["dup"] * 200)
    assert eco.apply_filter("dedup-log", text) == text


def test_an_unknown_filter_name_returns_the_input():
    assert eco.apply_filter("no-such-filter", "hello") == "hello"


# ---- body handling ----

def test_compact_request_returns_a_new_body_and_leaves_the_original_intact():
    # Failover re-sends the ORIGINAL body to another account; mutating in place
    # would send already-compacted text and stack markers.
    text = "\n".join(["dup line"] * 200)
    body = _body(text)
    out, stats = eco.compact_request(body, "light")
    assert out is not body
    assert body["messages"][0]["content"][0]["content"] == text, "input was mutated"
    assert out["messages"][0]["content"][0]["content"] != text
    assert stats.bytes_saved > 0


def test_tier_off_is_a_no_op():
    body = _body("\n".join(["dup"] * 200))
    out, stats = eco.compact_request(body, "off")
    assert out is body
    assert not stats.touched
    assert stats.bytes_saved == 0


def test_list_shaped_tool_result_content_is_compacted():
    text = "\n".join(["dup line"] * 200)
    block = {"type": "tool_result", "content": [{"type": "text", "text": text}]}
    body = {"messages": [{"role": "user", "content": [block]}]}
    out, stats = eco.compact_request(body, "light")
    assert out["messages"][0]["content"][0]["content"][0]["text"] != text
    assert stats.touched


def test_malformed_bodies_do_not_raise():
    for body in [{}, {"messages": "nope"}, {"messages": [None, 5]},
                 {"messages": [{"role": "user", "content": "plain string"}]}]:
        out, stats = eco.compact_request(body, "light")
        assert out is not None
        assert stats.bytes_saved == 0


def test_stats_report_exact_byte_savings():
    text = "\n".join(["dup line"] * 200)
    body = _body(text)
    out, stats = eco.compact_request(body, "light")
    served = out["messages"][0]["content"][0]["content"]
    assert stats.bytes_before == len(text)
    assert stats.bytes_after == len(served)
    assert stats.bytes_saved == len(text) - len(served)


# ---- the dedup-log filter itself ----

def test_dedup_log_collapses_runs_and_names_the_count():
    out = eco.dedup_log("\n".join(["same"] * 10))
    assert out.count("same") == 1
    assert "9 duplicate lines" in out
    assert eco.MARKER in out


def test_dedup_log_keeps_distinct_lines():
    text = "\n".join(f"line {i}" for i in range(50))
    assert eco.dedup_log(text) == text


def test_dedup_log_caps_runaway_output():
    text = "\n".join(f"unique {i}" for i in range(eco.MAX_LINES + 500))
    out = eco.dedup_log(text)
    assert len(out.split("\n")) <= eco.MAX_LINES + 1
    assert "truncated" in out


def test_detect_needs_enough_lines_to_be_worth_it():
    assert eco.detect("one\ntwo\nthree") is None
    assert eco.detect("\n".join(["a", "b", "c", "d", "e", "f"])) == "dedup-log"
    assert eco.detect("") is None


# ---- gateway wiring ----

def test_gateway_helper_is_a_no_op_when_eco_is_off():
    from claude_unlimited import gateway
    raw = __import__("json").dumps(_body("\n".join(["dup"] * 200))).encode()
    out, stats = gateway._eco_compact(raw, "off")
    assert out is raw and not stats.touched


def test_gateway_helper_compacts_when_enabled():
    from claude_unlimited import gateway
    raw = __import__("json").dumps(_body("\n".join(["dup line"] * 300))).encode()
    out, stats = gateway._eco_compact(raw, "light")
    assert len(out) < len(raw)
    assert stats.bytes_saved > 0


def test_gateway_helper_fails_open_on_unparseable_bodies():
    from claude_unlimited import gateway
    for raw in [b"", b"not json", b"{", b'{"messages": 5}']:
        out, stats = gateway._eco_compact(raw, "light")
        assert out == raw
        assert stats.bytes_saved == 0


def test_gateway_helper_fails_open_when_a_filter_raises(monkeypatch):
    from claude_unlimited import gateway
    def boom(*a, **k):
        raise RuntimeError("bug in eco")
    monkeypatch.setattr(gateway.eco, "compact_request", boom)
    raw = __import__("json").dumps(_body("\n".join(["dup"] * 200))).encode()
    out, stats = gateway._eco_compact(raw, "light")
    assert out is raw and not stats.touched


def test_eco_ships_off_by_default():
    from claude_unlimited.config import Settings
    # It changes what the model sees, so it must be chosen, never inherited.
    assert Settings().eco_tier == "off"


# ---- the settings field ----

def test_eco_tier_is_accepted_and_value_checked():
    from claude_unlimited.config import ECO_TIERS, validated_settings_changes
    for tier in ECO_TIERS:
        assert validated_settings_changes({"eco_tier": tier}) == {"eco_tier": tier}
    with pytest.raises(ValueError):
        # An unrecognised tier would fall through TIERS.get() to a silent
        # no-op, leaving the UI claiming ECO is on while nothing happens.
        validated_settings_changes({"eco_tier": "turbo"})


def test_every_settings_field_is_changeable():
    # Regression guard for a whole bug CLASS: _SETTINGS_FIELDS is a
    # hand-maintained literal, so adding a field to Settings without adding it
    # here makes both PATCH /api/settings AND bundle import reject it with a
    # 400 — which is exactly how adding eco_tier broke the export/import
    # round-trip. Keep the two in sync.
    from dataclasses import fields
    from claude_unlimited.config import Settings, _SETTINGS_FIELDS
    assert {f.name for f in fields(Settings)} == set(_SETTINGS_FIELDS)


def test_every_eco_tier_is_a_known_tier_table():
    from claude_unlimited.config import ECO_TIERS
    # config and eco must agree on the tier names, or a settable tier could
    # silently do nothing.
    assert set(ECO_TIERS) == set(eco.TIERS)


# ---- the seven light filters ----
#
# Real shapes, because detection sniffs CONTENT: a filter that never fires on
# the text it was written for is dead code, and one that fires on the wrong
# text can violate an invariant nothing else catches.

TREE = "\n".join(["src/"] + [f"├── file{i}.py" for i in range(60)] + ["└── last.py", "", "3 directories, 61 files"])
GIT_STATUS = "On branch main\n" + "\n".join([f" M src/file{i}.py" for i in range(30)] +
                                            [f"?? new{i}.txt" for i in range(15)] +
                                            [f"A  staged{i}.py" for i in range(12)])
GIT_LOG = "\n".join([f"commit {'a'*40}\nAuthor: A <a@b.c>\nDate: today\n\n    subject {i}\n"
                     "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new" for i in range(20)])
FIND = "\n".join([f"src/pkg{i % 4}/module{i}.py" for i in range(80)])
GREP = "\n".join([f"src/file{i % 5}.py:{i}:    matched content here" for i in range(90)])
LS = "total 128\n" + "\n".join([f"-rw-r--r--  1 u  g   1024 Jan  1 00:00 file{i}.py" for i in range(40)] +
                               ["drwxr-xr-x  1 u  g   4096 Jan  1 00:00 node_modules",
                                "drwxr-xr-x  1 u  g   4096 Jan  1 00:00 .git"])
BUILD = "\n".join(["npm run build"] + [f"Compiling pkg{i}" for i in range(30)] +
                  [f"warning: unused var {i}" for i in range(20)] +
                  [f"npm WARN deprecated pkg{i}@1.0.0" for i in range(10)] +
                  [f"error: cannot find name 'x{i}'" for i in range(4)] +
                  ["Build failed with 4 errors"])

# Which tier each shape belongs to after the split: LIGHT only collapses
# provably redundant text, AGGRESSIVE may discard detail.
SHAPES = {"git-status": GIT_STATUS, "grep": GREP, "build-output": BUILD}
AGGRESSIVE_SHAPES = {"tree": TREE, "git-log": GIT_LOG, "find": FIND, "ls": LS}
ALL_SHAPES = {**SHAPES, **AGGRESSIVE_SHAPES}


@pytest.mark.parametrize("expected,text", sorted(ALL_SHAPES.items()))
def test_each_shape_is_detected_as_its_own_filter(expected, text):
    assert eco.detect(text) == expected


@pytest.mark.parametrize("name,text", sorted(AGGRESSIVE_SHAPES.items()))
def test_lossy_filters_are_aggressive_only(name, text):
    # Each of these discards detail the model cannot reconstruct, so LIGHT —
    # which promises "collapses provably redundant output and always leaves a
    # count" — must leave them alone.
    assert name not in eco.TIERS["light"]
    assert eco.compact_text(text, "light")[1] != name
    assert eco.compact_text(text, "aggressive")[1] == name


@pytest.mark.parametrize("name,text", sorted(ALL_SHAPES.items()))
def test_every_filter_upholds_the_invariants(name, text):
    tier = "aggressive"          # the tier that enables every filter
    once, used = eco.compact_text(text, tier)
    assert used == name
    # 3: never grows, never empties
    assert 0 < len(once) <= len(text)
    # 1: deterministic
    assert eco.compact_text(text, tier)[0] == once
    # 2: idempotent
    assert eco.compact_text(once, tier)[0] == once


def test_build_output_keeps_every_error_line():
    out = eco.build_output(BUILD)
    # Errors are the whole reason a build log is in the context.
    for i in range(4):
        assert f"error: cannot find name 'x{i}'" in out
    assert "Compiled 30 packages" in out
    # Sampled warnings/deprecations are now counted in ONE omission marker
    # rather than several, because the filter no longer reorders the output
    # into error/deprecation/warning groups — it keeps original order and
    # tallies everything it left out.
    assert "omitted" in out


def test_git_status_reports_counts_per_category():
    out = eco.git_status(GIT_STATUS)
    assert "* main" in out
    assert "~ Modified: 30" in out
    assert "? Untracked: 15" in out
    assert "+ Staged: 12" in out
    assert "… +20 more" in out          # 30 modified, 10 shown


def test_git_log_drops_diff_bodies_but_keeps_subjects():
    out = eco.git_log(GIT_LOG)
    assert "subject 0" in out
    assert "diff body omitted" in out
    assert "+new" not in out and "-old" not in out


def test_tree_drops_the_count_footer():
    out = eco.tree(TREE)
    assert "3 directories, 61 files" not in out


def test_grep_groups_by_file_and_bounds_each():
    out = eco.grep_matches(GREP)
    assert "src/file0.py" in out
    assert "more" in out               # 18 per file, 10 shown


def test_find_groups_by_directory():
    out = eco.find_paths(FIND)
    assert "src/pkg0/" in out
    assert "entries" in out


def test_ls_marks_hidden_build_dirs_rather_than_silently_dropping_them():
    out = eco.ls_listing(LS)
    # Deliberate deviation from the reference: a model that cannot see .git
    # may conclude the path is not a repository.
    assert "hidden (build dirs)" in out
    assert "node_modules" not in out
    assert "-rw-r--r--" not in out     # permission columns dropped
    assert "py 40" in out              # extension histogram


def test_git_status_without_porcelain_lines_changes_nothing():
    # This used to assert only len(out) <= len(text), which PASSES when the
    # filter throws the whole status away and returns "* main". Assert the
    # actual contract: unchanged.
    text = "On branch main\nYour branch is up to date with 'origin/main'.\n\nnothing to commit\n" * 20
    out, name = eco.compact_text(text, "light")
    assert out == text and name is None


# ---- aggressive tier ----

DIFF = "\n".join(
    f"diff --git a/src/file{i}.py b/src/file{i}.py\n"
    "index abc..def 100644\n--- a/src/file{i}.py\n+++ b/src/file{i}.py\n"
    "@@ -1,200 +1,200 @@\n" + "\n".join(
        [f"-removed line {j}\n+added line {j}\n context {j}" for j in range(60)])
    for i in range(3))

LONG_OUTPUT = "\n".join(f"unique diagnostic line {i} with some content" for i in range(600))


def test_aggressive_only_filters_are_not_in_light():
    assert "git-diff" not in eco.TIERS["light"]
    assert "smart-truncate" not in eco.TIERS["light"]
    assert "git-diff" in eco.TIERS["aggressive"]
    assert "smart-truncate" in eco.TIERS["aggressive"]


def test_light_leaves_a_long_unique_dump_alone():
    # Nothing in light can shrink 600 distinct lines; it must not pretend to.
    out, used = eco.compact_text(LONG_OUTPUT, "light")
    assert out == LONG_OUTPUT and used is None


def test_smart_truncate_keeps_the_head_and_the_tail():
    out, used = eco.compact_text(LONG_OUTPUT, "aggressive")
    assert used == "smart-truncate"
    assert len(out) < len(LONG_OUTPUT)
    assert "unique diagnostic line 0 " in out        # head kept
    assert "unique diagnostic line 599 " in out      # tail kept
    assert "truncated" in out
    assert eco.compact_text(out, "aggressive")[0] == out


def test_a_matching_filter_that_cannot_help_does_not_block_a_later_one():
    # dedup-log matches almost any multi-line text but shrinks nothing here.
    # Detection must fall through to smart-truncate rather than give up.
    assert "dedup-log" in eco.detect_all(LONG_OUTPUT)
    assert "smart-truncate" in eco.detect_all(LONG_OUTPUT)
    assert eco.compact_text(LONG_OUTPUT, "aggressive")[1] == "smart-truncate"


def test_git_diff_names_every_file_with_its_totals():
    out, used = eco.compact_text(DIFF, "aggressive")
    assert used == "git-diff"
    assert len(out) < len(DIFF)
    for i in range(3):
        assert f"a/src/file{i}.py" in out            # no file disappears silently
    assert "of hunk omitted" in out
    assert eco.compact_text(out, "aggressive")[0] == out


def test_git_diff_is_untouched_by_the_light_tier():
    out, used = eco.compact_text(DIFF, "light")
    assert used != "git-diff"


# ---- measurement ----

def test_bytes_saved_is_exact_and_tokens_are_calibrated():
    from claude_unlimited.usage_history import calibrated_tokens_saved
    # 40,000 bytes billed as 10,000 tokens = 4 bytes/token; 4,000 saved bytes
    # is therefore ~1,000 tokens. Derived from what was actually billed, not a
    # fixed constant.
    assert calibrated_tokens_saved(4000, 40000, 10000) == 1000


def test_tokens_saved_is_absent_rather_than_invented():
    from claude_unlimited.usage_history import calibrated_tokens_saved
    # Nothing saved, nothing billed, or a ratio outside the believable band ->
    # store bytes only. A number that reads as measured must BE measured.
    assert calibrated_tokens_saved(0, 40000, 10000) is None
    assert calibrated_tokens_saved(4000, 40000, 0) is None
    assert calibrated_tokens_saved(4000, 40000, 100) is None      # 400 B/token
    assert calibrated_tokens_saved(4000, 100, 10000) is None      # 0.01 B/token


def test_a_row_records_what_eco_saved(tmp_path, monkeypatch):
    from claude_unlimited import config, db, usage_history
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "_degraded", {})
    event = usage_history.record("p1", None, "claude-sonnet-5",
                                 {"input_tokens": 100, "output_tokens": 10},
                                 eco_bytes_saved=2048, eco_tokens_saved=512, eco_mode="light")
    assert event.eco_bytes_saved == 2048
    assert event.eco_tokens_saved == 512
    assert event.eco_mode == "light"
    stored = usage_history.list_events()[-1]
    assert stored.profile_id == "p1"
    # Read back, not just written: the columns existed and were populated, but
    # list_events() rebuilt UsageEvent without them, so every reader saw None.
    assert stored.eco_bytes_saved == 2048
    assert stored.eco_tokens_saved == 512
    assert stored.eco_mode == "light"


def test_a_row_with_eco_off_stores_nulls(tmp_path, monkeypatch):
    from claude_unlimited import config, db, usage_history
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "_degraded", {})
    event = usage_history.record("p1", None, "claude-sonnet-5",
                                 {"input_tokens": 100, "output_tokens": 10})
    # Not 0 — absent. Zero would read as "ECO ran and saved nothing".
    assert event.eco_bytes_saved is None
    assert event.eco_mode is None



# ---- what ECO must NEVER touch -------------------------------------------
#
# These are ordinary things a tool prints. Every one of them was silently
# destroyed or fabricated by an earlier version of this module, and all of it
# passed a green suite, because the tests only fed shapes the filters were
# written for. Assert the contract: unchanged.

GIT_STATUS_LONG = (
    "On branch main\n"
    "Your branch is up to date with 'origin/main'.\n\n"
    "Changes not staged for commit:\n"
    '  (use "git add <file>..." to update what will be committed)\n'
    + "".join(f"\tmodified:   src/module{i}.py\n" for i in range(25))
    + '\nno changes added to commit (use "git add" and/or "git commit -a")\n'
)

README_WITH_PIP = (
    "# My Project\n\n## Install\n\n    pip install my-project\n\n## Usage\n\n"
    + "\n".join(f"Paragraph {i}: this line explains something important." for i in range(70))
)

TIMESTAMPED_LOG = "\n".join(
    f"12:30:{i % 60:02d} INFO  worker{i % 4} handled request {i}" for i in range(120))

DATED_LOG = "\n".join(
    f"2026-09-17 09:{i % 60:02d}:00 ERROR something went wrong in step {i}" for i in range(90))

PROSE_TOTAL = "total 5 tests failed.\n" + "\n".join(
    f"  FAILED tests/test_thing.py::test_case_{i}" for i in range(40))

JSON_DATA = "[\n" + ",\n".join("  0" for _ in range(63)) + "\n]"

UNTOUCHABLE = {
    "git status (long form)": GIT_STATUS_LONG,
    "README mentioning pip": README_WITH_PIP,
    "log with clock prefixes": TIMESTAMPED_LOG,
    "log with date prefixes": DATED_LOG,
    "prose starting 'total '": PROSE_TOTAL,
}


@pytest.mark.parametrize("what,text", sorted(UNTOUCHABLE.items()))
def test_light_leaves_ordinary_tool_output_alone(what, text):
    out, used = eco.compact_text(text, "light")
    assert out == text, f"LIGHT rewrote {what}"
    assert used is None


def test_a_summarising_filter_never_invents_a_clean_tree():
    # The worst failure mode found: `git status` became "* main", telling the
    # model the working tree was clean when 25 files were modified.
    out, _ = eco.compact_text(GIT_STATUS_LONG, "light")
    assert "modified:   src/module0.py" in out


def test_build_output_preserves_order_and_counts_what_it_drops():
    log = "\n".join(
        ["npm run build"]
        + [f"Compiling pkg{i}" for i in range(20)]
        + [f"warning: unused variable {i}" for i in range(20)]
        + ["final summary line"])
    out, used = eco.compact_text(log, "light")
    assert used == "build-output"
    # Order preserved: the command still precedes the summary.
    assert out.index("npm run build") < out.index("final summary line")
    # Nothing vanishes unannounced.
    assert "omitted" in out


# ---- savings reach the usage row (end to end) ----

def test_the_tier_that_produced_a_saving_is_recorded():
    from claude_unlimited import gateway
    import json as _json
    body = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "content": "\n".join(["dup line"] * 300)}]}]}
    raw = _json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    out, stats = gateway._eco_compact(raw, "light")
    assert stats.bytes_saved > 0
    # A saving without its tier cannot be compared across a settings change.
    assert gateway._eco_mode_of(stats) == "light"
    # And with ECO off nothing is claimed at all.
    _, off = gateway._eco_compact(raw, "off")
    assert off.bytes_saved == 0 and gateway._eco_mode_of(off) is None


def test_eco_reaches_every_profile_kind():
    # oauth, api and codex all send the same tool output. A setting that
    # reached only some of them would report savings that depend on which
    # account happened to serve the request.
    import inspect
    from claude_unlimited import gateway
    src = inspect.getsource(gateway.Gateway.handle)
    assert src.index("_eco_compact(body") < src.index('if profile.kind == "codex"')
    assert "headers, eco_body, now" in src
    assert "eco_stats" in inspect.signature(gateway.Gateway._handle_codex).parameters


def test_usage_capture_carries_the_savings_to_record():
    import inspect
    from claude_unlimited import gateway
    params = inspect.signature(gateway.Gateway._wrap_with_usage_capture).parameters
    assert "eco_stats" in params and "sent_bytes" in params
    src = inspect.getsource(gateway.Gateway._wrap_with_usage_capture)
    # The saving must land on the same row as the token counts.
    assert "eco_bytes_saved" in src and "calibrated_tokens_saved" in src


def test_settings_explainer_numbers_match_the_filters():
    # Settings → ECO, the Help page and the README all quote concrete numbers
    # and examples. If a filter's bounds change, the explanation must too —
    # otherwise the one screen meant to stop ECO looking like magic lies.
    import json
    from pathlib import Path
    en = json.loads((Path(eco.__file__).parent / "locales" / "en.json").read_text(encoding="utf-8"))

    assert eco.MIN_COMPACT_SIZE == 500 and "500 characters" in en["settings.eco.scope"]

    # "40 identical lines become one line plus … (39 duplicate lines, ECO)"
    log = "\n".join(["starting"] + ["retrying…"] * 40 + ["done"] * 1 + ["x" * 20] * 1)
    out = eco.dedup_log(log)
    assert "retrying…\n… (39 duplicate lines, ECO)" in out
    assert "39 duplicate lines, ECO" in en["settings.eco.f.dedup_desc"]
    assert eco.MAX_LINES == 2000 and "2,000 lines" in en["settings.eco.f.dedup_desc"]

    # Build logs: every error kept, 5 warnings, 3 deprecations, Compiling collapsed.
    build = "\n".join(["npm run build"] + [f"warning: w{i}" for i in range(9)]
                      + [f"deprecated api {i}" for i in range(6)]
                      + [f"error: e{i}" for i in range(4)]
                      + [f"   Compiling crate{i}" for i in range(84)])
    out = eco.build_output(build)
    assert all(f"error: e{i}" in out for i in range(4))
    assert sum("warning: w" in ln for ln in out.split("\n")) == 5
    assert sum("deprecated api" in ln for ln in out.split("\n")) == 3
    assert "Compiled 84 packages (ECO)" in out
    assert "first 5 warnings and 3 deprecation" in en["settings.eco.f.build_desc"]

    # git status and grep: first 10 per group/file.
    status = "\n".join(f" M src/f{i}.py" for i in range(25))
    assert "… +15 more" in eco.git_status(status)
    grep = "\n".join(f"app.js:{i}:hit" for i in range(37))
    assert "app.js  (37 matches)" in eco.grep_matches(grep) and "… +27 more" in eco.grep_matches(grep)
    assert "first 10" in en["settings.eco.f.grep_desc"] and "first 10" in en["settings.eco.f.git_status_desc"]

    # Very long output: 250+ lines, first 120 and last 60 kept.
    long = "\n".join(f"line {i}" for i in range(5000))
    lines = eco.smart_truncate(long).split("\n")
    assert eco.SMART_TRUNCATE_MIN_LINES == 250
    assert lines[119] == "line 119" and lines[120] == "… +4820 lines truncated (ECO)"
    assert "+4820 lines truncated (ECO)" in en["settings.eco.f.long_desc"]
    assert lines[-60] == "line 4940" and len(lines) == 181
    assert "250 lines" in en["settings.eco.f.long_desc"] and "first 120 and last 60" in en["settings.eco.f.long_desc"]

    # Tier membership matches which card each filter is listed under.
    assert set(eco.TIERS["light"]) == {"dedup-log", "build-output", "git-status", "grep"}
    assert set(eco.TIERS["aggressive"]) - set(eco.TIERS["light"]) == {
        "tree", "ls", "find", "git-log", "git-diff", "smart-truncate"}


def test_settings_explainer_diff_and_log_examples_hold():
    diff = "\n".join(["diff --git a/src/app.js b/src/app.js", "@@ -1,7 +1,42 @@"]
                     + [f"+add {i}" for i in range(42)] + [f"-del {i}" for i in range(7)]
                     + [f"+more {i}" for i in range(80)])
    out = eco.git_diff(diff)
    assert out.split("\n")[0].endswith("+122 -7")
    assert "… +29 lines of hunk omitted (ECO)" in out
    log = "commit abcdef1\nAuthor: x\n\n    subject\n\ndiff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b"
    assert eco.git_log(log).endswith("… diff body omitted (ECO)")


def test_settings_explainer_every_cut_is_counted_and_a_second_pass_is_a_no_op():
    # The "Counted" guarantee. Not every cut carries the ECO marker (grep and
    # git status say "… +N more"), so the copy must not claim one — but each
    # says how much went, and re-running over the result changes nothing.
    import json
    from pathlib import Path
    en = json.loads((Path(eco.__file__).parent / "locales" / "en.json").read_text(encoding="utf-8"))
    assert '"… +27 more"' in en["settings.eco.safety.marked"]
    samples = {
        "light": ["\n".join(f"app.js:{i}:hit {i}" for i in range(37)),
                  "\n".join(f" M src/components/panels/file_{i}.py" for i in range(25)),
                  "\n".join(["starting"] + ["retrying the upstream connection…"] * 40 + ["done"])],
        "aggressive": ["\n".join(f"line {i}" for i in range(5000)),
                       "\n".join(f"src/dir{i % 3}/file{i}.py" for i in range(60))],
    }
    for tier, texts in samples.items():
        for text in texts:
            once, name = eco.compact_text(text, tier)
            assert name is not None, (tier, text[:40])
            assert "more" in once or "ECO" in once
            assert eco.compact_text(once, tier)[0] == once


# ---------------------------------------------------------------------------
# End to end: a compacted request must WRITE its saving to usage_history.
#
# The existing coverage checked the pieces — compact_request produces stats,
# record() stores the columns, and _wrap_with_usage_capture's source mentions
# them. None of that catches the thing most likely to break: a call site that
# forgets to pass eco_stats through. These drive a real request through
# Gateway.handle() with ECO on and then read the stored row.
# ---------------------------------------------------------------------------

def _compressible_body() -> bytes:
    import json as _json
    tool_text = "\n".join(["Compiling frobnicator v0.1.0"] * 60)
    return _json.dumps({
        "model": "claude-sonnet-5", "max_tokens": 64,
        "messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": tool_text}]}],
    }).encode()


@pytest.fixture
def eco_gateway_env(monkeypatch, tmp_path):
    from claude_unlimited import config, db
    import claude_unlimited.gateway as gateway_module

    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE",
                        tmp_path / "runtime_state.json")
    monkeypatch.setattr(db, "_degraded", {})

    class FakeSecretStore:
        def get_token(self, profile_id):
            return f"tok-{profile_id}"

    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore())
    return tmp_path


def _sse_with_usage(input_tokens: int = 40) -> bytes:
    """A minimal Anthropic stream carrying a model and a usage block, which is
    what UsageCapture needs before it will record anything at all.

    `input_tokens` has to be PLAUSIBLE for the body that was actually sent:
    calibrated_tokens_saved() refuses any ratio outside 1-20 bytes/token, so a
    fixture claiming thousands of tokens for a few hundred bytes gets a null
    token saving — correctly, and confusingly if you did not mean it."""
    return (b'event: message_start\n'
            b'data: {"type":"message_start","message":{"model":"claude-sonnet-5",'
            b'"usage":{"input_tokens":' + str(input_tokens).encode() + b',"output_tokens":10}}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n')


def _believable_input_tokens(body: bytes) -> int:
    """~4 bytes per token, the middle of the band the calibration trusts."""
    from claude_unlimited import eco as _eco
    import json as _json
    compacted, _ = _eco.compact_request(_json.loads(body), "light")
    return max(1, len(_json.dumps(compacted).encode()) // 4)


def test_an_eco_compacted_request_records_its_saving(eco_gateway_env, monkeypatch):
    from claude_unlimited.config import Pool, Profile, Settings, save_pool
    from claude_unlimited.gateway import Gateway
    from claude_unlimited.upstream import UpstreamResponse
    from claude_unlimited import usage_history

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)],
                   settings=Settings(eco_tier="light")))

    class FakeConnection:
        def close(self):
            pass

    sent = {}
    original = _compressible_body()
    billed = _believable_input_tokens(original)

    def transport(req):
        sent["body"] = req.body
        return UpstreamResponse(
            status=200,
            headers={"content-type": "text/event-stream",
                     "anthropic-ratelimit-unified-5h-utilization": "0.2",
                     "anthropic-ratelimit-unified-5h-reset": "1799999999"},
            body_chunks=iter([_sse_with_usage(billed)]), connection=FakeConnection())

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, original)
    list(result.body_chunks or ())   # draining is what triggers the record

    # The compaction really happened on the wire, not just in the stats.
    assert len(sent["body"]) < len(original)

    row = usage_history.list_events()[-1]
    assert row.eco_bytes_saved and row.eco_bytes_saved > 0
    assert row.eco_mode == "light"
    # Calibrated from THIS request's billed tokens, so it must be a plausible
    # fraction of them rather than a fixed guess.
    assert row.eco_tokens_saved and row.eco_tokens_saved > 0


def test_eco_off_records_no_saving(eco_gateway_env):
    from claude_unlimited.config import Pool, Profile, Settings, save_pool
    from claude_unlimited.gateway import Gateway
    from claude_unlimited.upstream import UpstreamResponse
    from claude_unlimited import usage_history

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)],
                   settings=Settings(eco_tier="off")))

    class FakeConnection:
        def close(self):
            pass

    gw = Gateway(transport=lambda req: UpstreamResponse(
        status=200,
        headers={"content-type": "text/event-stream",
                 "anthropic-ratelimit-unified-5h-utilization": "0.2",
                 "anthropic-ratelimit-unified-5h-reset": "1799999999"},
        body_chunks=iter([_sse_with_usage()]), connection=FakeConnection()))

    result = gw.handle("POST", "/v1/messages", {}, _compressible_body())
    list(result.body_chunks or ())

    row = usage_history.list_events()[-1]
    # Absent, not zero: 0 would read as "ECO ran and saved nothing".
    assert row.eco_bytes_saved is None
    assert row.eco_mode is None


def test_a_codex_request_records_its_saving_too(eco_gateway_env, monkeypatch):
    """Parity across Profile kinds: the codex path is a SEPARATE call site into
    _wrap_with_usage_capture, so it can lose eco_stats on its own."""
    import claude_unlimited.gateway as gateway_module
    from claude_unlimited.config import Pool, Profile, Settings, save_pool
    from claude_unlimited.gateway import Gateway
    from claude_unlimited.openai_bridge import OpenAIBridgeResult
    from claude_unlimited import usage_history

    save_pool(Pool(profiles=[Profile(id="c", name="C", kind="codex",
                                      auth_mode="chatgpt_subscription",
                                      automatic=True, enabled=True)],
                   settings=Settings(eco_tier="light")))

    seen = {}

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        seen["body"] = body
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter([_sse_with_usage()]))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)

    gw = Gateway(transport=lambda req: None)
    original = _compressible_body()
    result = gw.handle("POST", "/v1/messages", {}, original)
    list(result.body_chunks or ())

    # The bridge is handed the COMPACTED body, which is also what sent_bytes
    # must measure — calibrating against the original would skew the ratio.
    assert len(seen["body"]) < len(original)

    row = usage_history.list_events()[-1]
    assert row.eco_bytes_saved and row.eco_bytes_saved > 0
    assert row.eco_mode == "light"


def test_a_wildly_compacted_request_records_bytes_but_no_token_estimate(eco_gateway_env):
    """The end-to-end consequence of the calibration band, which is easy to
    mistake for a bug: collapse 1.7 kB of repeated build output down to ~100
    bytes and the billed-token ratio lands far outside 1-20 bytes/token, so the
    token figure is left NULL while the exact byte saving is still stored.
    Absent beats invented — the Statistics panel would otherwise show a
    confident number derived from nothing."""
    from claude_unlimited.config import Pool, Profile, Settings, save_pool
    from claude_unlimited.gateway import Gateway
    from claude_unlimited.upstream import UpstreamResponse
    from claude_unlimited import usage_history

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)],
                   settings=Settings(eco_tier="light")))

    class FakeConnection:
        def close(self):
            pass

    gw = Gateway(transport=lambda req: UpstreamResponse(
        status=200,
        headers={"content-type": "text/event-stream",
                 "anthropic-ratelimit-unified-5h-utilization": "0.2",
                 "anthropic-ratelimit-unified-5h-reset": "1799999999"},
        # Implausible for a ~100-byte body: 4000 tokens is 0.025 bytes/token.
        body_chunks=iter([_sse_with_usage(4000)]), connection=FakeConnection()))

    result = gw.handle("POST", "/v1/messages", {}, _compressible_body())
    list(result.body_chunks or ())

    row = usage_history.list_events()[-1]
    assert row.eco_bytes_saved and row.eco_bytes_saved > 0   # measured, exact
    assert row.eco_tokens_saved is None                      # not measurable, so not claimed
