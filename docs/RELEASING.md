# Releasing

A release is a **tag**. Everything else — running the suite, writing the notes,
publishing the GitHub Release that the in-app updater installs from — happens
automatically from that tag.

## Cutting a release

0. Re-verify the hardcoded GPT window table against the Codex CLI's cache
   (docs/adr/0009 — a stale window is a routing bug):

   ```bash
   python3 scripts/check_gpt_windows.py
   ```

1. Bump the version in **`claude_unlimited/__init__.py`**:

   ```python
   __version__ = "0.2.0"
   ```

2. **Build the macOS HUD and commit its checksum** (on a Mac — see below for
   why this cannot be CI):

   ```bash
   cd macos-widget && ./build.sh --release
   ```

   That writes `macos-widget/dist/HUD-0.2.0-macos.zip` — a **universal**
   binary (Apple Silicon + Intel; the build refuses to finish otherwise) — and
   `macos-widget/HUD.sha256`. The **checksum file is committed**; the zip is
   not.

3. Commit the version and the checksum. Stage the two files by name — never
   `git commit -a`, which silently leaves out any file git does not track yet:

   ```bash
   git add claude_unlimited/__init__.py macos-widget/HUD.sha256
   git commit -m "chore(release): 0.2.0"
   ```

4. Tag and push:

   ```bash
   git tag v0.2.0
   git push origin main --follow-tags
   ```

   `.github/workflows/release.yml` then:

   - verifies the tag matches `__version__` (a mismatch fails the release rather
     than shipping a version that lies about itself);
   - runs the full test suite and the JS syntax check;
   - **only then** creates a **draft** GitHub Release, with notes generated
     from the commit subjects since the previous tag.

5. Once the workflow has finished, attach the HUD and publish:

   ```bash
   scripts/publish_release.sh v0.2.0 [release-notes.md]
   ```

   Pass a notes file to replace the generated list of commit subjects with
   hand-written release notes; they are set while the release is still a draft.

   It refuses unless the release is still a draft, the zip matches the
   `HUD.sha256` committed **at the tag**, and the binary is universal; it
   uploads the zip, downloads it back and checks the digest again, and only
   then publishes the release and marks it latest.

**Why a draft.** Releases in this repository are **immutable** once published:
GitHub refuses to attach anything afterwards. Publishing first and uploading
the HUD second — the old order — cannot work, and a release published without
its HUD would stay that way forever. The updater only sees published releases,
so nobody updates to the new version until step 5 has attached and verified it.

A release is never published from code that did not pass. This matters more
than usual here: the in-app updater installs whatever the latest release
points at.

**How the HUD reaches users.** Nothing asks them. `install.sh` installs it on a
fresh Mac; an existing install gets it from the NEW code the first time it
runs after an update (the daemon starts, or `cu code`), because the update
itself is applied by the old updater. The daemon retries a failed download on
a slow backoff (10 min → 6 h), so a Mac that updated offline still gets it.
`cu hud remove` is remembered: nothing automatic puts it back until
`cu hud install`.

## Why the HUD is built by hand

The release workflow runs on `ubuntu-latest` and could not build a macOS app
anyway, but there is a second reason a macOS CI job would not help: the HUD's
shader sources are licensed to **compile and ship** but
explicitly **not** to publish. They live outside this repository, so only a
machine that has them can produce the real build. CI would produce one without
them.

That is also why the checksum is committed rather than published in the
release body. `updater.stage_release` already proves the installed tree is the
commit GitHub named for the tag, so a digest read out of that tree inherits the
check; a digest read off the release page would only be the same transport,
asked twice. If `HUD.sha256` names a different version than the one being
installed, the installer does nothing — which is the correct behaviour for a
release that shipped no HUD.

The bundle is **ad-hoc signed**, so `claude_unlimited/hud.py` strips the
quarantine attribute after unpacking; without that, a downloaded app refuses to
launch. When there is a paid Developer ID to notarize with, that step goes away.

## Versioning

[Semantic versioning](https://semver.org): `MAJOR.MINOR.PATCH`.

| Bump | When |
|---|---|
| **PATCH** — `0.1.0 → 0.1.1` | Bug fixes, docs, internal changes with no visible behaviour change |
| **MINOR** — `0.1.0 → 0.2.0` | New features, new settings, anything additive |
| **MAJOR** — `0.9.0 → 1.0.0` | Breaking changes: a removed CLI command, an incompatible config or export format |

Since 1.0.0 the contract is real: a breaking change needs a MAJOR bump, and
says so plainly in the commit subject so it reaches the notes.

Tags are always `v`-prefixed (`v0.2.0`); `__version__` never is (`0.2.0`).

## How the updater consumes a release

Worth knowing when deciding what to publish, since it constrains the process:

1. The daemon asks the GitHub API for the latest release and reads its tag.
2. If that tag is a newer version than the running one, it asks the API for
   the commit SHA that tag points at.
3. It clones that tag and refuses to go further unless the commit git
   actually checked out is the same SHA the API named.
4. It installs into the existing virtualenv, keeping the previous copy, and
   rolls back automatically if the new version cannot even be imported.

Consequences:

- **Never move or delete a published tag.** The updater resolves tag → SHA,
  so a moved tag means an installed version no longer matches its own release.

  This is not just untidy — it is **unrecoverable**. Releases on GitHub are
  immutable: once a version has carried a published release, that version
  number is permanently reserved, and deleting the release does not free it.
  Re-publishing under the same tag is refused, so the only way forward is to
  skip the number entirely. Seven consecutive versions were burned this way in
  this repository, by deleting releases in order to re-publish them. If a
  release is wrong, **publish a new patch version** — never delete and retry.
- **Never publish a release for a tag that failed CI.** The workflow enforces
  this, so the only way to break it is by publishing a release by hand.
- Release notes are informational — nothing parses them.

## Changelog

Release notes are generated from commit subjects between tags, which is why
[the commit convention](../CONTRIBUTING.md#commit-messages) matters: the
subject line *is* the changelog entry a user reads.

There is no hand-maintained `CHANGELOG.md`. It would be a second source of
truth to keep in sync with the tags, and the tags are the thing the updater
actually reads.
