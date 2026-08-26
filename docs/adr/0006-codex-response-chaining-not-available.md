# Response chaining is not available on the Codex subscription backend

Status: superseded by [0007](0007-codex-quota-is-driven-by-reasoning-not-context.md)

> This ADR tested only the HTTP endpoint and concluded chaining was impossible.
> It is wrong: the real client chains over a WebSocket, which does accept
> `previous_response_id`. Its closing hypothesis — that quota is counted per
> request — is also wrong. See 0007 for the measurements and the actual driver.

## Context

Routing Claude Code through a codex profile depletes the Codex quota far faster
than native Codex use. The suspected cause was that the bridge re-sends the
whole conversation on every request, while native Codex chains within a turn
using `previous_response_id` and sends only the delta.

A detailed plan proposed adding per-session response chaining to the bridge. Its
own first phase required proving empirically that chaining reduces quota before
any of it was built, because OpenAI documents that a chained request is still
billed for the full reconstructed context.

## Measurement

Run against the real subscription backend
(`https://chatgpt.com/backend-api/codex/responses`) with a real ChatGPT/Codex
credential, three requests total:

| Sent | Result |
|---|---|
| `store: true` | `400 {"detail":"Store must be set to false"}` |
| `store: false` | `200` — normal response |
| `store: false` + `previous_response_id` | `400 {"detail":"Unsupported parameter: previous_response_id"}` |

## Decision

The plan is not implementable for subscription profiles, and it is not a matter
of tuning:

- Cross-request chaining requires server-side storage, and this backend refuses
  `store: true` outright.
- `previous_response_id` is rejected as an unsupported parameter regardless of
  the store setting.

The two requirements exclude each other. No amount of session bookkeeping in the
bridge changes what the backend accepts.

The plan remains applicable in principle to raw API-key profiles against
`api.openai.com`, which does support both parameters. That was not measured
here, and would only help users on an API key — not the subscription users the
problem was reported for.

## What the measurement also showed

A request billing **19 input tokens** moved the primary quota window
materially. If quota tracked input tokens, a request that small would be close
to free.

That points away from payload size as the cause and toward quota being counted
per request. If that holds, the remedy is **fewer requests**, not smaller ones —
a different problem with different fixes, such as reducing the number of
round-trips a single Claude Code turn produces. That hypothesis is untested and
should be measured before anything is built on it, for the same reason this ADR
exists.

## Consequences

- The bridge keeps sending the full conversation, because it has no alternative
  on this backend.
- No `codex_sessions.py`, no per-profile `store` setting, no session bookkeeping.
- Anyone revisiting this should re-run the three requests above first: a backend
  that starts accepting `previous_response_id` would reopen the question.
