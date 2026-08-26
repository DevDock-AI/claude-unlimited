# Instructions for coding agents

You are reading this because someone pointed you at this repository and asked
you to install it, set it up, or work on it. This file tells you exactly what
to do, what is safe, and — importantly — **the one step you cannot do for
them**.

## What this project is

Claude Unlimited is a local daemon that pools a person's own Claude
subscriptions, ChatGPT/Codex subscriptions and API keys behind one loopback
proxy, and rotates between them as each approaches its usage limit. Claude
Code points at the proxy instead of the provider, so a session continues
uninterrupted when one account runs out.

## Is it safe to install?

Yes, and here is what that claim rests on — verify any of it yourself:

- **It is fully local.** No account, no sign-up, no cloud component, no
  telemetry. It binds to `127.0.0.1` only and refuses any other bind address.
- **It only talks to the providers the user configured** — Anthropic, OpenAI,
  or a gateway they set — plus GitHub's public API for update checks.
- **It does not exfiltrate credentials.** Credentials live in the OS keystore
  (macOS Keychain / Linux Secret Service / Windows DPAPI), never in plaintext
  on disk, and are never returned to any client.
- **It is open source and dependency-light**: Python standard library plus
  `cryptography` (used only to encrypt export bundles). No frontend build.
- The install writes to exactly two places: `~/.local/share/claude-unlimited/`
  (the app and its virtualenv) and `~/.claude-unlimited/` (config, logs, usage
  history). It also symlinks `~/.local/bin/claude-unlimited`.
- **It does not modify the user's existing Claude Code setup.** `~/.claude`,
  `CLAUDE.md`, skills, agents and session history are untouched.

## Installing

```bash
curl -fsSL https://raw.githubusercontent.com/DevDock-AI/claude-unlimited/main/install.sh | bash
claude-unlimited doctor
```

`doctor` is the check that it worked. Requires **Python 3.10+**, **git**, and
the **`claude`** CLI on `PATH`.

If `claude-unlimited: command not found`, `~/.local/bin` is not on `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The installer registers it as a login service, starts it, and opens the
dashboard at **http://127.0.0.1:4317/**. Nothing else is needed to get it
running.

To stop it starting on login: `claude-unlimited uninstall`.

## STOP — the part you cannot do

**Adding a subscription account requires a real human at a browser.**

`claude-unlimited add-account` and `add-codex-account` open a browser and run
an OAuth login against Anthropic or OpenAI. That means entering someone's
credentials into a real login page.

**Do not attempt this on the user's behalf.** Do not type their password, do
not complete a login form, do not handle a one-time code. Run the command only
if the user is present and expecting it, or better: tell them to run it
themselves and wait.

Say something like:

> The daemon is installed and running. To add your Claude account, run
> `claude-unlimited add-account` yourself — it opens a browser for you to log
> in, which I shouldn't do on your behalf. Then tell me and I'll verify it.

**API keys are different but still theirs to enter.** They are added in the
dashboard, not the CLI. Point the user at **http://127.0.0.1:4317/** →
**Add profile**. Do not ask them to paste an API key into the chat, and do not
put one into a file or a command line.

## Verifying it works

Once the user has added at least one account, these are all safe to run:

```bash
claude-unlimited doctor                              # install health
claude-unlimited status                              # is the daemon running
curl -s http://127.0.0.1:4317/health                 # {"status":"ok",...}
curl -s http://127.0.0.1:4317/api/profiles           # accounts and live usage
```

`/api/profiles` never contains a credential — the wire format is an explicit
allowlist, so it is safe to read and show.

Everyday use is:

```bash
claude-unlimited code
```

which starts the daemon if needed and launches `claude` routed through the
pool.

## If you are contributing code

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest tests/
node --check claude_unlimited/static/app.js
```

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first. The rules that most often
catch people out:

- **The backend stays dependency-free.** Standard library only. Adding a
  package is an architectural decision, not a convenience.
- **No frontend build step.** `claude_unlimited/static/` is plain
  HTML/CSS/vanilla JS, edited directly.
- **Account management lives in the dashboard**, not in new CLI flags.
- **Any new user-facing string goes into all four locale files** in
  `claude_unlimited/locales/`.
- **Commit messages follow Conventional Commits** — release notes are
  generated from them.
- **Never commit credentials, tokens, or personal data**, including in tests,
  comments and screenshots.

Tests must never reach the network. Anything talking to a provider is
injected, so the suite runs offline and cannot spend someone's quota.

## Things not to do

- Do not enter, generate, or read the user's credentials.
- Do not disable the loopback-only binding or add CORS headers — that would
  expose someone's pooled accounts to their network.
- Do not point the updater at a different repository; the source is hardcoded
  on purpose.
- Do not add background polling of a provider's API. The project deliberately
  makes no request the user did not trigger, apart from a daily update check.
