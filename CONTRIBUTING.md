# Contributing

Standards for working on Claude Unlimited, so features added later — including non-macOS support — land the same way every time instead of each depending on tribal knowledge.

## Ground rules

- **Backend stays dependency-free, with one recorded exception.** Python standard library only — no `pip install` required to run the daemon, except `cryptography`, used exclusively by `export_import.py` for authenticated encryption of credential-containing Export bundles. If a feature seems to need a package, look for a stdlib way first; if there truly isn't one, that's a decision for a new ADR, not a quiet addition to `pyproject.toml`.
- **Frontend stays dependency-free too**, in the same sense: no `npm install`, no build step. A static asset (an icon font, a small chart library) may be vendored as a committed file and referenced locally — never fetched from a CDN at runtime.
- **OS-specific code lives behind its interface, always.** Secret storage and daemon auto-start each have one implementation per OS (macOS/Linux/Windows — see `docs/adr/0005-*.md`) behind a single small interface. Never hardcode an OS-only assumption (`security`/`secret-tool`/DPAPI, `launchd`/`systemd`/`schtasks`, a Keychain or credential-store path) outside that interface's implementation file — the rest of the daemon never checks `platform.system()` directly except the two `__init__.py` dispatch points and the one POSIX-only-stdlib guard in `daemon.py` (`resource`, not available on Windows).
- **The Dashboard is the only place profiles are managed.** The CLI is daemon lifecycle (`start`/`stop`/`status`) plus `doctor`. A feature that needs a form belongs in the Dashboard, not a new CLI flag.

## Vocabulary

Reuse the existing terms. If you're about to introduce a name that means roughly the same thing as one already in use (`Profile`, `Pool`, `Rotation`, `Switch threshold`, `Placeholder token`...), use the existing term instead — the codebase, the Dashboard copy, and the docs all read as one thing that way.

## When to write an ADR

Write one in `docs/adr/000N-slug.md` only when all three are true:

1. Hard to reverse later.
2. Would surprise a future reader who'd wonder "why did they do it this way?"
3. Was a real trade-off — genuine alternatives existed.

Most changes don't qualify. A short paragraph is enough: context, decision, why. See `docs/adr/0001-*.md` for the format and length to aim for.

## OS support status

Three backends exist today behind each interface (see `docs/adr/0002-*.md` for why they're pluggable, `docs/adr/0005-*.md` for how the second and third arrived):

- **Secret storage** (`claude_unlimited/secret_store/`) — macOS Keychain (`security` CLI, verified on real hardware), Linux Secret Service (`secret-tool`, unverified), Windows DPAPI (`ctypes` + `crypt32.dll`, unverified).
- **Daemon install/auto-start** (`claude_unlimited/daemon_installer/`) — macOS `launchd` (verified), Linux `systemd --user` (unverified), Windows Task Scheduler (`schtasks`, unverified).
- **Desktop notifications** (`claude_unlimited/notifications.py`) — macOS `osascript` (verified), Linux `notify-send` (unverified), Windows PowerShell toast (unverified).

"Unverified" means: real, reviewed, individually unit-tested code (mocking the subprocess/ctypes calls) that has never run against a real Secret Service provider, `systemd --user` session, DPAPI call, or Task Scheduler task — there's no Linux or Windows machine in this project's development environment. **If you're the first person running this on Linux or Windows: that's the real smoke test.** Run `claude-unlimited doctor`, `install`, `add-account`, and a full Profile add/remove/export cycle, and report back (or send a fix) for whatever doesn't match its module's docstring — that's specifically what CONTRIBUTING.md asks for before calling a backend verified.

## Translations

Every user-facing string lives in `claude_unlimited/locales/*.json` as a flat `key -> string` map. If you add or rename a key, add it to **all** locale files in the same change. A missing key falls back to English, so a partial translation still renders — but a key that exists in only one file is a bug waiting to surface.

Adding a whole new language is one file: copy `en.json` to `<code>.json` and translate it. No registration step — the available set is derived from the files present.

## Commit / PR expectations

- Keep changes scoped to what was asked. A bug fix doesn't carry a drive-by refactor.
- If a change touches config schema or an architectural decision, the corresponding doc/ADR update is part of the same change, not a follow-up task.
- Never commit credentials, tokens, or personal data — including in tests, comments, and screenshots.
