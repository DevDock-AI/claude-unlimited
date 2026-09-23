<div align="center">

# Claude Unlimited

### When one account hits its usage limit, your session doesn't.

Claude Unlimited pools your Claude (Pro · Max), ChatGPT/Codex, and Anthropic API accounts
into one continuous supply for Claude Code. When an account runs dry, the next one takes
over on the very next request — same session, same context, same terminal. You keep typing.

<br>

<table>
<tr>
<td align="center" width="150"><img src="docs/logos/claude.png" height="14" alt=""><br><b>Claude</b><br><sub>Pro · Max</sub></td>
<td align="center" width="150"><img src="docs/logos/openai.png" height="14" alt=""><br><b>ChatGPT</b><br><sub>Codex · Plus · Pro</sub></td>
<td align="center" width="150">🔑<br><b>API keys</b><br><sub>Anthropic · gateways</sub></td>
<td align="center" width="60"><b>→</b></td>
<td align="center" width="260"><b>one Claude Code session</b><br><sub>that never stops</sub></td>
</tr>
</table>

### **You never notice the switch.**

It happens **between requests**, in the background. No logout, no handover, no restarting
the session — the account underneath you changes and nothing else does. Often the
dashboard's activity log is the only place you'll find out it happened.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/DevDock-AI/claude-unlimited/actions/workflows/ci.yml/badge.svg)](https://github.com/DevDock-AI/claude-unlimited/actions/workflows/ci.yml)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Backend dependencies](https://img.shields.io/badge/backend%20dependencies-1-brightgreen)
![Tests](https://img.shields.io/badge/tests-815-brightgreen)
![100% local](https://img.shields.io/badge/100%25-local-8A63D2)
[![Support](https://img.shields.io/badge/support-ko--fi-ff5e5b)](https://ko-fi.com/devdock)

<br>

<img src="docs/screenshots/overview.png" alt="Claude Unlimited dashboard">

</div>

---

## Contents

| | |
|---|---|
| **[Why Claude Unlimited](#why-claude-unlimited)** | The problem it removes |
| **[Features](#features)** | What you get |
| **[How it works](#how-it-works)** | The flow, and why nothing leaves your machine |
| **[Install](#install)** | One line |
| **[<img src="docs/logos/claude.png" height="14" alt=""> Add a Claude subscription](#add-a-claude-subscription)** | One command |
| **[<img src="docs/logos/openai.png" height="14" alt=""> Add a ChatGPT / Codex subscription](#add-a-chatgpt--codex-subscription)** | One command |
| **[Add an API key](#add-an-api-key)** | Dashboard only |
| **[Models parity](#which-gpt-model-runs-your-claude-model)** | Which GPT model runs each Claude model |
| **[Usage](#usage)** | Daily driving |
| **[The Claude desktop app](#using-the-claude-desktop-app)** | Route the app through your pool too |
| **[Your own app](#calling-the-daemon-from-your-own-app)** | Anything that speaks the Messages API |
| **[The dashboard](#the-dashboard)** | What you can see and control |
| **[ECO](#eco--efficient-context-optimization)** | Fewer tokens for the same work |
| **[HUD - Heads-Up Display](#hud---heads-up-display-macos)** | Your pool at a glance, on macOS |
| **[Notifications](#notifications)** | Know before you run out |
| **[Updates](#updates)** | How new versions reach you |
| **[Command reference](#command-reference)** | Every command |
| **[AGENTS.md](AGENTS.md)** | Setting this up with a coding agent |
| **[Troubleshooting](#troubleshooting)** · **[Security](#security)** · **[Contributing](#contributing)** | The rest |

---

## Why Claude Unlimited

You're deep in a Claude Code session. Context is loaded, the plan is working — and you hit
your usage limit.

The manual workaround is grim: write yourself a handover note, log out, log into another
account, paste the note back, and hope nothing important got lost.

**Claude Unlimited removes that moment entirely.** When one account approaches its limit,
the next one takes over on the very next request. Same session, same context, same
terminal. You don't get logged out, you don't get prompted, you don't restart anything.
Often the first you'll know about it is a line in the activity log.

It pools whatever you've got — Claude Pro/Max subscriptions, ChatGPT/Codex subscriptions,
Anthropic API keys, your own gateway — and treats them as one continuous supply.

> Other multi-account tools automate the swap too, but they're CLI-only. There's nowhere
> to *see* what's happening. Claude Unlimited's control surface is a real dashboard.

---

## Features

|  | |
|---|---|
| 🔄 | **Seamless rotation** — the handover happens between requests. Your session never breaks, never prompts, never restarts. |
| <img src="docs/logos/claude.png" height="14" alt=""><img src="docs/logos/openai.png" height="14" alt=""> | **Mixed pools** — Claude subscriptions, ChatGPT/Codex, and API keys side by side, each with its own priority and threshold. |
| <img src="docs/logos/openai.png" height="14" alt=""> | **Claude Code, powered by GPT** — a Codex account is translated to and from the Anthropic API shape, with real token-level streaming. Claude Code can't tell. |
| 🎚️ | **[Models parity](#which-gpt-model-runs-your-claude-model)** — you decide which GPT model and reasoning effort each Claude model maps to, and what it costs your Codex quota. |
| 🧠 | **[The 1M context window, through the pool](#the-1m-context-window)** — Claude Code normally drops to 200K behind a proxy; `cu code` gets the million back, and a ChatGPT account in the pool is guarded turn by turn instead of costing you the window. |
| ⚖️ | **[Balanced sessions](#balancing-sessions-and-subagents-across-accounts)** — `cu code --distribute` spreads the main agent and each subagent across accounts, each staying put so its prompt cache stays warm. |
| ⏳ | **[Per-model limits](#when-fable-runs-out-but-the-account-hasnt)** — when a plan's Fable week runs out before the account does, the session can move on instead of failing every Fable request. |
| 🖥️ | **[The Claude desktop app too](#using-the-claude-desktop-app)** — not just the terminal. One command points it at your pool, and `--revert` puts it back. |
| 📊 | **A dashboard you'll actually open** — live usage bars, cost tracking, model split, per-project attribution, activity log. |
| 📈 | **[Statistics](#the-dashboard)** — spend and tokens over time as line charts, broken down by model, project and account, plus what each request *asked for* versus what served it. |
| 🌿 | **[ECO](#eco--efficient-context-optimization)** — shortens what tools printed before it is sent, so the same work costs fewer tokens. Off by default; only tool output is ever rewritten. |
| 🎛️ | **[HUD - Heads-Up Display](#hud---heads-up-display-macos)** — a floating macOS dock showing every account's usage at a glance, always on top if you want it. |
| 🔒 | **100% local** — no account, no telemetry, no cloud. Your credentials never leave your machine. |
| ⚙️ | **One command to work** — `claude-unlimited code` and you're routed. |
| 🔔 | **Notifications** — told before you run out, not after. |
| 🔑 | **OS-native credential storage** — Keychain / Secret Service / DPAPI. Never plaintext on disk. |
| 📦 | **Encrypted export/import** — move your whole setup to another machine safely. |
| 🌍 | **4 languages** — English, Spanish, Romanian, German. |
| 🪶 | **Almost no dependencies** — Python standard library, one library for export encryption, zero frontend build. |

---

## How it works

```
   claude  (or any Anthropic-API client)
      │
      │   ANTHROPIC_BASE_URL=http://127.0.0.1:4317
      ▼
╔═══════════════════════════════════════════════╗
║   Claude Unlimited daemon — YOUR MACHINE      ║   127.0.0.1 only.
║                                               ║   Not reachable from
║   ┌─────────────────────────────────────┐     ║   your network, let
║   │  Router — picks the account         │     ║   alone the internet.
║   │  sticky until it hits its threshold │     ║
║   └──────────────────┬──────────────────┘     ║
║   ┌──────────────────▼──────────────────┐     ║
║   │  Gateway — swaps in the real        │     ║
║   │  credential for this one request    │     ║
║   └──────────────────┬──────────────────┘     ║
║   ┌──────────────────▼──────────────────┐     ║
║   │  Dashboard  ·  127.0.0.1:4317       │     ║
║   └─────────────────────────────────────┘     ║
╚═══════════════════┬═══════════════════════════╝
                    │  only the request you already meant to make
                    ▼
     api.anthropic.com   ·   ChatGPT/Codex   ·   your own gateway
```

<table>
<tr><td>

**The rotation is invisible to Claude Code.** It sends one request, gets one answer. It is
never told which account served it, never asked to re-authenticate, and never sees a
different endpoint. <img src="docs/logos/claude.png" height="14" alt=""> Claude, <img src="docs/logos/openai.png" height="14" alt=""> ChatGPT/Codex and 🔑 API keys all
arrive as the same Anthropic-shaped response — which is exactly why your session can carry
on through a switch without noticing one happened.

</td></tr>
</table>

### Nothing leaves your computer

This is the part worth being explicit about:

- **No account. No sign-up. No cloud component.** There is no Claude Unlimited server. It
  cannot phone home, because there is no home.
- **No telemetry, ever.** Usage stats, cost estimates, the activity log, project
  attribution — all of it is written to files in your home directory and read by a
  dashboard served from `127.0.0.1`. None of it is transmitted anywhere.
- **Your credentials never move.** They live in your OS keystore (macOS Keychain, Linux
  Secret Service, Windows DPAPI), never in plaintext on disk, and are attached to a
  request only at the moment it goes to **the provider you configured** — Anthropic,
  OpenAI, or your own gateway.
- **`claude` never sees your real credentials either.** It authenticates to the daemon
  with a local placeholder token. The real credential is substituted server-side and never
  returned to the client.
- **Almost the only outbound traffic is the API call you already intended to make** —
  same destination as without this tool. No quota probing, no keep-warm pings, no
  background chatter. The two exceptions are both ones you can name: a once-a-day
  version check against GitHub's public API, and refreshing an account's own OAuth
  token shortly before it expires, so an account you haven't used in a while doesn't
  quietly need a re-login.

---

## Install

Pick your system, copy the **one line**, paste, done. It installs everything it needs
(even Python, if you don't have it), starts in the background, and opens the dashboard at
**http://127.0.0.1:4317/** — where you add your first account.

**🍎 macOS & 🐧 Linux** — paste into a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/DevDock-AI/claude-unlimited/main/install.sh | bash
```

**🪟 Windows** — press **Win + R** (or open **Command Prompt**), paste, press Enter:

```bat
powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://raw.githubusercontent.com/DevDock-AI/claude-unlimited/main/install.ps1 | iex"
```

**🪟 Windows, already in PowerShell?** — paste this shorter version instead:

```powershell
irm https://raw.githubusercontent.com/DevDock-AI/claude-unlimited/main/install.ps1 | iex
```

Then run `claude-unlimited code` and you're routed. On Windows, run the installer from an
**Administrator** PowerShell if you also want it to start automatically at logon;
credentials are stored with Windows DPAPI instead of the Keychain.

> **Which platform is this best on?** Claude Unlimited is developed and tested on
> **macOS**, so that's the most battle-hardened path. **Windows and Linux are best-effort:**
> every OS-specific piece (credential storage, the background service, the installer) is
> implemented and covered by tests, and it *should* work fine — but it hasn't had the same
> real-world mileage, so treat it as "should be fine, not guaranteed." If anything's off,
> please [open an issue](https://github.com/DevDock-AI/claude-unlimited/issues).

Don't want it starting on login? Turn it off in **Settings → Daemon**, or run
`claude-unlimited uninstall`. It keeps running either way until you stop it.

> **Using an AI agent to set this up?** Point it at
> [`AGENTS.md`](AGENTS.md) — it spells out the install, what's safe, and the
> one step an agent must hand back to you (the browser login).

<details>
<summary>What that command does, and how to install from a clone instead</summary>

<br>

The installer creates an isolated environment at `~/.local/share/claude-unlimited/`,
links `claude-unlimited` into `~/.local/bin`, runs `claude-unlimited doctor`, and
registers the daemon as a login service (`launchd` / `systemd --user` / Task Scheduler).
It touches nothing else, and tells you if `~/.local/bin` isn't on your `PATH`.

If the service can't be registered it falls back to running for this session only, and
says so rather than pretending it succeeded.

Prefer to read it first, or install from a checkout?

```bash
git clone https://github.com/DevDock-AI/claude-unlimited.git
cd claude-unlimited
./install.sh
```

Requires **Python 3.10+**, **git**, and the **`claude`** CLI on your `PATH`.

</details>

---

## Add a Claude subscription

<img src="docs/logos/claude.png" height="14" alt=""> **Claude Pro or Max**

```bash
claude-unlimited add-account
```

Opens your browser, logs into the account, and adds it. That's it — no account IDs to find
or paste.

It logs in through an **isolated session**, so whatever is already signed into `claude` on
your machine is never touched. Run it again for each additional account.

---

## Add a ChatGPT / Codex subscription

<img src="docs/logos/openai.png" height="14" alt=""> **ChatGPT Plus, Pro, or Business — used through Codex**

```bash
claude-unlimited add-codex-account
```

Same flow, for a ChatGPT/Codex account. Once added, it joins the same rotation — Claude
Code keeps speaking the Anthropic API, and requests routed to this account are translated
to and from OpenAI's transparently, streaming included.

Also isolated: your existing `codex` login is left alone.

### Purchased credits

A ChatGPT account can hold purchased credits on top of its plan. Claude Unlimited reads the
balance from the account's own responses and shows it on the profile card, so an account
sitting idle at 100% while you have credits on it is no longer a mystery.

By default those credits are **not** spent: when the plan window runs out the account is
rotated away from and comes back when the window resets, exactly as before. Your plan window
is already paid for; credits are charged per request, and a pool that started spending them
on its own would be a bad surprise. **Settings → ChatGPT credits** turns it
on. While it is on, an account running on credits is marked **On credits** with its balance
on the card, in the table and in the HUD, so you can see what is costing money.

The setting appears only if you have a ChatGPT account in the pool, and affects no Claude
account.

### Which GPT model runs your Claude model

Claude Code asks for a Claude model. When the request lands on a Codex account, something
has to decide which GPT model actually answers — and how hard it thinks. That's the
**parity list** in **Settings → Models parity**, and it's yours to edit:

| Claude model | Claude effort | Runs as (Codex) | Codex effort |
|---|---|---|---|
| Claude Fable 5.1 | Automatic | `gpt-5.6-sol` | medium |
| Claude Opus 5.5 | Automatic | `gpt-5.6-terra` | high |
| Claude Sonnet 5 | Automatic | `gpt-5.6-terra` | medium |
| Claude Haiku 4.5 | Automatic | `gpt-5.6-luna` | low |

- **It's an editable list.** It starts with those four. Add a row with **+ Add model**,
  remove one with the red 🗑, and **Save**. **Reset to defaults** brings the four back.
- **The list is your model picker.** Whatever you save is exactly what `/model` offers in a
  Codex-served `cu code` session — nothing more, nothing less. Restart the session to pick
  up changes (the picker is read once at launch).
- **Both sides have an effort dial.** *Codex effort* governs a request served by a Codex
  account; *Claude effort* is applied when a Claude account serves it (`output_config.effort`
  — leave it **Automatic** to keep Claude Code's own choice). Haiku-class and older Sonnet
  models don't accept it, so it's simply ignored there.
- **Nothing maps to `gpt-6-astra` out of the box.** Claude Code picks Fable on its own for
  ordinary work, so making Fable the flagship's row meant spending top-tier Codex quota you
  never asked for. It's still in the dropdown — one edit away — just not a default.
- **Dropdowns list the priciest/most-capable first**, drawn live from the model catalogue.
- **Names and prices are live.** The lineup comes from the model catalogue refreshed at each
  session launch, so `gpt-6-astra` / Fable 5.1 show up without a release.

**Effort is the expensive dial, not model size.** A Codex plan's quota is spent on
reasoning tokens produced, multiplied by the model's tier. Turning a row up to `high` costs
noticeably more of your weekly allowance than leaving it at `medium`, and the cheapest
setting of all is **`none`**, which turns reasoning off: the model answers straight away and
produces zero reasoning tokens. Good for mechanical work, worse for anything that needs
thinking through. That's measured behaviour, not a guess — see
[`docs/adr/0007-codex-quota-is-driven-by-reasoning-not-context.md`](docs/adr/0007-codex-quota-is-driven-by-reasoning-not-context.md).

The list applies to Codex profiles left on **Automatic**. A profile with its own model
override keeps using that instead, so you can pin one account to a specific model and let
the rest follow the list.

> **Picker limitation:** Claude Code's `/model` picker can relabel at most its four standard
> tiers (Fable, Opus, Sonnet, Haiku). Extra rows you add are still served and usable by id
> (`/model <id>` or `--model <id>`), but may not appear in the picker itself.

---

## Add an API key

🔑 **Anthropic API keys and Anthropic-compatible gateways**

API keys are added **in the dashboard**, not the CLI — open
**http://127.0.0.1:4317/** → **Add profile**.

That's deliberate. Anything with a form (base URL, auth mode, default model, force model,
budget cap, token threshold) belongs somewhere you can see and edit it, not behind flags you
have to remember. The same goes for everything else about an account: priority, thresholds,
enabling, disabling, and removal are all dashboard-managed.

Works with Anthropic API keys and any Anthropic-compatible gateway — including a **local
model server** that speaks the Anthropic Messages API (an MLX server, LM Studio, and others):
the Base URL may be `http://` when it points at your own machine or LAN (`localhost`,
`127.0.0.1`, `192.168.x.x`, `10.x.x.x`); anything else must be `https://`. This applies to
API-key profiles only — a Codex profile's Base URL is always `https://`.

Two optional model fields decide what the endpoint is asked for:

- **Force model** — every request to this profile uses exactly this model, whatever Claude
  Code asked for. This is how you say "always Qwen3-Coder" for a local server.
- **Default model** — used when the endpoint refuses the model a request asked for. With
  Force model also set, it is the fallback for when the **forced** model is refused.

If both are filled, the forced model is always sent first; the default is used only if the
endpoint refuses the forced one. A refusal is remembered for that profile, so later requests
go straight to the model that works. A "prompt is too long" error is never mistaken for a
refused model.

---

## Usage

### Start working

```bash
claude-unlimited code
```

Starts the daemon if it isn't running and launches `claude` routed through your pool. With
more than one account it asks which to use — or pick **Rotated accounts** to let it manage
itself.

> **Tip:** `cu` is a shorthand for `claude-unlimited` — every command works under both names
> (`cu code`, `cu status`, `cu doctor`, …). An existing install gains `cu` after an update.

```bash
claude-unlimited code --profile "Personal Max"   # pin this session to one account
claude-unlimited code --distribute               # balance agents across accounts
claude-unlimited code --model opus               # any extra args pass through to claude
```

The dashboard URL stays in Claude Code's status line while you work.

### What rotation actually does

1. Requests go to the enabled account with the **lowest priority number**.
2. It stays on that account — *sticky* — until it crosses its **switch threshold**
   (default 98%) or hits a real quota limit.
3. The next account takes over **on the next request**. Your session continues
   uninterrupted; nothing is lost and nothing is restarted.
4. When a quota window resets, that account rejoins the rotation automatically.

A brief rate-limit blip never causes a switch — only a real threshold crossing or genuine
exhaustion does.

**Coming back.** Once it has switched, the pool stays on the new account for as long as that
account works — even after your first-choice one refills. That is on purpose: moving back
throws away a warm prompt cache, and with session pinning it would move agents that are
mid-task. If you would rather it came back, **Settings → Session routing → "Go back to your
first-choice account when it's free"** does exactly that, and only while the pool has been
idle for ten minutes, so no session is ever moved out from under you. **Take over** still
wins over it, and a pinned branch keeps its own account until that account stops serving it.

### Keeping usage numbers fresh

Usage normally arrives with real responses, so an account nobody has used lately would show
a stale number or *not yet observed*. While you're active — sending requests through Claude
Unlimited or using the dashboard — each Claude and ChatGPT-subscription account's usage is
read every **5–10 minutes** from the provider's own read-only usage endpoint (the one Claude
Code's `/usage` and the Codex CLI read). No messages are sent.

- **Pauses after 30 minutes idle**, and resumes the moment you're back.
- **Skips an account that real traffic just refreshed.**
- **Backs off hard.** A rate limit waits at least 15 minutes (or the provider's
  `Retry-After`), doubling up to 6 hours, and pauses that whole provider. A refused
  credential waits hours and never marks the account broken. Backoff survives restarts.
- API-key profiles are skipped — they have no subscription windows to read.
- **Per-model limits.** A Claude account can have a weekly limit for one model on top of its
  overall windows — Fable, for example, which can run out while the account still looks
  healthy. The same read picks these up; they show as an extra bar (*Fable weekly*) on the
  profile card and in its details, beside the account name in the Profiles table, and as a
  third meter in the widget's detail card — and a profile can be set to **leave when its
  Fable week is spent** (see below).

Turn it off in **Settings → Daemon → Keep usage up to date**.

### When Fable runs out but the account hasn't

A plan can put a single model on its own weekly clock. You can burn through your Fable week
while the account's 5h and overall weekly windows still look healthy — and then every Fable
request fails on an account the pool has every reason to think is fine.

Each profile has a switch for it: **"Leave this profile when its Fable limit is spent"**, in
the profile's details (and in the Add-profile form). **Off by default.**

- **Off**: the account keeps serving exactly as it always has. Fable requests get the
  provider's own limit error; every other model keeps working on that account.
- **On**: the moment the account's Fable weekly limit is spent, the **whole session moves** to
  another account — every model, not just Fable requests — and rotation skips the account
  until the limit resets or a fresh usage read shows room. Nothing about the account's own
  state changes: it is not marked exhausted, because it isn't.

**Settings → Session routing → "Switch every profile when its Fable limit is spent"** turns
the switch on for every profile at once (off by default). While it is off, each profile's own
switch decides.

Whichever way it is set:

- A **pinned session** (`cu code --profile`) and a standing **Take over** are honoured: you
  named an account, so you get it, and the provider's own limit error with it. One line in
  Activity says why.
- A **pinned branch** moves for good rather than being diverted once, so its prompt cache
  follows the work — and only once another account can actually take it.
- If **no** other account can take the session, it stays where it is and you get the
  provider's real error — we don't invent a local one.

**ChatGPT accounts are covered too**, with the difference the two providers force on us.
Anthropic tells us a *percentage* per model ("Fable, 43% of your week"), so a Claude account
counts as spent when that crosses its switch threshold. OpenAI only tells us *whether* a model
is usable right now — there is no percentage anywhere in what it reports — so a ChatGPT
account counts as spent when the GPT model your Fable maps to is reported unavailable, and
comes back when the provider says it has returned. Same behaviour, coarser signal, because that
is all the provider gives.

API-key profiles report no per-model limits at all; the switch is hidden for them and they
route exactly as they always have.

### The 1M context window

Several Claude models hold a million tokens: **Sonnet 5**, **Opus 4.7 / 4.8 / 5 / 5.5**, **Fable 5
and 5.1**, **Mythos 5 and 5.1**. Claude Code only uses that window when it is talking
straight to `api.anthropic.com`. Pointed anywhere else — including this daemon on your own
machine — it falls back to **200K**, because it has no way to tell what is on the other end
of that address.

For a Claude or Anthropic-API account that caution is unnecessary: the daemon relays to
Anthropic with your own credential, so the full window really is there. Claude Unlimited says
so on your behalf, and you get the million back:

| Model | `cu code`, before | `cu code`, now |
|---|---|---|
| Opus 5 | 200,000 | **1,000,000** |
| Sonnet 5 | 200,000 | **1,000,000** |

**A ChatGPT/Codex account in the pool no longer costs you the window.** A ChatGPT account
answers with a GPT model whose own window is about **272K** tokens, so the daemon sizes every
turn before routing it: a ChatGPT account takes the turns that fit its window (about 226K
estimated input tokens, leaving room for the reply and its reasoning), and longer turns go to
a Claude account. When no account that may take the request can hold it — say the Claude
accounts are out of capacity and a 400K conversation arrives — the daemon answers exactly the
"prompt is too long" error Claude Code reacts to, and Claude Code compacts and carries on.
The one visible sign of this in a mixed pool is a compaction that comes earlier than it would
on Claude accounts alone. The budget is 272K rather than the backend's 872K ceiling because
past 272K a ChatGPT subscription is charged at a higher usage rate for the whole request, and
this daemon does not spend that silently.

The rules the guard follows: an explicit choice — `cu code --profile <ChatGPT account>` or
**Take over** — is honoured and never substituted, but an oversized turn gets that same
"prompt is too long" instead of being sent to fail. A branch pinned to a ChatGPT account whose
conversation outgrew it moves to a Claude account and stays there. A profile marked "Forced in
subagents" falls back to normal balancing for a subagent whose conversation it cannot hold. A
GPT model this build has not met is assumed at 272K and said so, in the Activity log and in
Settings — never excluded on a guess.

**Auto is the cautious alternative** — it stays quiet when it cannot be sure: when only ChatGPT/Codex accounts can take the
session (nothing there holds more than 272K, so 1M would only mean compacting sooner), when an
API profile points somewhere other than Anthropic, or when your Claude Code version has not
been checked for it. The launch prints why, and Settings shows the same verdict together with
each ChatGPT account's window.

**Settings → 1M context window** has four choices: **Force 1M** (the default), **Auto**,
**Claude Code default** and **Prefer 200K**. Force 1M sets the window on every
session whatever the route — a ChatGPT-only pool, a pinned ChatGPT account, an API profile
pointing elsewhere, even an unverified Claude Code version. The capacity guard stays on under
it, it cannot enlarge a model that is not natively 1M (Sonnet 4.6 and Haiku 4.5 stay at 200K),
and a backend you pointed it at may reject what the client now sends — a local model server
with a small window, for instance, answers "prompt too long" once a conversation outgrows it.
Pick **Auto** if your pool routes a lot of traffic to backends smaller than 1M. Your own
`CLAUDE_CODE_DISABLE_1M_CONTEXT` always wins over all four.

A bigger window is not a bigger quota. It does not buy you more usage — it means a long
session compacts far less often, which is usually what you actually wanted.

### Balancing sessions and subagents across accounts

A Claude Code session isn't one caller: the main agent and every subagent it spawns send
their own requests, and Claude Code labels each one. Claude Unlimited can route them
separately.

**`code --distribute`** balances the session across your pool. Each new agent — the main
agent when the session starts, and each subagent when it's spawned — goes to the account
with the **fewest agents already working on it**, counted across all your sessions (ties go
to your priority order, then to the least-used account). It then *stays* there, so it keeps
its own prompt cache warm instead of paying a cache miss on every rotation.

> **Balancing happens only when an agent starts — never mid-session.** An agent moves only
> if its account reaches its switch threshold or a usage limit, becomes unavailable (turned
> off, cooling down, needs re-auth), or the agent sits idle for an hour. It balances by how
> many agents each account is serving, not by usage percentage, so usage evens out roughly
> over many sessions rather than exactly.

While agents are spread out, each profile on the dashboard shows how many it is serving
(**Agents: N**), and every agent moved off an unavailable account is logged in Activity.

**Settings → Session routing → "Balance sessions and subagents across accounts"** makes that
the default for every session, with no flag to remember. **Off by default**, because it changes how your
accounts are consumed. The flag still works while it's off, and turning it on never
overrides a `--profile` pin.

**Forced in subagents** is a per-account switch — on the Profiles list, in each profile's
⋮ menu or its edit modal. Turn it on for one account and every subagent, in every session,
goes to it — while the main agent keeps rotating normally. That's how you run, say, a
Claude model as the orchestrator with all its subagents on a Codex/GPT account, or on a
local model. Only one account can hold it at a time, and if that account is exhausted or
disabled, subagents fall back to spreading across the pool rather than failing.

It also works **inside a pinned session**: `cu code --profile "Second account"` keeps the main
agent on that account, and its subagents go to the account marked Forced in subagents. There,
both are held strictly — nothing is rerouted; if the subagents' account cannot serve, their
requests fail instead of moving. A *disabled* holder counts as none: the subagents then
follow the pin, like the main agent. If the account holding it is
disabled, turning it on elsewhere simply moves it; an enabled holder has to be switched off
first.

Which one wins, in order:

| How | What happens |
|---|---|
| `cu code --profile <name>` | That terminal's main agent uses that account, never rotated. Its subagents do too — unless a profile is marked "Force in subagents", in which case they go there, just as strictly. |
| "Force in subagents" on a profile | Every subagent goes there; the main agent rotates normally (or stays pinned, with `--profile`). |
| `cu code --distribute` | Main agent and each subagent start on the least-busy account and stay there, that session. |
| Settings → "Balance sessions and subagents across accounts" | Same as above, for every session, no flag needed. |
| *nothing set (default)* | Normal rotation — everyone shares one account until it's spent. |

### Using the Claude desktop app

The desktop app can send its inference through your pool too, not just the
terminal:

```bash
claude-unlimited desktop
```

It creates an inference profile named **Claude Unlimited** in the app, points it
at the local daemon, makes it active, and starts the app. Any inference profiles
you already have are left alone.

> **This restarts the Claude desktop app.** If it is running, the command asks
> it to quit, waits for it to close, and reopens it afterwards. **Save anything
> you are working on first.** The app rewrites its own settings as it exits, so
> it has to be fully stopped before its configuration can be changed — a
> half-quit app would overwrite the new settings on its way out. If it will not
> quit (a dialog, unsaved work), the command stops and changes nothing rather
> than writing settings that are about to be clobbered.

**Undoing it.** Everything the command changes is backed up before the first
write, and:

```bash
claude-unlimited desktop --revert
```

restores it. Quit the app before reverting too, for the same reason. The
inference profile named *Claude Unlimited* stays in the app's list — remove it
there if you no longer want it.

Three things worth knowing:

- The app calls this **third-party inference mode**, and it runs from a separate
  app profile with its own session — so you may be asked to sign in again. Your
  normal Claude setup is unaffected.
- The app's **own chat still talks to claude.ai** and is not routed through the
  pool. Only Claude Code sessions inside the app are.
- **Enable at least one account first.** Otherwise the app's "Test connection"
  fails with *No eligible Profile available*, which looks like a configuration
  error but isn't.

This is the only command that writes outside `~/.claude-unlimited/` and
`~/.local/share/claude-unlimited/`; it touches
`~/Library/Application Support/Claude*`, and only when you run it. macOS only
for now. The Help page in the dashboard also documents the manual route, if you
would rather set it up in the app yourself.

### Your project setup is untouched

`claude-unlimited code` runs the same `claude` binary in the same directory. Your
`CLAUDE.md`, `.claude/` settings, skills, subagents, memory, and session history all
behave exactly as they normally do. The only thing that changes is which account serves
the request.

Your project's `.claude/settings.json` is used as-is — permissions, allow lists, hooks,
everything. The one exception is its `env` block: if it pins `ANTHROPIC_BASE_URL` or
`ANTHROPIC_AUTH_TOKEN`, Claude Code applies that **on top of** the routing set up for you,
and every request would go to whatever that file names, with whatever credential it
carries, never reaching your pool. Those two keys are reasserted for the session, and it
says so when it happens. Nothing else in `env` is touched, and no file is modified.

> One other difference: because the daemon authenticates you with its own local token,
> claude.ai-hosted connectors are disabled for that session. Locally-configured MCP
> servers are unaffected.

### Calling the daemon from your own app

Anything that speaks the Anthropic Messages API can use the pool: point its base URL at
`http://127.0.0.1:4317` and give it the local token from **Settings → Network** as the API
key. No SDK change, no wrapper.

One thing to add, though. Claude Code labels every request with the session and agent it
came from, and that label is what keeps a conversation on one account. Your app sends no
such label, so by default each request is routed on its own — and a batch of requests can
land on several accounts, each one paying a fresh cache miss. Send a stable id instead:

```http
x-claude-unlimited-session: cv-screening-worker-1
```

Every request carrying that id is treated as one session: it stays on the account it
started on for as long as that account can serve it, keeping its prompt cache warm, and
rotates only when the account hits its threshold, runs out, or becomes unavailable — the
same rules as a Claude Code session. Give each worker, conversation or job its own id and
they spread across your pool instead of piling onto one account.

**Take over** — from the dashboard or the HUD — still wins over all of it, and holds every
request on the account you picked until you release it.

**Knowing when the pool is free.** To hold a batch job back until nobody is working, read
`GET /api/status`:

```json
{ "idle_seconds": 412.5, "serving_now": [] }
```

`idle_seconds` is how long since any account last served a request — `null` when the daemon
has served none since it started, and `0` while a request is open. `serving_now` lists the
accounts serving one at this instant. Don't use `in_use_now` from `/api/profiles` for this:
it deliberately stays true for 15 minutes after the last request so the dashboard's
"Used now" light doesn't flicker between polls, which reads as a false busy.

---

## The dashboard

**http://127.0.0.1:4317/** — everything is managed here.

<div align="center">
<img src="docs/screenshots/profiles.png" alt="Profiles — search, filter, drag to reorder priority, edit thresholds inline">
<br><em>Profiles — drag to reorder priority, edit thresholds inline, enable or disable in one click</em>
</div>

<br>

- **Overview** — account cards (roomy list or compact cells), live usage and cost, model
  breakdown, per-project attribution, and recent activity.
- **Profiles** — the full table: search, filter, sort, drag to reorder priority, and a
  menu to edit, test, disable, or remove.
- **Statistics** — cost and tokens over time, by model, project and account, over ranges
  from today to all time. Also shows which model each request asked for against the one
  that actually served it, an activity grid of calls per day, and what ECO has saved.
- **Activity** — every rotation, session, and config change, filterable and exportable.
- **Settings** — updates, [models parity](#which-gpt-model-runs-your-claude-model),
  auto-start, process controls, notifications, export/import, language.
- **Help** — every CLI command, explained.

Numbers update live, without refreshing. Usage bars shift amber then red as an account
nears its threshold, so you see it coming.

Every view has its own URL — `/profiles`, `/stats`, `/activity`, `/settings`, `/help` — so you can
bookmark one, reload without losing your place, and use the browser's back button.

<details>
<summary>More screenshots — statistics, activity log, settings, help, light theme</summary>
<br>
<img src="docs/screenshots/statistics.png" alt="Statistics — spend over time, by model, project and account">
<br><br>
<img src="docs/screenshots/activity.png" alt="Activity log">
<br><br>
<img src="docs/screenshots/settings.png" alt="Settings">
<br><br>
<img src="docs/screenshots/help.png" alt="Help — every CLI command">
<br><br>
<img src="docs/screenshots/overview-light.png" alt="Light theme">
</details>

---

## ECO — Efficient Context Optimization

Claude Code re-sends the whole conversation every turn, so a long tool output is paid for
again on every subsequent turn. ECO rewrites **what a tool printed** into a shorter form
before the request leaves the daemon, so the same work costs fewer input tokens.

**Off by default.** It changes what the model sees, so it has to be chosen, never inherited.
Turn it on in **Settings → ECO**.

| Mode | What it does |
|---|---|
| **Off** | Nothing is rewritten. The default. |
| **Light** | Collapses repetition and groups long lists. Every error is kept and every cut leaves a count behind. |
| **Aggressive** | Everything in Light, plus it discards detail the model cannot reconstruct. |

Every rewrite, by mode:

| Output | Mode | What ECO does |
|---|---|---|
| Repeated lines | Light | 40 identical lines become one line plus `… (39 duplicate lines, ECO)`. Logs over 2,000 lines are capped and the rest counted. JSON and source code are skipped — there, repeated lines are values. |
| Build and test logs | Light | Every error line kept in place; the first 5 warnings and 3 deprecation notices kept; `Compiling …` lines become `Compiled 84 packages`. |
| `git status` | Light | A count per group (staged, modified, untracked) with the first 10 paths of each. |
| Search results | Light | `path:line` matches grouped by file, first 10 per file, then `… +27 more`. |
| Very long output | Aggressive | 250+ lines: the first 120 and last 60 kept, `… +N lines truncated (ECO)` in between. |
| `git diff` | Aggressive | One summary line per file with its added/removed counts (`+42 -7`); each hunk keeps its first 100 lines and the rest is counted. |
| `git log` | Aggressive | Commit headers and messages kept; embedded diffs dropped. |
| `tree`, `ls`, `find` | Aggressive | `tree` capped at 200 lines; `ls -l` loses its permission/owner/size/date columns, with `node_modules`, `.git` and similar counted rather than listed; `find` grouped by folder, 10 names each. |

What ECO **never** touches: your messages, the model's replies, the system prompt, tool
*inputs*, any tool result marked as an error — an error trace is exactly what the model
needs in full — and any output under 500 characters. It runs before an account is chosen,
so it behaves the same for Claude, Codex and API profiles.

What it guarantees:

- **Deterministic** — the same output is rewritten the same way on every turn, so the prompt
  cache keeps hitting.
- **Never worse** — a rewrite that does not make the output shorter is discarded and the
  original is sent. Never empty.
- **Counted** — every cut says how much went (`… +27 more`, `(39 duplicate lines, ECO)`), and
  running ECO again over text it already shortened changes nothing.
- **Fails open** — if anything inside ECO fails, the original request is sent.

How savings are counted: the **bytes** removed are exact, recorded per request. **Tokens** are
estimated from what that same request was actually billed, and **cost** from that model's
input price, so Statistics marks both as approximate. Settings → ECO shows this same list.

### Primitive speech

ECO shrinks what goes **in**; primitive speech shrinks what comes **out**. Turn it on in
**Settings → ECO → Primitive speech** and the model answers you in fewer words, keeping every
technical fact. It works by adding one short instruction to each agent turn, plus a one-line reminder
on each message you type — the reminder is what makes the style hold against the agent's own
formatting habits. Nothing already written is reworded, and no reply is edited.

| Level | Style |
|---|---|
| **Off** | Normal replies. The default. |
| **Lite** | Full sentences, no filler, hedging or pleasantries. |
| **Full** | Fragments, no articles, no headings or bold: *"Bug in auth middleware. Expiry check uses < not <=. Fix:"* |
| **Ultra** | As short as stays unambiguous — usually a few plain lines. |

- **Always kept exactly:** code, commands, file paths, error messages, numbers, and words that
  flip meaning (*not*, *only*, *unless*). Security warnings and destructive or irreversible
  steps are written in full sentences. Replies stay in your language.
- **Written normally at every level:** files, code comments, commits, documentation, and pull
  request or issue text.
- **No narration** of the model's own reasoning ("Privately…", "Let me…").
- **Everywhere at once:** every session, subagent and account type, Codex included — nothing to
  install per agent. Helper calls (titles, summaries, token counting) are left alone.
- **Cache-safe:** the instruction is the last system block, after Claude Code's own, and each
  message gets the same reminder on every turn, so prompt caching keeps working; changing the
  level mid-session costs one uncached turn.
- **Measured:** each reply records the level it was written under, and **Statistics** shows
  output tokens per reply with and without it (a rough comparison — different requests).

Adapted from the [Caveman](https://github.com/juliusbrussee/caveman) skill by Julius Brussee
(MIT License). Not affiliated with it; "Caveman" is its author's trademark.

---

## HUD - Heads-Up Display (macOS)

A small floating dock that shows every account as a circle with its usage rings, so you can
see the state of your pool without opening the dashboard.

**On macOS you get it automatically.** Installing or updating Claude Unlimited installs
`HUD - Heads-Up Display.app` into `~/Applications`, registers it to start when you log in,
and opens it — nothing to build, nothing to run. It also appears in the menu bar, showing
the account currently serving and how close it is to rotating.

<p align="center">
<img src="docs/screenshots/hud-floating-vertical.png" height="300" alt="HUD floating, vertical">
<img src="docs/screenshots/hud-card-dark.png" height="300" alt="HUD detail card">
<img src="docs/screenshots/hud-docked-right.png" height="300" alt="HUD docked to the right edge">
</p>
<p align="center">
<img src="docs/screenshots/hud-docked-top.png" width="360" alt="HUD docked to the top edge">
<img src="docs/screenshots/hud-floating-horizontal.png" width="360" alt="HUD floating, horizontal">
</p>
<p align="center">
<img src="docs/screenshots/hud-card-light.png" height="300" alt="HUD light theme with a Codex account's card">
</p>

```bash
cu hud status     # is it installed, does it start at login, which version
cu hud install    # fetch and install it again, e.g. after deleting it
cu hud remove     # take it away (app and login item) and keep it away until `cu hud install`
```

The download is verified against a checksum shipped in the source tree before anything is
unpacked, so a bad or tampered file installs nothing and leaves the copy you have alone. If
the download fails — you updated while offline, say — the daemon tries again in the
background, a little less often each time, until it succeeds. It runs on macOS 13 or later,
on both Apple Silicon and Intel Macs.

Building it yourself from a checkout still works and is what you want while changing it:

```bash
cd macos-widget && ./build.sh
```

That builds and installs the bundle to `~/Applications` and launches it (and removes
the old `CapacityWidget.app`, the name it had before, keeping your saved position and settings).

- Drag it anywhere; **Always on top** keeps it above other windows. Drag it near a screen edge and it
  is **pulled onto the edge** and holds there as you keep dragging — slide it along the edge freely;
  pull it about 40 points away to let go. Release it while it is stuck and it **docks**: flush against the edge, its ends curving
  out into it like the display notch, vertical on the left or right, horizontal along the top or bottom, remembered across restarts and display
  changes. **Undock** in the cog menu floats it again.
- An account that is serving lights up from inside, in its provider's colour; several can be lit
  at once. Each account has **two rings**: the outer one is the 5-hour window — green, then amber,
  then red as it nears the switch threshold, the same bands as the Dashboard's bars — and the
  thinner inner one is the weekly window, blue until 90% and red from there. An account whose
  provider reports only one window gets a single ring.
- The number under each account names its window with a small subscript (`5h` or `w`). An
  account that needs re-authentication shows **Reauth** in red instead.
- The logo in the middle takes the colour you gave the account on the Profiles page.
- An **API-key** account has no plan windows, so its tile shows the **estimated total cost** so
  far instead (at list rates). If you set a token cap, the ring shows how much of it is used.
- A first launch starts it **always on top**, **Large**, with **Liquid Glass** where macOS has it
  (Dark otherwise). Anything you change in the cog menu is kept.
- It refreshes every 5 seconds (every 2 while the pointer is over it).
- Hover a profile for its detail card: plan, usage windows, reset times, today's spend, and a
  session line — while it is serving, how many agents are on it and for how long; otherwise when
  it was last used. Under that, the model and project of its latest request (a Codex account
  shows the Claude model it translated, e.g. `opus-5 → gpt-5.6-terra`).
- Right-click (or Control-click) a profile to **Take over**, to **Enable** or **Disable** it, or
  to choose whether its percentage shows the **5-hour** or the **weekly** window (**Percentage
  shows**). The choice is remembered per profile; a window the provider does not report is greyed
  out, and the detail card marks the meter the percentage is showing.
- The cog appears on hover: size, orientation, background and transparency. Background is
  **Dark**, **Light**, or — on macOS 26 and later — **Liquid Glass**, which follows the system
  appearance: light glass with dark text on a light desktop, dark glass on a dark one. With
  *Reduce transparency* turned on in System Settings, Liquid Glass falls back to the frosted
  background in whichever direction it resolved. On a light dock the usage colours, the
  provider marks and the percentages darken so they still read against white.
- Closed it? Reopen from the dashboard — the button directly above the language selector.

It is **read-only apart from "Take over" and Enable/Disable** — the same
`POST /api/profiles/<id>/take-over` and `PATCH /api/profiles/<id>` the Dashboard sends,
authorised with the per-run CSRF token the daemon serves on `/api/status`. It talks only to
`127.0.0.1:4317`, and is currently **macOS only and English only**.

---

## Notifications

Desktop notifications tell you what's happening without watching the dashboard:

| Notification | Tells you |
|---|---|
| **Approaching threshold** | An account is close to switching — before it does |
| **Rotated** | Which account just took over |
| **Quota reset** | An account is available again |
| **Needs attention** | Something needs you: re-auth, or no eligible account left |
| **Update available** | A new version has been released, downloaded, or installed |

Turn them on per category in **Settings → Notifications**, and use **Send test
notification** there to confirm they reach you.

> **Not seeing any?** Check two things. First, **Settings → Notifications** — each
> category has its own switch, and *Rotated* and *Quota reset* are **off by default**, so
> the two most frequent events are silent until you enable them. Second, your OS: on macOS
> your terminal needs permission under **System Settings → Notifications**, or they're
> delivered silently and you never see them.

---

## Updates

Claude Unlimited checks for new releases on its own and does exactly what you
tell it to in **Settings → Updates**:

| Mode | What happens when a release is found |
|---|---|
| **Fully manual** | You're told. Nothing is downloaded. |
| **Auto-download only** | Downloaded and verified, then waits for you to click install. |
| **Auto-download + install** | Downloaded, verified, and installed. Restart to finish. |

How a download is trusted, since this installs code on your machine:

1. The release source is **hardcoded** — nothing in your config can point the
   updater at a different repository.
2. Every request is HTTPS with certificate verification; a non-HTTPS redirect
   is refused.
3. The GitHub API names the commit a release's tag points at. The updater
   clones that tag and **refuses to install unless the commit it actually got
   is that same commit**. Git objects are content-addressed, so altered
   contents cannot produce the expected hash.
4. Your previous installation is kept. If the new version can't even be
   imported, it's **rolled back automatically** — a bad release leaves you on
   the version that worked.

This proves the code came from this repository's history as GitHub reports it.
It is not a signature check: it can't prove GitHub itself, or an account with
push access, is honest. That's a deliberate, documented limit.

## Command reference

Every command is daemon lifecycle or account authentication. **Everything else — accounts,
thresholds, priority, budget caps, export/import — lives in the dashboard.** The same list
is in the dashboard under **Help**.

Every command below also works under the short alias **`cu`** — e.g. `cu code`, `cu status`.

<details open>
<summary><b>Getting started</b></summary>

<br>

| Command | What it's for |
|---|---|
| `claude-unlimited doctor` | Checks your install, credential storage, and config. Run it first, and whenever something seems off. |
| `claude-unlimited add-account` | Adds a Claude subscription via browser login. Isolated — never disturbs your existing `claude` login. |
| `claude-unlimited add-codex-account` | Same, for a ChatGPT/Codex subscription. |
| `claude-unlimited reauth` | Re-authenticates an account that needs it. Logs back into the *same* account, never a new one. |

</details>

<details open>
<summary><b>Everyday use</b></summary>

<br>

| Command | What it's for |
|---|---|
| `claude-unlimited code` | **The one you'll use.** Launches `claude` routed through your pool. |
| `claude-unlimited code --profile <name>` | Pin the session to one account instead of rotating. |
| `claude-unlimited code --distribute` | Balance the session across accounts — the main agent and each subagent start on the least-busy account and stay there, each keeping its own prompt cache warm. |
| `claude-unlimited desktop` | Route the Claude **desktop app** through your pool, then launch it. `--revert` undoes it. |
| `claude-unlimited status` | Is the daemon installed and running, and its pid. |
| `claude-unlimited start` | Run the daemon in this terminal (Ctrl-C to stop). |

</details>

<details>
<summary><b>Background service</b></summary>

<br>

Auto-start on login — the same thing **Settings → Daemon** controls.

| Command | What it's for |
|---|---|
| `claude-unlimited install` | Start automatically on login (`launchd` / `systemd --user` / Task Scheduler), and start now. |
| `claude-unlimited uninstall` | Stop starting on login. |
| `claude-unlimited service-start` | Start the background daemon now. |
| `claude-unlimited service-stop` | Stop it. |
| `claude-unlimited hud install` / `status` / `remove` | macOS only. The HUD installs and updates itself, so this is rarely needed: `install` brings it back (or re-downloads it), `status` shows whether it is installed and starts at login, `remove` takes it away **and keeps it away** until you run `install`. |
| `claude-unlimited restart` | Stop and start the daemon, service-managed or not. Needed after an update replaces the code, since a running process keeps serving the version it started with. |

</details>

<details>
<summary><b>Removing it</b></summary>

<br>

| Command | What it's for |
|---|---|
| `claude-unlimited purge` | Removes everything: stored credentials, config, usage history, the app and its virtualenv, the CLI symlink, the service registration, and on macOS the HUD. If the Claude desktop app was routed through the pool, its own settings are restored first. Asks for confirmation first. `~/.claude` is never touched. |

Credentials are deleted from your OS keystore *before* the config goes, since
the config is the only record of which Profiles exist.

</details>

---

## Troubleshooting

<details>
<summary><b>"Claude Unlimited — all profiles are out of capacity"</b></summary>
<br>

Every enabled account has used up its window. Requests are answered with **503** and that
message, plus a `Retry-After` header saying how many seconds until the first window
reopens — the dashboard shows the same reset time on each account.

It is said **once** per outage: one line in Activity and one notification, no matter how
many requests get rejected in the meantime. When an account comes back, Activity records
*Capacity is back*, and the next outage is announced afresh.

Nothing to fix if you are simply out of quota — add or enable another account to keep
working. A pool whose accounts are all *disabled* or *need re-auth* reports the different
message *No eligible Profile is available*, because that one is a configuration problem.
</details>

<details>
<summary><b>An account says "needs re-auth"</b></summary>
<br>

Usually it fixes itself — tokens are refreshed in the background whether or not an account
is currently in rotation. If it doesn't, run `claude-unlimited reauth`: it lists whichever
accounts actually need it, so there's no guessing.
Once it is signed back in, its usage is read straight away, so the Dashboard and the HUD
show real numbers for it within seconds instead of at the next scheduled read.
</details>

<details>
<summary><b>"Waiting for API response · will retry … check your network"</b></summary>
<br>

Claude Code shows this when a request has had no answer for three minutes, then sends it
again — while the first is still being worked on. Since 1.3.1 the proxy prevents it: when a
streaming request's upstream has not answered after 10 seconds, the proxy starts the response
itself and sends the same keep-alive pings the Anthropic API sends while a model thinks, until
the real answer arrives. If that answer is an error, it reaches Claude Code as the provider's
own error event, handled as it would be on a direct connection.

Still seeing it? The upstream itself is unreachable — check Activity for *could not reach
upstream*.
</details>

<details>
<summary><b><code>claude-unlimited: command not found</code></b></summary>
<br>

`~/.local/bin` isn't on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```
</details>

<details>
<summary><b>The dashboard won't load</b></summary>
<br>

```bash
claude-unlimited status
claude-unlimited doctor
```

If it's installed but stopped: `claude-unlimited service-start`. Logs are in
`~/.claude-unlimited/logs/`.
</details>

<details>
<summary><b>Rotation isn't happening</b></summary>
<br>

Check the next account is **enabled**, not already exhausted, and has a higher priority
number than the current one. The activity log records every rotation and every reason one
was skipped.
</details>

<details>
<summary><b>Port 4317 is taken</b></summary>
<br>

`claude-unlimited start --port 4400` — and point `ANTHROPIC_BASE_URL` at the same port.
</details>

---

## Where your data lives

| What | Where |
|---|---|
| Credentials | macOS Keychain / Linux Secret Service / Windows DPAPI — never plaintext on disk |
| Account configuration | `~/.claude-unlimited/config.json` |
| Usage history, activity log, project attribution | `~/.claude-unlimited/claude_unlimited.db` — one local SQLite file, kept in full (older `*.jsonl` logs are imported once on first start and renamed `*.imported`) |
| Logs | `~/.claude-unlimited/logs/` |
| Your Claude Code setup | `~/.claude` — untouched |

## Security

- Binds to `127.0.0.1` only, and refuses to bind anywhere else.
- `claude` authenticates with a local placeholder token, never your real credentials.
- The dashboard API is CSRF-protected with a strict Content-Security-Policy.
- Nothing local is ever transmitted anywhere except the provider you configured.

Full threat model and vulnerability reporting: [`SECURITY.md`](SECURITY.md).

## Requirements

- **macOS, Linux, or Windows** — macOS is verified on real hardware; the Linux and Windows
  service backends are a real but **unverified first cut** (the test suite itself runs on
  macOS and Linux in CI)
  ([details](docs/adr/0005-windows-linux-backends-unverified-first-cut.md)).
- **Python 3.10+**, **git**, and the **`claude`** CLI.
- The HUD needs **macOS 13** or later; everything else runs without it.

## A note on Terms of Service

Claude Unlimited automates something you could do by hand: switching to another of your
own accounts when one runs low. It uses only credentials you configure, runs entirely on
your machine, and is fully open source.

Anthropic hasn't explicitly endorsed automated multi-account rotation. This project
deliberately avoids what would make that worse — no quota probing, no keep-warm traffic,
nothing that acts without a real request from you. Use your own judgment about your own
accounts' terms.

## Development

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest tests/
```

`pytest` is the only extra the `dev` install adds — the daemon itself still
needs nothing but the standard library and `cryptography`.

The HUD's pure logic (edge snapping) has its own tests: `cd macos-widget && swift test`.

No frontend build step — `claude_unlimited/static/` is plain HTML/CSS/JS.
See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the module map, and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the ground rules.

Adding a language is one file: copy `claude_unlimited/locales/en.json` and translate it.

## Roadmap

- **Signed releases** — the updater verifies that a downloaded release matches the commit
  GitHub names for its tag, which proves the code came from this repository's history. A
  detached signature would additionally prove authorship; not implemented yet.
- **Real-hardware verification on Linux and Windows** — the code exists and is unit-tested,
  but needs someone running it for real. [`CONTRIBUTING.md`](CONTRIBUTING.md#os-support-status)
  has what to check.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Keep the backend dependency-free, keep OS-specific
code behind the existing interfaces, and keep account management in the dashboard.

Bug reports and PRs welcome — especially from Linux and Windows users.

## License

[MIT](LICENSE) — do whatever you'd like with it.

<sub>Statistics charts use <a href="https://github.com/leeoniya/uPlot">uPlot</a> © Leon Sorokin,
MIT (<a href="claude_unlimited/static/vendor/LICENSE">license</a>).</sub>

<div align="center">
<br>
If this saves you a session, <a href="https://ko-fi.com/devdock">buy me a coffee</a> ☕
</div>
