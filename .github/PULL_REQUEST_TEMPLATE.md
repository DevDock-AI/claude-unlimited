## What this changes

<!-- One or two sentences. What problem does it solve? -->

## Why

<!-- Link an issue if there is one. -->

## How it was verified

<!-- Tests added/updated? Checked against a running daemon? On which OS? -->

- [ ] `python3 -m pytest tests/` passes
- [ ] Verified against a running daemon (say which OS)

## Project ground rules

<!-- See CONTRIBUTING.md. Tick what applies; explain in the PR if one can't hold. -->

- [ ] No new backend dependency (Python standard library only)
- [ ] No frontend build step introduced
- [ ] OS-specific code stays behind the existing pluggable interfaces
- [ ] Profile management stays in the Dashboard rather than new CLI flags
- [ ] Any new user-facing string is added to **all** locale files in `claude_unlimited/locales/`
- [ ] No credentials, tokens, or personal data in code, tests, comments, or screenshots
