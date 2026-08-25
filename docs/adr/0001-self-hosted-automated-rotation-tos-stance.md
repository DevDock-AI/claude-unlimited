# Self-hosted automated Rotation is the accepted ToS stance

Claude Unlimited automates Rotation between a user's own Claude Pro/Max subscriptions and API credentials as each approaches its switch threshold. Anthropic hasn't explicitly blessed automated multi-account pooling, but Claude Code itself already offers a manual "switch to another account" path at exhaustion — this project only automates a click the user could already make themselves, using only credentials the user owns and configures in their own Dashboard.

We adopt this framing rather than avoiding the automation, following the precedent set by comparable existing multi-account tools in this space. To stay inside a "human-present, human-initiated" reading of that precedent, active quota probing and keep-warm traffic — the two mechanisms that act without the user present — are excluded from MVP entirely (see the project's stated non-goals).

Status: accepted
