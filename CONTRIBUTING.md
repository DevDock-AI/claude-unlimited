# Contributing

Standards for working on Claude Unlimited, so features added later — including non-macOS support — land the same way every time instead of each depending on tribal knowledge.

Run the suite with `python -m pip install -e ".[dev]"` then `python -m pytest tests/`. The `dev` extra adds `pytest` and nothing else.

## Ground rules

- **Backend stays dependency-free, with one recorded exception.** Python standard library only — no `pip install` required to run the daemon, except `cryptography`, used exclusively by `export_import.py` for authenticated encryption of credential-containing Export bundles. If a feature seems to need a package, look for a stdlib way first; if there truly isn't one, that's a decision for a new ADR, not a quiet addition to `pyproject.toml`.
- **Frontend stays dependency-free too**, in the same sense: no `npm install`, no build step. A static asset (an icon font, a small chart library) may be vendored as a committed file and referenced locally — never fetched from a CDN at runtime.
- **OS-specific code lives behind its interface, always.** Secret storage and daemon auto-start each have one implementation per OS (macOS/Linux/Windows — see `docs/adr/0005-*.md`) behind a single small interface. Never hardcode an OS-only assumption (`security`/`secret-tool`/DPAPI, `launchd`/`systemd`/`schtasks`, a Keychain or credential-store path) outside that interface's implementation file — the rest of the daemon never checks `platform.system()` directly except the two `__init__.py` dispatch points and the one POSIX-only-stdlib guard in `daemon.py` (`resource`, not available on Windows).
- **The Dashboard is the only place profiles are managed.** Profile CRUD — listing, editing, deleting, thresholds, priority, enable/disable — has no CLI and should not grow one. A feature that needs a form belongs in the Dashboard, not a new CLI flag. The CLI covers what a browser form cannot do: daemon lifecycle (`start`/`status`/`restart`/`install`/`uninstall`/`service-*`), `doctor`, `purge`, launching a routed session (`code`) or the desktop app (`desktop`), and the interactive browser logins that must happen at a terminal (`add-account`, `add-codex-account`, `reauth`).

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

"Unverified" means: real, reviewed, individually unit-tested code (mocking the subprocess/ctypes calls) that has never run against a real Secret Service provider, `systemd --user` session, DPAPI call, or Task Scheduler task — there's no Linux or Windows machine in this project's development environment. **If you're the first person running this on Linux or Windows: that's the real smoke test.**

### Smoke-test checklist (run on a real Linux / Windows box)

Do these in order; each line is something a static review can't confirm. Report (or fix) anything that doesn't match.

**Both platforms**
1. `claude-unlimited doctor` — secret store names *this* platform's backend (not "macOS Keychain"), notifications line is right, no traceback.
2. `claude-unlimited add-account` — the browser opens (this is the env/`.cmd`-shim path); the account appears in the dashboard afterwards.
3. Add an API key in the dashboard, then **remove** it — its credential is gone from the OS store (not just the config row).
4. `claude-unlimited code` — a real `claude` session starts and routes through the pool (check the Activity log); on Windows the shell doesn't fight the TUI for the console.
5. Dashboard → **Send test notification** — an actual toast/notification appears (not just a "sent" toast).
6. Settings → **Restart** the daemon, and let an update install if one's available — the daemon comes back (this is the self-restart path).
7. `claude-unlimited uninstall` — the service/unit/task is gone and doesn't error on next login.

**Linux specifically**
8. After `install`, log out and back in — the daemon is still running (`loginctl show-user $USER --property=Linger` should say `Linger=yes`).
9. On a **non-systemd** box (WSL2 without `systemd=true`): `install` fails with a clear message, not a traceback, and `doctor`/`status` still work afterward.
10. Re-run `install --port <new>` — the daemon actually moves to the new port.

