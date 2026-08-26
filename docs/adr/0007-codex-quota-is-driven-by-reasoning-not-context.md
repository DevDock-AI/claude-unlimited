# Codex quota is driven by reasoning output, not by resent context

Status: accepted

Supersedes [0006](0006-codex-response-chaining-not-available.md).

## Context

Routing Claude Code through a codex Profile depletes the Codex quota far faster
than native Codex use. Three explanations were on the table, and all three were
wrong:

1. The bridge re-sends the whole conversation every turn, so we pay for the
   history repeatedly.
2. `previous_response_id` chaining would fix that, but the backend refuses it
   (ADR 0006).
3. Failing that, quota is counted per request, so the remedy is fewer requests
   (ADR 0006's closing hypothesis).

ADR 0006 tested only the plain HTTP endpoint. Reading the real client
(`openai/codex`, `codex-rs/core/src/client.rs`) showed why that was too narrow:
Codex does not chain over HTTP at all. It holds a Responses **WebSocket** open
for the turn, sends `previous_response_id` plus only the items appended since
the last request, and treats server-returned output items as already-known
baseline.

## Measurements

All against the real subscription backend with a real ChatGPT/Codex credential.

**Chaining is available — ADR 0006's central claim is refuted.** Over
`wss://chatgpt.com/backend-api/codex/responses` with header
`OpenAI-Beta: responses_websockets=2026-02-06`:

| Sent | Result |
|---|---|
| `response.create`, `store:false` | `200` |
| `response.create`, `store:false` + `previous_response_id` | `200` — accepted |

The same `previous_response_id` is rejected as an unsupported parameter on the
HTTP endpoint. The transport, not the parameter, was the constraint.

**Resent context is already nearly free.** Two HTTP requests sharing an
11.6k-token prefix, with no `prompt_cache_key` sent:

| Request | input_tokens | cached_tokens |
|---|---|---|
| first | 11,620 | 0 |
| second | 11,620 | 11,008 |

Prefix caching engages on its own. Sending `prompt_cache_key` explicitly
changed nothing (the first request with a key was already served 11,008 cached).

**Input volume does not move the primary window.** Roughly 46,000 input tokens
across four requests moved the 5h window by **0%**.

**Reasoning output on the top tier does.** Same prompt, same 108 input tokens:

| Model / effort | output | reasoning | 5h window |
|---|---|---|---|
| `gpt-5.6-terra` / low (trivial prompt) | 5 | 0 | 0% |
| `gpt-5.6-terra` / medium | 2,371 | 1,335 | 0% |
| `gpt-5.6-sol` / high | 3,575 | 2,424 | 1% |
| `gpt-5.6-sol` / max | 7,282 | 6,214 | 1% |

## Decision

Quota tracks **output and reasoning tokens, weighted by model tier**. It does
not track input volume, and it is not counted per request.

This inverts the remedy. Chaining and history trimming target the one dimension
that is already cheap; they would save upload bandwidth and nothing else. So:

- **No WebSocket transport, no session bookkeeping, no history trimming** for
  quota reasons. The mechanism exists and works, but buys nothing here. Revisit
  only if bandwidth or latency — not quota — becomes the problem.
- **No `prompt_cache_key`.** Caching already applies without it.
- The lever that matters is the model and reasoning effort a request lands on,
  which is what `openai_models.py` decides. `claude-opus-5` maps to
  `gpt-5.6-sol` at `high`, and Claude Code defaults to Opus — so an ordinary
  session runs on the most expensive tier by default. `Profile.codex_model` and
  `Profile.codex_reasoning_effort` are the existing controls, and they are the
  right place to spend effort.

## Consequences

- The bridge keeps sending the full conversation, now for a documented reason
  rather than for lack of an alternative: it costs essentially nothing.
- Anyone revisiting this should re-run the tier table above first. A backend
  that starts charging for input, or a lineup whose tiers price differently,
  would change the conclusion.
- Model ids in that mapping are a moving target — the backend rejects
  `gpt-5.6-codex` outright for ChatGPT accounts — so the bridge walks a
  fallback ladder when a model is refused rather than failing the request. See
  `fallback_models()` in `openai_models.py`.
