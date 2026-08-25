# Windows and Linux backends exist now, as an unverified first cut

`docs/adr/0002-*` deliberately scoped secret storage and daemon auto-start to macOS only for MVP, behind two small interfaces (`secret_store`, `daemon_installer`) specifically so a second OS would be additive later — a new backend module and one dispatch branch, never touching a call site. That day came before a Windows or Linux machine was available to develop or test against.

## Decision

Implement real Linux and Windows backends now, against the exact same interface contracts the macOS backends already satisfy, using only Python's standard library plus well-documented OS-native mechanisms (no new pip dependency):

- **Linux secrets**: `secret-tool` (libsecret / the Secret Service D-Bus API) — the same "shell out to a system CLI" shape `macos_keychain.py` uses for `security`.
- **Linux daemon install**: a `systemd --user` unit — the direct analogue of the macOS LaunchAgent.
- **Windows secrets**: DPAPI (`CryptProtectData`/`CryptUnprotectData` in `crypt32.dll`) via `ctypes` — per-user-encrypted blobs on disk, no extra dependency.
- **Windows daemon install**: a Task Scheduler task (`schtasks`) triggered on logon.
- **Windows process memory** (`GET /api/process`'s `memory_mb`): `GetProcessMemoryInfo` via `ctypes` + `psapi.dll`, since the `resource` module `_process_stats()` relied on is POSIX-only and doesn't exist on Windows at all (this was a real import-time crash waiting to happen the moment `daemon.py` was ever imported on Windows, independent of the two pluggable interfaces — fixed alongside them).
- **Linux/Windows desktop notifications**: `notify-send` (libnotify) and a PowerShell-driven WinRT toast, added to `notifications.py` alongside the existing `osascript` path.

## Why now, unverified, rather than waiting

Waiting for real hardware before writing a single line was the original plan, and remains the *right* plan for calling this verified. But the interfaces were already designed for exactly this extension, the mechanisms chosen (`secret-tool`, `systemd --user`, DPAPI, `schtasks`) are standard and thoroughly documented independent of this project, and every command-construction and orchestration path that *can* be tested without the real OS (mocking `subprocess.run`, faking `ctypes.windll` to verify the ctypes struct/call plumbing) now is — see `tests/test_secret_store_linux_secretservice.py`, `tests/test_daemon_installer_linux_systemd.py`, `tests/test_secret_store_windows_dpapi.py`, `tests/test_daemon_installer_windows_taskscheduler.py`, `tests/test_daemon_memory_mb.py`, and the Linux/Windows additions to `tests/test_notifications.py`. What those tests cannot prove is that a real Secret Service provider, a real `systemd --user` session, real DPAPI, or a real Task Scheduler task actually behave the way their documentation says on an actual Linux or Windows machine.

So: this is real, reviewable, individually-testable code — not a stub — but it is explicitly **unverified**, and every new backend module says so plainly in its own docstring. This project's own standing principle (verify against real behavior, never claim something works because it looks right) applies here as much as anywhere: a Windows or Linux user is the first real verification this code gets, and CONTRIBUTING.md's "third-OS smoke test" guidance is the way that gap closes — someone running `claude-unlimited doctor` / `install` / `add-account` for real on their own machine and reporting back (or fixing forward) what's actually wrong.

## What this does and doesn't change

- `claude_unlimited/secret_store/__init__.py` and `claude_unlimited/daemon_installer/__init__.py` now dispatch on `platform.system()` across three branches (`Darwin`, `Linux`, `Windows`) instead of one, with the same "unsupported OS" fallback as before for anything else.
- The earlier "macOS-only daemon install and secret storage" scope line is superseded by this ADR for the two interfaces it names.
- Nothing about the Dashboard, Rotation, proxy, or usage-tracking logic changed — this is purely the OS-glue layer CONTRIBUTING.md already described as the intended extension point.
- No new dependency was added on any platform — `pyproject.toml`'s `dependencies` list is unchanged.
