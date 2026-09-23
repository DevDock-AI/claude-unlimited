# 1M context through the gateway: the base URL, not the plan, is what caps it

Status: accepted

Answers issue-driven reports that `cu code` sessions were capped at 200K
context even on natively-1M models.

## Observed

A user running `cu code` on two Claude Pro accounts saw immediate, repeated
compaction ending in Claude Code's auto-compact thrashing error. The open
question was whether pointing `ANTHROPIC_BASE_URL` at our loopback daemon makes
the client budget a native-1M model at 200K.

It does — for **every** native-1M model, not just some of them. Measured on this
machine against the running daemon, Claude Code 2.1.278, reading the client's own
`context_window.context_window_size` out of a status-line payload:

| Model | native 1M? | `cu code`, as shipped | with the fix |
|---|---|---|---|
| `claude-opus-5` | yes | 200,000 | **1,000,000** |
| `claude-sonnet-5` | yes | 200,000 | **1,000,000** |
| `claude-fable-5-1` | yes | 200,000 | **1,000,000** |
| `claude-opus-4-8` | yes | — | **1,000,000** |
| `claude-sonnet-4-6` | **no** | — | 200,000 |
| `claude-haiku-4-5` | **no** | — | 200,000 |

The last two rows are the control that matters: the fix does not inflate a
model that is not natively 1M, so the client is never told something false.

No API request is involved: the window is computed at client startup, so the
probe costs nothing and spends no quota.

## Cause

Read out of the installed binary (not from documentation), 2.1.278 decides a
native-1M model's window like this:

```js
Oy(model):                                   // is 1M active?
  if (CLAUDE_CODE_DISABLE_1M_CONTEXT) return false
  if (!isNative1m(model))            return false
  surface = Oe()
  if (surface === "firstParty" && fs() || uI(surface) || surface === "mantle") return true
  return wae(surface, ctx)                   // a bedrock/vertex/foundry/gateway table

fs():  if (_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL) return true
       return host(ANTHROPIC_BASE_URL) === "api.anthropic.com"   // unset also passes
```

Two things follow, and both are counter-intuitive:

1. **`Oe()` never looks at `ANTHROPIC_BASE_URL`.** Its `"gateway"` value means
   Claude Code's *own* enterprise gateway auth/server process, which we never
   set. A `cu code` session is therefore classified **`firstParty`**.
2. Being `firstParty` with a non-Anthropic base URL fails `fs()`, and then falls
   through to `wae("firstParty", …)`, whose switch has no `firstParty` case and
   returns `false`.

So the 3p capability table is **never reached**, which is why even
`claude-sonnet-5` — the only model in the baked catalogue carrying a
`native_1m_3p: {bedrock, vertex, foundry}` block — is capped at 200K through the
pool. The cap is caused by the base URL's hostname and nothing else: not the
plan, not the account, not the model.

The baked catalogue has **8** native-1M models: `claude-sonnet-5`,
`claude-opus-4-7/4-8/5`, `claude-fable-5/5-1`, `claude-mythos-5/5-1`. All 8 were
capped; all 8 are fixed by the same single predicate.

## Fix

Set `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1` for the launched client, from
`cli._apply_one_million_context`, decided by the pure
`cli._one_million_decision`.

For an `oauth` or Anthropic-`api` route the variable states something **true**:
the daemon relays to `api.anthropic.com` with the user's own credential, so the
1M capacity Claude Code is being conservative about genuinely exists. Claude
Code's caution is correct in general and wrong for this particular gateway.

`Settings.context_1m` — `auto` (default) | `client_default` | `prefer_200k`.

> **Amended 2026-09-23:** ADR 0009 added a fourth value, `force_1m`, and it is now the default.
> A pooled session is overwhelmingly on Claude accounts, where 200K only means compacting
> several times as often. `auto` remains available as the cautious choice for pools that route
> much of their traffic to backends smaller than 1M.
`auto` sets it only when **every** profile that could serve the session is
Anthropic-backed:

| Situation | Decision |
|---|---|
| all-Anthropic pool, or `--profile` pinned to one | `enabled_1m_verified` |
| a rotation-reachable `codex` profile in the route | `mixed_backend_capacity` — *retired by [ADR 0009](0009-per-request-capacity-guard.md)* |
| an `api` profile whose `base_url` is not Anthropic | `custom_gateway_unknown` |
| `claude --version` unreadable, or older than 2.1.229 | `client_version_unverified` |
| user set `CLAUDE_CODE_DISABLE_1M_CONTEXT` | `user_forced_200k` |
| user already set the variable themselves | `user_already_set` |

A pin rescues a mixed pool: `--profile` is a guarantee of exactly one account,
so the rest stops mattering. The refusals that are not the user's own choice are
printed at launch, so a 200K session is never silently 200K.

**"In the route" means reachable by rotation**, not merely present in the pool.
`router.choose()` and `choose_for_new_branch()` both filter candidates on
`automatic`, so an enabled-but-manual-only Codex account — a common "keep it as
a fallback" setup — does not cost the whole pool its window. The one exception
is `forced_for_subagents`, which `gateway._branch_decision` honours whatever
`automatic` says, so that flag does still block.

This is deliberately not a claim that the session can never reach a Codex
account: a mid-session **Take over**, or a second terminal running
`cu code --profile <that account>`, still can, and the window is fixed by then.
The line drawn is "the pool will not do it on its own", which is the difference
between a surprise and a choice.

**The decision reason is published.**
`GET /api/settings` carries `context_1m_preview` — the same pure function run
over the same pool, with no pin and an empty environment — and Settings renders
it as a plain sentence ("Right now: 200K. A ChatGPT/Codex account is in the
rotation…"). The dashboard cannot observe the real decision, which happens
inside `cu code` in another process, so this is explicitly a preview of the
common case.

## Validation

* 29 unit tests over the pure decision (`tests/test_cli_one_million_context.py`),
  including the lookalike-host case `api.anthropic.com.evil.example`.
* Live, against the running daemon: four native-1M models 200,000 → 1,000,000,
  and two non-1M models unchanged at 200,000 with the flag set.
* Live, against a real mixed pool (one oauth + one codex profile):
  whole pool → `mixed_backend_capacity`; pinned to the Claude account →
  `enabled_1m_verified`; pinned to Codex → `mixed_backend_capacity`.
* Settings round-trip verified in the running dashboard, and the published
  reason rendered there against that same mixed pool.
* A bug the live check caught and code review would not have: the daemon runs
  under launchd with a minimal PATH, so `shutil.which("claude")` found nothing
  and the preview reported `client_version_unverified` on a machine with a
  working 2.1.278. `cli._claude_executable()` now falls back to the usual
  install locations.

## What it was not

* Not the **`gateway`** surface, and not the documented `sonnet[1m]`
  remedy. Neither applies: our surface is `firstParty`, and the suffix aliases
  (`sonnet[1m]`, `sonnet-4-6[1m]`, `sonnet-5[1m]`, `opusplan[1m]`) are a
  different mechanism from the native-1M path.
* Not a stripped beta header either: native-1M models need no beta header at
  all. `build_upstream_request` was never implicated.

## Remaining limits

* **The variable is underscore-prefixed** — internal to Claude Code,
  undocumented, and free to change or disappear in any release. Hence the
  version gate and the three-way setting. If it stops working the failure is
  soft: the window returns to 200K.
* **Codex profiles get nothing here** and deliberately so. A 1M client window in
  front of a GPT backend would be discovered only by overflowing it.
* **Mid-session rotation** — *superseded by [ADR 0009](0009-per-request-capacity-guard.md).*
  As written, `auto` refused any pool whose rotation could reach a codex
  profile (`mixed_backend_capacity`), a static route check. ADR 0009 adds the per-request capacity guard this bullet asked for —
  the gateway estimates each inbound conversation and keeps a codex profile out
  of any turn that would overflow its backend — so a mixed pool now runs at 1M
  (`enabled_1m_guarded`), a codex-only route is refused with its own reason
  (`codex_only_route`), and `mixed_backend_capacity` no longer exists. The
  decision table above is amended accordingly there.
* **Not a quota change.** A larger window does not buy more usage; it means far
  fewer compactions. Whether it resolves the reporter's thrashing error is
  untested — that needs a real long session, and a large file or tool output can still
  refill even a 1M window.
* **Opus/Sonnet 4.6 extended context via credits** is untouched; this ADR covers
  only models that are natively 1M.