**Windows specifically**
11. `pip install`, then `claude-unlimited install` from an **elevated** prompt — the logon task is created; confirm it runs unelevated (`/rl limited`).
12. Close the terminal you ran `claude-unlimited code` from — the background daemon keeps running (detached), and Ctrl-C in that terminal didn't kill it.
13. Whichever `claude` you have (native `.exe` or npm `.cmd`) — `code` and `add-account` both launch it.

## Translations

Every user-facing string lives in `claude_unlimited/locales/*.json` as a flat `key -> string` map. If you add or rename a key, add it to **all** locale files in the same change. A missing key falls back to English, so a partial translation still renders — but a key that exists in only one file is a bug waiting to surface.

Adding a whole new language is one file: copy `en.json` to `<code>.json` and translate it. No registration step — the available set is derived from the files present.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org). The subject line
is not bookkeeping here — release notes are generated from these subjects, so
each one is the changelog entry a user will read.

```
<type>(<scope>): <subject>
```

| Type | For |
|---|---|
| `feat` | A new capability a user can notice |
| `fix` | A bug fix |
| `docs` | Documentation only |
| `refactor` | Behaviour unchanged, structure changed |
| `test` | Tests only |
| `chore` | Tooling, CI, dependencies, release bumps |
| `perf` | A performance change |

Scope is optional and names the area: `rotation`, `codex`, `dashboard`,
`updater`, `cli`, `security`, `i18n`.

Rules that matter:

- **Subject in the imperative, lowercase, no trailing period** — "add a
  weekly quota window", not "Added weekly quota windows."
- **Say what changed, from the user's side**, not which function you edited.
  `fix(codex): honor stream:false so auto mode can classify tools` beats
  `fix: update gateway.py`.
- **The body explains why**, wrapped at 72 columns. The diff already shows
  what.
- **Breaking changes** get a `!` and a footer:

  ```
  feat(config)!: drop the legacy single-profile format

  BREAKING CHANGE: configs written before 0.1 are no longer read.
  Export from the old version and import into the new one.
  ```

Examples:

```
feat(updater): check, download and install releases per the configured mode
fix(rotation): keep the backoff streak across snapshot rebuilds
docs(readme): show the rotation flow in the header
chore(release): 0.2.0
```

## Pull requests

`main` is protected. Everything lands through a pull request, reviewed and
merged by the maintainer.

A PR description says:

1. **What changes** — one or two sentences, from the user's side.
2. **Why** — the problem, linked to an issue if there is one.
3. **How it was verified** — tests added or updated, and which OS you
   actually ran it on.

The title follows the same convention as a commit subject, because a squashed
merge becomes one. `.github/PULL_REQUEST_TEMPLATE.md` carries the checklist.

## Never let untrusted text reach a shell

This bit us once and is worth stating outright.

- **In workflows, never interpolate `${{ ... }}` into a `run:` block.** Commit
  subjects, branch names, PR titles and issue bodies are attacker-supplied.
  Interpolating them means backticks and `$( )` in a commit message execute in
  CI. Write the value to a file, or pass it through `env:` and reference it as
  a shell variable.
- **In Python, always pass an argv list to `subprocess`.** No `shell=True`, no
  `os.system`, no f-string commands. Every call in this codebase does this
  already.
- **In shell, quote every expansion**, and validate anything that came from
  the environment before it reaches a command line.
- **Give a workflow the narrowest `permissions:` it can do its job with**, and
  scope any write to the single job that needs it.

## Scope and hygiene

- Keep changes scoped to what was asked. A bug fix doesn't carry a drive-by refactor.
- If a change touches config schema or an architectural decision, the corresponding doc/ADR update is part of the same change, not a follow-up task.
- Never commit credentials, tokens, or personal data — including in tests, comments, and screenshots.

## Releases

See [`docs/RELEASING.md`](docs/RELEASING.md). Short version: bump
`__version__`, tag `vX.Y.Z`, push. CI verifies the tag matches the package
version, runs the suite, and only then publishes the GitHub Release the
in-app updater installs from.
