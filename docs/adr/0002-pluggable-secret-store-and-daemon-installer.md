# OS-specific code lives behind two small interfaces, not scattered platform checks

Claude Unlimited ships macOS-only for MVP (daemon auto-start and secret storage both currently only have a macOS implementation), but cross-platform support was an explicit goal from the start. Rather than defer that decision until a second OS is actually being added — which tends to mean untangling `platform.system()` checks sprinkled through the codebase after the fact — we define two narrow interfaces now, each with exactly one implementation:

- **Secret storage**: `set_token` / `get_token` / `delete_token` / `has_token`, implemented today by `keychain.py` against macOS Keychain via the `security` CLI.
- **Daemon install/auto-start**: register-on-login, start, stop, status, implemented today against macOS `launchd`.

Every other module calls these interfaces, never the underlying OS mechanism directly. Adding Windows or Linux support later is adding one new implementation file per interface (Windows Credential Manager via `ctypes`; a permission-locked file for Linux as a first pass) and a platform-detection switch at the single point each interface is constructed — not a rewrite, and not a search-and-replace across the codebase for every place a secret gets read or the daemon gets started.

The cost is a small amount of upfront structure for a capability (non-macOS support) that doesn't exist yet. We accept that cost because retrofitting an interface boundary after platform-specific assumptions have leaked into a dozen call sites is the more expensive path, and this project's stated goal — "the same code runs everywhere, thin per-OS edges" — only holds if the boundary exists before it's needed, not after.

Status: accepted
