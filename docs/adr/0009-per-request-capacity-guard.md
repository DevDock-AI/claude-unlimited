# A per-request capacity guard lets a mixed pool run at 1M

Status: accepted

Supersedes the "Mid-session rotation is still not covered" bullet and the
`mixed_backend_capacity` decision of [ADR 0008](0008-1m-context-through-the-gateway.md).

## Context

ADR 0008 gave `cu code` the 1M window on an all-Anthropic route and refused it
for any pool whose rotation could reach a ChatGPT/Codex Profile: a codex
request is translated to a GPT model with its own window, and a 1M client in
front of it would only discover that by overflowing it. That refusal was
static — it cost the whole pool the window for the mere possibility of one
oversized turn landing on the codex account.

Three facts, read out of the installed Codex CLI, the ChatGPT backend's model
listing it caches, and the installed Claude Code 2.1.278 binary, made a
per-request guard possible:

1. **The ChatGPT/Codex backend's windows are small, uniform and published.**
   Every slug the backend lists (`gpt-6-astra`, `gpt-5.6-sol/terra/luna`,
   `gpt-5.5`, …) has `context_window = 272,000` and
   `effective_context_window_percent = 95`; `max_context_window` (872K for the
   5.6 line) is a ceiling a user can raise the CLI's budget to, **and the
   boundary of OpenAI's higher-usage band** — above 272K input a request is
   charged at a higher rate for the whole request. LiteLLM's figures (922K) are
   the public API's and are wrong for this backend by ~3.4x, so the model
   catalogue cannot be the source.
2. **Claude Code reactively compacts on exactly one error.** A 400/413 whose
   message matches `/prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)/i`
   in Anthropic's error envelope makes it compact by the parsed gap and
   re-dispatch the same turn. A 503/`overloaded_error` instead makes it hold
   for ten minutes and retry the same body — the worst possible answer to an
   oversized request. An OpenAI-shaped 400 relayed from the backend is not
   recognised at all.
3. **The router already had the right shape.** `must_leave()` is a per-Profile
   fact composed into both choosers; the capacity fit is a per-*request* fact
   that composes the same way, without touching `ProfileRuntime.state`.

## Decision

**Size every real turn in the gateway and keep a codex Profile out of any turn
its backend cannot hold.** Concretely:

* `gpt_windows.py` (new, pure) hardcodes the windows, keyed by backend:
  ChatGPT subscription (`context_window`, the higher-usage boundary) and the
  public OpenAI API (max input) for `api_key` codex Profiles. A custom
  `base_url` is an unknown backend. **Budget = floor(window × 0.95) − 32,000**
  output/reasoning reserve: **226,400** estimated input tokens on a
  subscription. The table is maintained by hand: `scripts/check_gpt_windows.py`
  diffs it against the Codex CLI's cache before a push, outside pytest.
* **Unknown GPT id → assumed at 272K, said once in Activity, shown as
  "assumed" in Settings** — never excluded on a guess.
* **Estimate** (`gpt_windows.estimate_input_tokens`): stage 1 is free — a body
  under 673,200 bytes cannot exceed the smallest budget at 3 bytes/token, so
  nothing is parsed; stage 2 reuses the request's one parse, subtracts image
  base64 (a screenshot is ~1.6K tokens, not 460K), and counts
  `ceil(text_bytes / 3) + 1,600 per image + 2,000`. `/3` is the
  "err toward excluding codex" margin. An unparsable body is sized from its
  bytes alone, which only over-estimates. Any failure inside the guard
  degrades to "not sized" — today's routing — and an oauth/api Profile is
  never sized at all.
* **Router**: `RequestFit(estimated_tokens, over_capacity)` and `fits()`;
  both choosers take `fit`. Over-capacity is treated like not-ELIGIBLE, **not**
  like `must_leave`: a spent Fable week has "serve anyway, the provider says
  no" as its honest degrade; an overflow has none. A sticky current that no
  longer fits hands over with reason `window_handover`; when nothing fits the
  reason is `no_profile_fits_request`.
