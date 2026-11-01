# Serving the codex CLI from the pool

Status: accepted

## Context

Issue #7 asked for the pool's ChatGPT accounts to serve OpenAI's own `codex`
CLI, not only Claude Code. Codex CLI 0.149 offers two ways to point it
somewhere else:

1. **`openai_base_url` on the built-in `openai` provider.** codex keeps
   requiring a ChatGPT login (so a fake `auth.json` would have to exist and
   would be refreshed against OpenAI by codex itself), compresses request
   bodies with zstd, fetches `/models`, and tries a WebSocket first.
2. **A custom model provider** (`model_providers.<id>` with `env_key` and
   `wire_api = "responses"`). No login, plain JSON bodies, HTTP SSE only. It
   can be set per invocation with `-c` overrides.

How codex reacts to errors decides where rotation can live. It does not retry
a 429 `usage_limit_reached` (it prints "try again at HH:MM" from `resets_at`)
or a 400; it retries a 5xx 30 times in about 24 seconds; and it never switches
account itself. Reasoning `encrypted_content` and `x-codex-turn-state` are
minted per account and refused by any other.

## Decision

- **`cu codex` launches the real `codex` with a custom provider, set by `-c`
  overrides for that one run** (`codex_cli.launch_overrides`). The user's
  `~/.codex`, their own login and plain `codex` are never touched. The local
  placeholder token (or a `--profile` session token) is passed in
  `CLAUDE_UNLIMITED_CODEX_TOKEN`.
- **`POST /codex/v1/responses` is a passthrough**, not a translation: codex
  already speaks the backend's dialect. An allowlist of codex's own headers is
  forwarded; the credential and account id are substituted; the body and the
  SSE stream are relayed byte for byte, with usage read off
  `response.completed` as it passes. `GET /codex/v1/models` is relayed from
  one pool account so codex sees the same catalogue as with its own login.
- **All rotation happens in the daemon, before the first byte.** 401/403/429/
  5xx from one account move the request to the next. Once a response has
  started, it is never retried.
- **Errors use the shapes codex acts on correctly.** An exhausted pool is a
  429 `usage_limit_reached` with `resets_at`; anything the user must fix is a
  400. The daemon never answers a pool problem with a 5xx.
- **A conversation is pinned to the account that served it** (keyed by
  `prompt_cache_key`, codex's thread id). When it has to move, reasoning items
  and the turn-state header are stripped; messages, tool calls and outputs are
  kept. A 400 refusing ciphertext on an unpinned conversation (begun on the
  user's own login, or before a restart) is retried once, stripped.
- **Only enabled ChatGPT-subscription Profiles serve it.** Never a Claude
  account, never an API key. codex traffic keeps its own rotation pointer and
  does not move Claude Code's.
- **Off by default** (`codex_cli_serving`), with OpenAI's terms quoted next to
  the switch and in the README.

## Consequences

- ECO, primitive speech, the capacity guard and model parity do not apply on
  this path; codex gets exactly what it asked for.
- A conversation that moves account loses its cached reasoning and prompt
  cache for one turn.
- Thread pins live in memory: after a daemon restart a conversation is pinned
  again on its next request, and the ciphertext-refusal retry covers the gap.
- Only one ChatGPT account was available when this shipped, so rotation
  between two real accounts is covered by the offline tests, not a live run.
