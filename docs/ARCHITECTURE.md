# Architecture

How Claude Unlimited is put together. Start with [`README.md`](../README.md) for what it
does; this is the map for someone about to change it.

See [`docs/adr/`](adr/) for why specific calls were made, and
[`CONTRIBUTING.md`](../CONTRIBUTING.md) for the rules a change has to follow.

## The shape

One process, one loopback port, two jobs:

```
Claude Code ──▶ 127.0.0.1:4317 ──┬──▶ /api/*  the Dashboard's own API
                                 ├──▶ /       the Dashboard itself
                                 └──▶ *       the live proxy, forwarded upstream
```

The two namespaces never share auth logic. `/api/*` is CSRF- and Host-checked and never
touches a provider credential. Everything else is checked against the local placeholder
token and never sees the Dashboard's CSRF token.

Because the proxy is the catch-all, **any path the daemon does not explicitly recognise is
forwarded upstream**. Adding a Dashboard route means adding it to `_VIEW_ROUTES` in
`daemon.py` and `VIEW_ROUTES` in `static/app.js`, which a test holds to agreement.

## Profile kinds

A Profile is one account. Three kinds share the same rotation, thresholds and Dashboard:

| kind | What it is | How it talks upstream |
|---|---|---|
| `oauth` | A Claude Pro/Max subscription | Proxied byte-for-byte to Anthropic |
| `api` | An Anthropic API key or compatible gateway | Proxied byte-for-byte |
| `codex` | A ChatGPT/Codex subscription | Translated to and from OpenAI's Responses API |

They differ in credential handling, transport and quota signals. A change verified on one
kind is not verified on the others — this is the project's most repeated defect.

## Module map

**Request path** — everything a live session touches.

- `gateway.py` — orchestrates one request: pick a Profile, send it, classify the answer,
  rotate and retry on failure. The one place the three kinds branch.
- `router.py` — the pure rotation decision. No I/O, no clock of its own, fully unit tested.
- `observation.py` / `openai_observation.py` — turn a provider response into one of a small
  set of facts: usage snapshot, quota exhausted, rate limit, auth invalid, unavailable.
- `proxy.py` / `upstream.py` — build and send the real upstream request, substitute the
  credential, strip hop-by-hop headers, rewrite `metadata.user_id`.
- `openai_bridge.py` — the codex path: owns its own HTTPS call and response translation.
- `openai_translate.py` — pure Anthropic ⇄ OpenAI shape mapping, both directions.
- `wire_formats.py` — which endpoint shape a Profile speaks, and how to translate to it.
- `usage_tracking.py` — tees the response to count tokens without altering a byte of it.
- `session_tokens.py` — the per-session credential behind `code --profile` and
  `code --distribute`. Resolves to a `SessionGrant`: a pin, or distribute mode.
- `project_attribution.py` — what a request says about itself: the project it came from,
  the session it belongs to, and whether a subagent or the main agent sent it.

**State**

- `config.py` — the `Profile`/`Pool` model and atomic on-disk persistence. Profiles save
  via `asdict()` but load by explicit enumeration, so the load side is what drifts.
- `profiles.py` — the only place config.json and the keychain are coordinated. Validates
  everything a caller may set, because `load_pool()` coerces on read.
- `secret_store/` — credentials, one backend per OS behind one interface.
- `runtime_state.py` — the Dashboard-visible slice of live state, across restarts. A
  display cache: rotation state itself deliberately does not persist.
- `usage_history.py` / `pricing.py` — per-request tokens and estimated cost.
- `activity.py` — the append-only event log behind the Activity page.
- `export_import.py` — encrypted bundles. The only user of `cryptography`.

**Surface**

- `daemon.py` — the HTTP server and every route.
- `static/` — the Dashboard. Plain HTML/CSS/JS, no build step.
- `locales/*.json` — one flat key→string map per language. A missing key falls back to
  English; a key in only one file is a bug a test catches.
- `cli.py` — daemon lifecycle, the interactive logins, `code`, `desktop`, `purge`.
- `daemon_installer/` — auto-start, one backend per OS behind one interface.
- `notifications.py` — OS-native desktop notifications, no dependency.
- `updater.py` — checks, verifies and installs releases.
- `connection_test.py` — the Profiles menu's "Test connection".

## How a request flows

1. Claude Code sends a normal Anthropic request to the loopback port.
2. `gateway.handle()` recovers any expired cooldowns, then asks `router.choose()` for a
   Profile.
3. The credential is fetched from the OS keychain and substituted in.
4. For `oauth`/`api` the request is relayed as-is; for `codex` it is translated.
5. The response is classified into an `Observation` and folded back into rotation state.
6. On quota exhaustion the next eligible Profile is tried, invisibly — the client sees one
   request and one answer.
