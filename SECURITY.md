# Security

Claude Unlimited is a local daemon holding real Anthropic credentials on your own machine. This document explains its security model and how to report a problem.

## Threat model

Claude Unlimited defends against:

- **A malicious website in your browser.** The daemon binds to `127.0.0.1` only and validates the `Host` header on every request (blocking DNS-rebinding), so a page open in another tab can't reach the Dashboard's API. State-changing requests additionally require a per-run CSRF token that only same-origin JavaScript can obtain.
- **Another process on your machine reading credentials off disk.** Credentials never touch disk in plain text — they're stored in the OS's native secret store (macOS Keychain, Linux Secret Service, Windows DPAPI) and never appear in `config.json`, logs, or any API response.
- **XSS via a crafted Profile name, project name, or model string.** Every piece of user- or upstream-controlled text is HTML-escaped before being rendered in the Dashboard.

Claude Unlimited does **not** defend against:

- **Someone with your OS user account already logged in.** Filesystem-level access to `~/.claude-unlimited` and your OS's secret store is the same trust boundary every local credential manager (browser password store, `gh` CLI, `git credential` helpers) relies on. If your machine is compromised at that level, this project's security model doesn't add protection beyond what those already assume.
- **A malicious or compromised upstream gateway you've configured as an API/gateway Profile.** You're choosing what to point a Profile's `base_url` at; the daemon substitutes credentials into whatever you configured.

## What never leaves your machine

Nothing — usage stats, project attribution, the activity log, your Profile configuration — is transmitted anywhere except to the provider you configured — Anthropic, OpenAI/Codex, or your own gateway — as part of the API requests you're already making. There is no telemetry, no analytics, and no phone-home beyond polling GitHub's public Releases API to compare version numbers. The daemon does make one other request you didn't directly trigger: refreshing an OAuth account's access token shortly before it expires, which sends that account's own refresh token to the provider it already belongs to and nothing else.

## Hardening in place

- Strict `Content-Security-Policy` (`default-src 'self'`, no inline scripts, `object-src 'none'`, `frame-ancestors 'none'`, and more — see `daemon.py`'s `_security_headers()`).
- CSRF tokens generated with `secrets.token_urlsafe(32)`, compared with `secrets.compare_digest` (constant-time).
- The local placeholder token `claude` authenticates with is compared the same way, and is never sent to Anthropic.
- Export bundles containing credentials are always passphrase-encrypted (Fernet, PBKDF2-HMAC-SHA256 at 600,000 iterations — the OWASP 2023 floor — with a fresh random salt per export).
- Request logging is disabled by default in the HTTP handler specifically so a path, header, or query string containing something sensitive is never written to a log file.

## Reporting a vulnerability

If you find a security issue, please **do not open a public GitHub issue**. Instead, use GitHub's private vulnerability reporting: go to the repository's **Security** tab → **Report a vulnerability**. You should get an acknowledgment within a few days.

Please include:
- A description of the issue and its potential impact
- Steps to reproduce (a minimal example is ideal)
- The version/commit you tested against

## Platform-specific notes

The macOS backend (Keychain, `launchd`) is verified against real hardware. The Linux (`secret-tool`, `systemd --user`) and Windows (DPAPI, Task Scheduler) backends are real, unit-tested code that has not yet been run against real Linux/Windows hardware — see [`docs/adr/0005-windows-linux-backends-unverified-first-cut.md`](docs/adr/0005-windows-linux-backends-unverified-first-cut.md). If you find a security-relevant issue specific to those platforms, it's especially valuable to report.
