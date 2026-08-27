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
- `session_tokens.py` — the per-session credential behind `code --profile`.

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

## Usage tracking

`usage_tracking.py` is a **strict tee**. Every chunk read from upstream is yielded onward
unmodified and in order; a separate copy is parsed for token counts. Every parse is
guarded, so a malformed body costs a usage record and never the response.

Recording happens once the body is fully forwarded, on the committed (non-retry) path
only. A client that disconnects mid-stream simply gets no usage record.

## Model parity (codex only)

Claude Code asks for a Claude model; something has to choose which GPT model answers and
how hard it reasons. `openai_models.py` owns that mapping and Settings → Models parity
edits it. A Profile with its own override ignores the table.

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