7. The body streams back untouched while a tee counts tokens for the usage history.

## Rotation rules

- Requests go to the enabled Profile with the **lowest priority number**.
- It stays there — *sticky* — until it crosses its `switch_threshold` or hits a real quota
  limit.
- A brief rate limit is a cooldown, never a state change away from eligible.
- A cooldown honours a real `Retry-After`; with none, it backs off exponentially, because
  retrying a rate-limited endpoint every minute never lets the window clear.
- When a window resets, the Profile rejoins rotation automatically.
- An account whose credential was rejected shows "needs re-auth" and tries its own refresh
  token to recover, rather than waiting for a manual re-login.

## Branches: sessions and subagents

A Claude Code session is not one caller. The main agent and every subagent it spawns issue
their own requests, and Claude Code labels them: `x-claude-code-agent-id` is sent **only by
a subagent** (absence means the main agent), and it is stable for that subagent's whole
life. The session id — body `metadata.user_id.session_id`, header as fallback — is shared
by the whole lineage. Together they form a **branch key**: `(session_id, agent_id)`, with
`agent_id = "main"` for the main agent.

Branches matter because the prompt cache is per-account. Rotating a branch to a different
account costs a full cache miss; keeping a branch on one account keeps it warm. So the
gateway holds `_branch_pins`: a bounded, TTL'd `branch key → profile` map (1 h, 512
entries, LRU). It is a routing preference, never a guarantee — a pin whose Profile went
ineligible, was already attempted this request, or was deleted is dropped, and the request
falls through to normal rotation.

Precedence for one request, first match wins:

1. **Session pin** (`code --profile`) — every request in the terminal, main and subagents
   alike, goes to that one Profile.
2. **Live branch pin** — this branch already has a warm account. (`branch_pinned`)
3. **Forced-for-subagents Profile** — if a subagent sent this and some Profile carries
   `forced_for_subagents`, and it is eligible. (`subagent_forced`)
4. **New branch assignment** — `router.choose_for_new_branch()` picks the least-loaded
   eligible Profile and the choice is remembered as this branch's pin. Only in distribute
   mode, or for a subagent when a forced Profile exists but is not currently eligible.
   (`branch_assigned`)
5. **Normal rotation** — `router.choose()`, sticky-until-threshold.

Distribute mode is `grant.distribute OR settings.distribute_sessions_default` — the
per-session `code --distribute` flag, or the global Settings toggle. OR-ed, never assigned,
so the setting can only turn distribution on; `_branch_decision` reads it from the pool it
already loaded, so toggling takes effect on the next request without a restart. `cli.code()`
reads the same setting, only so the interactive profile picker doesn't pin a session that
was meant to distribute.

