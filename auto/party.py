"""Party-mode result helpers — pure logic for round counting and consensus
detection.

Kept free of Telegram/CliRunner concerns so the heuristics can be unit-tested
in isolation. The Telegram-aware caller lives in `auto/executor.py` and the
runner glue lives in `auto/runner.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Match round headers like "## Round 1", "### Round 12", "**Round 3**".
# Anchored at the start of a stripped line; case-insensitive so the bmad
# party-mode skill's various output styles all match.
_ROUND_HEADER_PATTERNS = (
    re.compile(r"^#+\s*round\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"^\*+\s*round\s+(\d+)\s*\*+", re.IGNORECASE),
)

# Horizontal rule separators between agent turns. Used as a fallback when
# the skill emits no explicit round headers — every round is bracketed by
# at least two separators so we divide the count by two.
_SEPARATOR_PATTERN = re.compile(r"^-{3,}\s*$")

# Phrases that signal the agents have landed on a recommendation.
# Matched as case-insensitive substrings, NOT word boundaries — that way
# variants like "consensus:", "we agree on", "final recommendation —"
# all hit. Order matters slightly: longer/more specific phrases first
# so the extracted summary starts at the most informative point.
_CONSENSUS_PHRASES: tuple[str, ...] = (
    "final recommendation",
    "agreed recommendation",
    "consensus reached",
    "consensus:",
    "all agents agree",
    "we all agree",
    "we agree",
    "unanimous",
    "recommendation:",
)

# Maximum chars to keep from the consensus summary. Long enough to be
# useful, short enough to fit comfortably in a Telegram inline-keyboard
# header without truncating buttons.
_CONSENSUS_SUMMARY_CAP = 500


@dataclass
class PartyModeResult:
    """Outcome of a party-mode invocation.

    Attributes:
        rounds: Actual rounds detected (always >= the caller's `min_rounds`).
        consensus: True when output contained a recognized consensus phrase
            after at least 2 rounds.
        consensus_summary: The text that immediately follows the consensus
            phrase, trimmed and capped at 500 chars. Empty when
            `consensus` is False.
    """

    rounds: int
    consensus: bool = False
    consensus_summary: str = ""


def count_rounds(output: str, *, min_rounds: int = 0) -> int:
    """Count rounds emitted in `output`. Returns at least `min_rounds`.

    Strategy:
      1. Scan for explicit round headers (most reliable). Take the
         maximum round number observed — handles outputs that re-state
         "Round N" in nested contexts without inflating the count.
      2. If no header markers are present, fall back to counting `---`
         horizontal-rule separators and dividing by two (each round is
         typically wrapped in a leading + trailing separator).
      3. Floor the final count at `min_rounds` so callers can trust the
         skill honored the minimum even when its output is terse.
    """
    if not output:
        return max(0, min_rounds)

    detected = 0
    separators = 0
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched_header = False
        for pattern in _ROUND_HEADER_PATTERNS:
            m = pattern.match(line)
            if m:
                try:
                    detected = max(detected, int(m.group(1)))
                except ValueError:
                    pass
                matched_header = True
                break
        if matched_header:
            continue
        if _SEPARATOR_PATTERN.match(line):
            separators += 1

    if detected == 0 and separators >= 2:
        detected = separators // 2

    return max(detected, max(0, min_rounds))


def detect_consensus(
    output: str, *, rounds_completed: int,
) -> tuple[bool, str]:
    """Detect whether `output` contains a consensus signal.

    Consensus is only recognized after at least 2 rounds — anything
    earlier is treated as warm-up chatter and ignored. Returns
    `(found, summary)`. The summary is the text immediately following
    the matched phrase, trimmed of leading punctuation/whitespace and
    cut at the next major structural break (`\\n##`, `\\n---`, blank
    line) or 500 chars, whichever comes first.
    """
    if rounds_completed < 2 or not output:
        return False, ""

    lower = output.lower()
    best_idx = -1
    best_phrase_len = 0
    for phrase in _CONSENSUS_PHRASES:
        idx = lower.find(phrase)
        if idx == -1:
            continue
        # Prefer the earliest occurrence; among ties, the longer phrase
        # gives a cleaner summary start (skips the keyword itself).
        if best_idx == -1 or idx < best_idx or (
            idx == best_idx and len(phrase) > best_phrase_len
        ):
            best_idx = idx
            best_phrase_len = len(phrase)

    if best_idx == -1:
        return False, ""

    start = best_idx + best_phrase_len
    summary = output[start : start + _CONSENSUS_SUMMARY_CAP * 2]
    # Strip leading punctuation/whitespace that typically separates the
    # phrase from the summary text ("consensus: <summary>" or
    # "consensus reached.\n\n<summary>").
    summary = summary.lstrip(":.\n\t -—")

    # Trim at the next structural break so we don't bleed into the next
    # section of the skill's output.
    cuts: list[int] = []
    for sep in ("\n##", "\n---", "\n\n\n"):
        i = summary.find(sep)
        if i > 0:
            cuts.append(i)
    if cuts:
        summary = summary[: min(cuts)]

    summary = summary.strip()
    if len(summary) > _CONSENSUS_SUMMARY_CAP:
        summary = summary[:_CONSENSUS_SUMMARY_CAP].rstrip() + "…"

    return True, summary


__all__ = [
    "PartyModeResult",
    "count_rounds",
    "detect_consensus",
]
