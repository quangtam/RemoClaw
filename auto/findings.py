"""Heuristic detection of review findings in CLI output.

When a code-review skill runs, its output may or may not contain real
findings/issues. We want to divert to party-mode only when there's
something to actually discuss.

This is intentionally simple — false positives are fine (we'll just spawn
a low-stakes party-mode round), false negatives are the failure mode to
avoid (missing real issues).
"""

from __future__ import annotations

import re

# Phrases that strongly indicate findings exist
_FINDING_PHRASES = [
    r"\bissue(?:s)?\s+found\b",
    r"\bfindings?\s*:",
    r"\b(?:critical|major|minor|blocker|high|medium|low)\s+(?:issue|finding|severity)s?\b",
    r"\bvulnerab(?:le|ility)\b",
    r"\bsecurity\s+(?:issue|concern|risk)\b",
    r"\bmust\s+fix\b",
    r"\bshould\s+(?:fix|address|investigate)\b",
    r"\bblocker(?:s)?\b",
    r"\b(?:bug|defect|problem)(?:s)?\s+(?:identified|detected|found)\b",
    r"\bnot\s+ready\b",
    r"\bfailed\s+(?:check|review|validation)\b",
]

# Emoji/symbol-based finding indicators (matched separately to avoid
# Unicode word-boundary issues in combined regex)
_FINDING_SYMBOLS = ["✗", "❌", "🔴", "⚠️"]

# Phrases that strongly indicate NO findings (override matches above)
_CLEAN_PHRASES = [
    r"\bno\s+(?:issues?|findings?|blockers?)\s+(?:found|identified|detected)\b",
    r"\b(?:approved|ready\s+to\s+(?:ship|merge))\b",
    r"\b(?:all|everything)\s+(?:looks?|is)\s+(?:good|clean|ok)\b",
    r"\b✅\s+(?:approved|clean|ready|passed)\b",
    r"\bclean\s+(?:bill\s+of\s+health|review)\b",
]


_FINDING_RE = re.compile("|".join(_FINDING_PHRASES), re.IGNORECASE)
_CLEAN_RE = re.compile("|".join(_CLEAN_PHRASES), re.IGNORECASE)


def has_findings(text: str) -> bool:
    """Return True if `text` looks like it contains review findings.

    Logic:
        1. If a clean-bill-of-health phrase appears → no findings
        2. If any finding phrase OR symbol appears → has findings
        3. Otherwise → no findings (default optimistic)
    """
    if not text:
        return False

    # Clean signals override — if review explicitly says it's clean, trust it
    if _CLEAN_RE.search(text):
        return False

    if _FINDING_RE.search(text):
        return True

    # Check finding symbols separately — Unicode + alternation in regex
    # can be flaky, so we use a simple substring check.
    return any(symbol in text for symbol in _FINDING_SYMBOLS)


def extract_findings_summary(text: str, max_chars: int = 1500) -> str:
    """Pull a short context blurb to feed party-mode discussion.

    Strategy: grab the lines around the first finding match plus a few
    surrounding lines — enough to give agents something to react to,
    short enough to fit in a subagent prompt.
    """
    if not text:
        return ""

    match = _FINDING_RE.search(text)
    if not match:
        # No specific match — return the tail of the output
        return text[-max_chars:].strip()

    # Find the line containing the match
    start = text.rfind("\n", 0, match.start()) + 1
    # Get up to 30 lines after the match
    lines_ahead = 0
    end = match.end()
    while lines_ahead < 30 and end < len(text):
        nl = text.find("\n", end + 1)
        if nl == -1:
            end = len(text)
            break
        end = nl
        lines_ahead += 1

    snippet = text[start:end].strip()
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rsplit("\n", 1)[0] + "\n…(truncated)"
    return snippet