`forced_for_subagents` is a per-Profile flag, at most one holder pool-wide (`config.py`
refuses a second on save, and `load_pool` normalizes a file carrying several to the first
enabled holder — the one routing uses — so a bad file can't wedge every later save).
Claiming it is refused while another *enabled* Profile holds it; a disabled holder routes
nothing, so its flag moves to the claimant. It is asymmetric on purpose: it steers subagents only, so a
Claude orchestrator can run its subagents on a Codex account. If that account is
exhausted or disabled, subagents fall to step 4 rather than failing.

Branch routing bypasses the "Rotated" global-pointer update the way a session pin does: a
branch choosing its own account must not move the pointer every other session follows.
Because that traffic never moves `current_profile_id`, the Dashboard reads
`Gateway.live_agent_counts()` (`live_agents` in `/api/status` and `/api/profiles`) to show
which accounts are actually serving agents. When a live pin has to be re-assigned because
its account stopped being eligible, `_record_branch_move` logs every move and fires the
"rotated" notification at most once per source account every
`BRANCH_MOVE_NOTIFY_INTERVAL_SECONDS`. The branch key itself — a full JSON parse of the
request body — is only computed when a per-branch mode can apply to the request.

## Usage freshness checks

`usage_probe.py` reads subscription usage from the providers' read-only endpoints —
`api.anthropic.com/api/oauth/usage` for oauth Profiles, `chatgpt.com/backend-api/wham/usage`
for subscription codex Profiles — and converts each response into the same rate-limit
headers a real response carries, so `daemon._record_ping` feeds it through the ordinary
observation path. `Scheduler` decides who is due: only while the user is present (proxied
traffic or `POST /api/presence` from real Dashboard input, idle after 30 minutes), one read
per account every 5–10 minutes counted from its last reading from *any* source
(`Gateway.usage_observed_at`), at most two per 30-second tick. A 429 backs off 15 min → 6 h
and pauses the provider; 401/403 backs off 1 h → 24 h without touching the Profile's state;
other failures 5 min → 1 h. Backoff persists in `usage_probe_state.json`. Tokens are
refreshed only through the existing per-Profile refresh clocks. Off switch:
`Settings.keep_usage_fresh`.

## Usage tracking

Each recorded event also carries `requested_model` whenever the client asked for a
different model than the one that served it — a codex-kind Profile answers as `gpt-*`, so
without it the log cannot distinguish a Fable request from an Opus one.

`usage_tracking.py` is a **strict tee**. Every chunk read from upstream is yielded onward
unmodified and in order; a separate copy is parsed for token counts. Every parse is
guarded, so a malformed body costs a usage record and never the response.

Recording happens once the body is fully forwarded, on the committed (non-retry) path
only. A client that disconnects mid-stream simply gets no usage record.

## Model parity (codex only)

Claude Code asks for a Claude model; something has to choose which GPT model answers and
how hard it reasons. `openai_models.py` owns that mapping and Settings → Models parity
edits it. A Profile with its own override ignores the list.

The parity is an **explicit ordered list** (`Settings.model_parity`) of rows
`{claude_model, model, effort, claude_effort}`; the saved list IS what a codex Profile
advertises at `GET /v1/models` and therefore what `/model` offers. Empty means the default
four (fable/opus/sonnet/haiku family heads). A legacy sparse-dict config is migrated to the
list at read time by `openai_models.normalize_parity` (no on-load rewrite; the file keeps
its old shape until the next save). `claude_effort` is injected as `output_config.effort`
onto oauth/api-served `/v1/messages` requests, gated per model by
`openai_models.apply_claude_effort` so an unsupported model (Haiku, Sonnet ≤4.5) is never
sent a value that would 400. Dropdown order is the catalogue rank (cost/generation desc).

Codex quota is spent on reasoning tokens produced × model tier, not on context size — see
[ADR 0007](adr/0007-codex-quota-is-driven-by-reasoning-not-context.md).

## The Claude desktop app

`claude-unlimited desktop` points the desktop app's inference at the pool. The app calls
this third-party inference mode and runs it from a **separate** userData directory,
`~/Library/Application Support/Claude-3p/`, which is why none of it appears in the normal
profile. The settings live in `configLibrary/<uuid>.json` with a `_meta.json` naming the
applied entry.

Two constraints shape the command:

- The app loads this config at startup and rewrites parts of it on exit, so it must be
  fully stopped before anything is written. `desktop` quits it gracefully, waits, writes,
  then relaunches. If it will not quit, the command refuses rather than writing something
  about to be overwritten.
- One backup is taken before the first modification and never overwritten. `--revert`
  restores it, and `purge` restores it too — purge deletes the directory that backup lives
  in, so without that the app would be left pointing at a gateway that no longer exists.

## Environment parity with plain `claude`

`claude-unlimited code` must give a project exactly what plain `claude` gives it. It adds
only `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and the model labels, then `execvp`s the
same binary in the same directory. It never sets `CLAUDE_CONFIG_DIR` — that is used only
by the `add-account`/`reauth` subprocesses — so `~/.claude`, `CLAUDE.md`, skills, agents
and session history are the user's real ones.

One unavoidable difference: setting `ANTHROPIC_AUTH_TOKEN` makes Claude Code treat this as
a custom auth source, which disables claude.ai-hosted connectors. Locally-configured MCP
servers are unaffected. This is inherent to routing through a custom base URL — the proxy
has to authenticate its callers, or any local process could spend the pool.

## OS support

Three backends exist behind each interface. macOS is the only one verified on real
hardware; see [ADR 0005](adr/0005-windows-linux-backends-unverified-first-cut.md) for what
that means and [`CONTRIBUTING.md`](../CONTRIBUTING.md#os-support-status) for what to check
if you are the first to run it elsewhere.

| | macOS | Linux | Windows |
|---|---|---|---|
| Credentials | Keychain ✅ | Secret Service | DPAPI |
| Auto-start | launchd ✅ | systemd --user | Task Scheduler |
| Notifications | osascript ✅ | notify-send | PowerShell toast |

## Not built yet

- **Signed releases.** The updater proves a download matches the commit GitHub names for
  its tag, which shows it came from this repository's history. It does not prove
  authorship; a detached signature could layer on top.
- **Real-hardware verification of Linux and Windows.** The code exists and is unit tested.

## Testing

```bash
python3 -m pytest tests/
```

Tests never reach the network, the real keychain, `~/.claude`, or the real `claude`
binary — anything that talks to a provider is injected, so the suite runs offline and
cannot spend anyone's quota.
