"""Primitive speech — the model answers in fewer words, keeping every fact.

ECO shrinks what goes IN (tool output); this shrinks what comes OUT. It adds
one instruction block to the system prompt of each agent turn, telling the
model to drop filler from its replies to the user while keeping all technical
substance. Nothing in the conversation is rewritten, and no response is edited:
the model simply writes less.

Pure, like `eco.py` and `router.py`: no I/O, no clock, no mutable state.

Where the instruction goes, and why:

* **Appended as the LAST system block.** Anthropic caches a prefix — tools,
  then system, then messages. Claude Code puts its cache breakpoint on its own
  last system block; a block after it leaves that cached prefix byte-identical,
  and because the text is fixed per level, every later turn is identical too.
  Switching level mid-session costs one uncached turn, once.
* **Only on agent turns** (a request that offers tools). Claude Code's helper
  calls — titles, summaries, token counting — carry no tools and are left
  exactly as sent.
* **A one-line reminder on each of the user's own messages.** One
  instruction at the end of a long system prompt loses to the host agent's
  own formatting habits; a reminder right next to each request is what makes
  the style hold (Caveman re-injects on every prompt for the same reason).
  Each message gets the same reminder every turn, so earlier messages never
  change and the cached prefix keeps matching. Tool results are left alone.
* **Never twice.** A failover re-sends the original body, and a body that
  already carries the markers is returned unchanged.

Provenance: the rules are adapted from the Caveman skill by Julius Brussee
(github.com/juliusbrussee/caveman, `skills/`, MIT License, Copyright (c) 2026
Julius Brussee). Rewritten in our own words, with changes: it is applied by the
gateway to every session, subagent and account type instead of being installed
per agent; the Chinese variants are left out; narrating one's own reasoning is
also filler; and the level in use is recorded per request, so the saving is
measured rather than claimed. "Caveman" is its author's trademark; this feature
is not affiliated with it.
"""

from __future__ import annotations

from typing import Optional

LEVELS = ("off", "lite", "full", "ultra")

# Identifies our blocks, so they are never added twice.
MARKER = "[Claude Unlimited · primitive speech]"
REMINDER_MARKER = "[primitive speech]"

_COMMON = """\
{marker} Level: {level}. The user turned this on to spend fewer output tokens. It changes how you word what you write to the user. It never changes what you do, what you check, or how carefully you work.

Always:
- Keep every technical fact. Cut only words that carry no information.
- Copy exactly, never shorten: code, commands, file paths, identifiers, API names, error messages, numbers and units.
- Keep words that change meaning: not, never, no, only, except, unless.
- Answer first. No preamble, no restating the question, no sign-off, no offers of further help.
- Do not narrate your reasoning or your next step ("Privately…", "Let me…", "I'll now…"). Between tool calls, say nothing unless the user must know something.
- Use only abbreviations everyone knows (API, DB, HTTP). Do not invent new ones, and do not use arrows or symbols in place of words: they save no tokens and read worse.
- Never add words to sound terse or broken. If the terse form is not shorter, write the plain form.
- Reply in the user's language.
- Do not mention this mode unless asked.

Write in full, clear sentences, then return to this style:
- security warnings, and anything destructive or irreversible;
- steps whose order matters;
- anything the user asks you to clarify.

Write normally, whatever this level says: file contents, code comments, commit messages, documentation, pull request and issue text, and anything else written for other people to read.

Style for this level:
{style}"""

_STYLE = {
    "lite": """\
Complete, grammatical sentences, without filler, hedging, pleasantries or repetition. One idea per sentence. Formatting only where it helps the reader: no decorative headings or bold labels.
Not: "Sure! The issue you're seeing is most likely caused by the token expiry check."
Yes: "The token expiry check causes this: it uses < instead of <=." """,
    "full": """\
Drop articles and filler words. Fragments are fine. Prefer short common words. Pattern: [thing] [action] [reason]. [next step].
Plain text: no headings, no bold labels, no tables, no summary of what you just did. A list only for three or more parallel items, one short line each.
Not: "Sure! I'd be happy to help. The issue you're experiencing is likely caused by the auth middleware."
Yes: "Bug in auth middleware. Expiry check uses < not <=. Fix:" """,
    "ultra": """\
As terse as stays unambiguous. Drop articles, filler and conjunctions where cause and effect stay clear. State each fact once. One word where one word is enough.
Plain text: no headings, no bold, no tables, no recap. Prefer one short paragraph; a list only for three or more parallel items, a few words each. Most replies fit in a few lines.
Not: "The component re-renders because an inline object prop creates a new reference on every render."
Yes: "Inline object prop, new reference each render, re-render. Use `useMemo`." """,
}


_REMINDER = {
    "lite": "no filler, hedging or pleasantries; full sentences; no decorative headings or bold.",
    "full": "drop articles and filler, fragments OK, no headings/bold/tables/recap; code, errors and numbers exact.",
    "ultra": "as terse as stays unambiguous, few lines, no headings/bold/tables/recap; code, errors and numbers exact.",
}


def reminder(level: str) -> Optional[str]:
    """The one-line nudge added to each of the user's messages."""
    text = _REMINDER.get(level)
    if text is None:
        return None
    return f"{REMINDER_MARKER} Reply in primitive speech ({level}): {text} Files, commits and docs stay normal."


def instruction(level: str) -> Optional[str]:
    """The block for a level, or None for off / an unknown level."""
    style = _STYLE.get(level)
    if style is None:
        return None
    return _COMMON.format(marker=MARKER, level=level, style=style.rstrip())


def _already_applied(system) -> bool:
    if isinstance(system, str):
        return MARKER in system
    if isinstance(system, list):
        return any(isinstance(b, dict) and isinstance(b.get("text"), str) and MARKER in b["text"]
                   for b in system)
    return False


def _is_user_prompt(message) -> bool:
    """A message the user typed, as opposed to tool results sent back."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    return any(isinstance(b, dict) and b.get("type") == "text" for b in content)


def _with_reminder(message: dict, text: str) -> dict:
    content = message["content"]
    if isinstance(content, str):
        if REMINDER_MARKER in content:
            return message
        blocks = [{"type": "text", "text": content}]
    else:
        if any(isinstance(b, dict) and isinstance(b.get("text"), str) and REMINDER_MARKER in b["text"]
               for b in content):
            return message
        blocks = list(content)
    return {**message, "content": [*blocks, {"type": "text", "text": text}]}


def apply(body: dict, level: str) -> tuple[dict, bool]:
    """Returns a NEW body with the instruction appended to its system prompt
    and the reminder added to each user-typed message, and whether anything
    was added. Never mutates the input: a failover re-sends the original to
    another account."""
    text = instruction(level)
    if text is None or not isinstance(body, dict):
        return body, False
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return body, False            # a helper call, not an agent turn
    system = body.get("system")
    if _already_applied(system):
        return body, False
    block = {"type": "text", "text": text}
    if system is None or system == "" or system == []:
        new_system = [block]
    elif isinstance(system, str):
        new_system = [{"type": "text", "text": system}, block]
    elif isinstance(system, list):
        new_system = [*system, block]
    else:
        return body, False            # a shape we do not understand: leave it alone
    new_body = {**body, "system": new_system}
    messages = body.get("messages")
    if isinstance(messages, list):
        nudge = reminder(level)
        new_body["messages"] = [_with_reminder(m, nudge) if _is_user_prompt(m) else m for m in messages]
    return new_body, True
