# Releasing

A release is a **tag**. Everything else — running the suite, writing the notes,
publishing the GitHub Release that the in-app updater installs from — happens
automatically from that tag.

## Cutting a release

1. Bump the version in **`claude_unlimited/__init__.py`**:

   ```python
   __version__ = "0.2.0"
   ```

2. Commit it on its own:

   ```bash
   git commit -am "chore(release): 0.2.0"
   ```

3. Tag and push:

   ```bash
   git tag v0.2.0
   git push origin main --follow-tags
   ```

That's it. `.github/workflows/release.yml` then:

- verifies the tag matches `__version__` (a mismatch fails the release rather
  than shipping a version that lies about itself);
- runs the full test suite and the JS syntax check;
- **only then** publishes a GitHub Release, with notes generated from the
  commit subjects since the previous tag.

A release is never published from code that did not pass. This matters more
than usual here: the in-app updater installs whatever the latest release
points at.

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