* **Gateway**, when nothing that may take the request can hold it: **HTTP 400
  in Anthropic's exact envelope**, `invalid_request_error`,
  `prompt is too long: N tokens > M maximum` with the real estimate and the
  largest budget among the accounts considered, **no `[claude-unlimited]`
  prefix**, no `retry-after`. Never a 503; the existing capacity-exhaustion
  503 is unchanged and still wins when the pool is genuinely out.
* **Explicit choices are honoured, never substituted** — `--profile <codex>`
  and **Take over** get that 400 instead of a request known to fail, and stay
  on their account. `forced_for_subagents` falls through to the balanced
  selector (the flag already documents "falls back when unavailable"). A
  branch pin on codex whose conversation outgrew it **moves for good**, and the
  move says why.
* **No new setting for the guard**: it is part of `context_1m` and inert
  under `prefer_200k`/`client_default` (a 200K conversation never trips it).
  Under `auto` a mixed pool now decides `enabled_1m_guarded`; a route with
  nothing but codex accounts decides `codex_only_route` (1M there would only
  mean compacting at ~226K instead of 200K). A non-Anthropic api gateway stays
  `custom_gateway_unknown`. `mixed_backend_capacity` no longer exists.
* **A fourth `context_1m` value, `force_1m`**: sets the
  variable on every session whatever the route or client version. The guard
  stays active under it; the user's own `CLAUDE_CODE_DISABLE_1M_CONTEXT` and a
  pre-set variable still win; it cannot enlarge a non-native-1M model and a
  foreign backend may reject what the client then sends — all said in the
  Settings sub-label.

Decision table, replacing ADR 0008's:

| Situation (`auto`) | Decision |
|---|---|
| all-Anthropic route, or `--profile` pinned to one | `enabled_1m_verified` |
| a rotation-reachable (or forced-for-subagents) codex Profile beside a Claude one | `enabled_1m_guarded` |
| only codex Profiles in the route, or `--profile` pinned to one | `codex_only_route` |
| an `api` Profile whose `base_url` is not Anthropic | `custom_gateway_unknown` |
| `claude --version` unreadable, or older than 2.1.229 | `client_version_unverified` |
| user set `CLAUDE_CODE_DISABLE_1M_CONTEXT` | `user_forced_200k` |
| user already set the variable themselves | `user_already_set` |
| `force_1m` (any route, any client, env still wins) | `forced_1m` |

## Consequences

* A mixed pool runs at 1M. The one visible sign of the guard is a compaction
  that comes earlier than it would on Claude accounts alone — only when
  nothing but a ChatGPT account is left to take a long turn.
* The budget line is **272K, not 872K**: budgeting past it would walk a
  subscription into the higher-usage band silently, which is exactly what
  ADR 0007's quota concern forbids. Raising it would be a per-Profile opt-in,
  not a default, and the server's behaviour above 872K is unverified.
* Two Claude Code internals are now depended on: the underscore variable
  (ADR 0008) and the prompt-too-long parser. Both are read out of 2.1.278 and
  both fail soft — the window returns to 200K, or the client surfaces the
  error text instead of compacting.
* The estimator is a heuristic (3.0 bytes/token, 1,600/image, 32K reserve),
  chosen conservatively and unvalidated against real
  `(payload_bytes, prompt_tokens)` pairs; calibrating it is a later, separate
  step and does not change the design.
* The macOS HUD shows accounts and usage, not windows: nothing to add there.

## Surfaces

`gpt_windows.py`, `router.py`, `gateway.py`, `daemon.py` (error mapping,
`context_1m_preview.guard`), `cli.py` (launch notes), `context_window.py`,
`config.py` (`force_1m`), `static/index.html`, `static/app.js`, all four
locales, README "The 1M context window", `scripts/check_gpt_windows.py`,
CONTRIBUTING, RELEASING, tests (`test_gpt_windows`, `test_router`,
`test_gateway_codex`, `test_daemon_proxy_e2e`, `test_cli_one_million_context`,
`test_dashboard_settings_api`).
