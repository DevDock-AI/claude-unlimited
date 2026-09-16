# Self-hosted automated Rotation is the accepted ToS stance

Claude Unlimited automates Rotation between a user's own Claude Pro/Max subscriptions and API credentials as each approaches its switch threshold. Anthropic hasn't explicitly blessed automated multi-account pooling, but Claude Code itself already offers a manual "switch to another account" path at exhaustion — this project only automates a click the user could already make themselves, using only credentials the user owns and configures in their own Dashboard.

We adopt this framing rather than avoiding the automation, following the precedent set by comparable existing multi-account tools in this space. To stay inside a "human-present, human-initiated" reading of that precedent, active quota probing and keep-warm traffic — the two mechanisms that act without the user present — are excluded from MVP entirely (see the project's stated non-goals).

Status: accepted

## Amendment — 2026-09-15: read-only usage checks while the user is present

The owner chose to keep usage numbers fresh with background reads of each provider's
read-only usage endpoint (`usage_probe.py`). This stays within the "human-present" reading
above: reads run only while the user is present (any proxied request or Dashboard input;
paused after 30 idle minutes), send no messages, are limited to one per account every 5–10
minutes, back off hard and persistently on 429/401/403, and can be turned off in Settings.
Keep-warm traffic — requests sent to start or game a quota window — remains excluded.

