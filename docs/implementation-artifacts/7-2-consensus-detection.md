# Story 7.2: Consensus Detection & Round Counting

## Story

**As a** autonomous mode runner,
**I want** reliable detection of consensus and accurate round counting from party-mode output,
**So that** the system can make correct decisions about when to auto-accept (YOLO) or present gates (AUTO) after multi-agent debate.

## Status

**Status:** ready-for-dev
**Story ID:** 7.2
**Epic:** 7 — Autonomous Mode: Party-Mode Integration (Sprint D)
**Sprint:** D
**Depends on:** Story 7.1 (creates `auto/party.py` with `PartyModeResult` dataclass and basic structure)

## Business Context

Story 7-1 establishes the `auto/party.py` module, `PartyModeResult` dataclass, and wires the executor/runner protocol change. This story focuses on making the consensus detection and round counting **robust and well-tested** — the pure logic that determines whether party-mode reached agreement.

The `bmad-party-mode` skill produces structured multi-agent debate output. This story implements the heuristics that parse that output to:
1. Count how many debate rounds actually occurred
2. Detect whether agents reached consensus
3. Extract a summary of the consensus for user presentation

Getting this wrong means either:
- **False positive consensus:** Auto-accepting when agents disagree → shipping bad code
- **False negative consensus:** Never detecting agreement → always requiring manual intervention, defeating the purpose of autonomous mode
- **Wrong round count:** Misreporting progress → confusing UX in `/auto status`

## Acceptance Criteria (BDD)

### AC1: Round counting with markdown headers

**Given** party-mode output contains lines like "## Round 1", "## Round 2", "## Round 3"
**When** `count_rounds(output)` is called
**Then** it returns 3 (the highest round number found)

### AC2: Round counting with bold markers

**Given** party-mode output contains "**Round 1**", "**Round 2**"
**When** `count_rounds(output)` is called
**Then** it returns 2

### AC3: Round counting with mixed formats

**Given** party-mode output uses inconsistent formatting ("## Round 1" then "**Round 2**" then "Round 3:")
**When** `count_rounds(output)` is called
**Then** it returns 3 (detects all common formats)

### AC4: Round counting floor at min_rounds

**Given** party-mode output has no detectable round markers (unstructured text)
**When** `count_rounds(output, min_rounds=2)` is called
**Then** it returns 2 (trusts the skill honored min_rounds even if markers aren't parseable)

### AC5: Round counting with agent name headers

**Given** party-mode output uses agent names as headers ("## Winston (Architect)", "## Mary (Analyst)")
**When** `count_rounds(output)` is called with these as the only headers
**Then** it counts agent turns and divides by the number of unique agents to estimate rounds
**Or** falls back to `min_rounds` if the heuristic is ambiguous

### AC6: Consensus detection — explicit phrases

**Given** party-mode output contains "**Consensus reached:**" followed by a recommendation
**When** `detect_consensus(output, rounds=3)` is called
**Then** it returns `ConsensusResult(detected=True, summary="<text after the phrase>")` with summary capped at 500 chars

### AC7: Consensus detection — "all agree" variant

**Given** party-mode output contains "All agents agree that the implementation should..."
**When** `detect_consensus(output, rounds=2)` is called
**Then** it returns `ConsensusResult(detected=True, summary="the implementation should...")`

### AC8: Consensus detection — "final recommendation" variant

**Given** party-mode output ends with "## Final Recommendation\n\nThe team recommends..."
**When** `detect_consensus(output, rounds=2)` is called
**Then** it returns `ConsensusResult(detected=True, summary="The team recommends...")`

### AC9: Consensus NOT detected when rounds < 2

**Given** party-mode output contains "consensus reached" but only 1 round was detected
**When** `detect_consensus(output, rounds=1)` is called
**Then** it returns `ConsensusResult(detected=False)` — consensus requires at least 2 rounds of debate

### AC10: Consensus NOT detected — disagreement phrases

**Given** party-mode output contains "no consensus" or "agents disagree" or "unresolved"
**When** `detect_consensus(output, rounds=3)` is called
**Then** it returns `ConsensusResult(detected=False)` — explicit disagreement overrides any consensus phrases

### AC11: Consensus NOT detected — no matching phrases

**Given** party-mode output is a normal discussion without any consensus/agreement language
**When** `detect_consensus(output, rounds=2)` is called
**Then** it returns `ConsensusResult(detected=False)`

### AC12: Consensus summary extraction — paragraph after phrase

**Given** consensus phrase "Consensus reached:" appears on a line
**When** the summary is extracted
**Then** it includes text from that line through the next blank line (or end of text), up to 500 chars
**And** leading/trailing whitespace is stripped

### AC13: Consensus summary extraction — truncation

**Given** the text after a consensus phrase is longer than 500 characters
**When** the summary is extracted
**Then** it is truncated at the last complete sentence boundary before 500 chars
**And** "…" is appended

### AC14: Empty or whitespace-only input

**Given** `count_rounds("")` or `detect_consensus("", rounds=2)` is called
**When** the input is empty or whitespace
**Then** `count_rounds` returns `min_rounds` (default 0)
**And** `detect_consensus` returns `ConsensusResult(detected=False)`

## Technical Requirements

### File to CREATE

| File | Purpose |
|------|---------|
| `auto/party.py` | `PartyModeResult`, `ConsensusResult`, `count_rounds()`, `detect_consensus()` |
| `tests/test_auto_party.py` | Comprehensive tests for all detection heuristics |

### IMPORTANT: Relationship to Story 7-1

Story 7-1 creates `auto/party.py` with the `PartyModeResult` dataclass and basic wiring. This story **extends** that file with the full detection logic. If 7-1 has already been implemented, ADD to the existing file. If 7-1 has NOT been implemented yet, create the file fresh with everything needed.

The `PartyModeResult` dataclass (from 7-1's design):

```python
@dataclass
class PartyModeResult:
    rounds: int                    # actual rounds detected (>= min_rounds)
    consensus: bool                # whether consensus was detected
    consensus_summary: str = ""    # extracted summary text (up to 500 chars)
```

This story adds:

```python
@dataclass
class ConsensusResult:
    detected: bool
    summary: str = ""

def count_rounds(output: str, min_rounds: int = 0) -> int: ...
def detect_consensus(output: str, rounds: int) -> ConsensusResult: ...
```

The executor (story 7-1) calls these functions after streaming completes:

```python
# In executor.run_party_mode(), after collecting all output:
from auto.party import count_rounds, detect_consensus
rounds = count_rounds(output, min_rounds=min_rounds)
consensus = detect_consensus(output, rounds=rounds)
return PartyModeResult(
    rounds=rounds,
    consensus=consensus.detected,
    consensus_summary=consensus.summary,
)
```

## Developer Context & Guardrails

### Pattern: Follow `auto/findings.py` exactly

This story's detection logic is architecturally identical to `auto/findings.py`:
- Pure functions, no I/O, no async
- Regex-based heuristics with explicit phrase lists
- "Override" patterns (disagreement phrases override consensus phrases, just like clean phrases override finding phrases)
- Defensive: empty input → safe default
- Testable with simple string inputs

**DO NOT** introduce any I/O, async, or Telegram dependencies in `auto/party.py`. It must remain pure and testable.

### Round Detection Patterns

The `bmad-party-mode` skill output varies by LLM and configuration. Observed formats:

```text
## Round 1

**Winston (Architect):** I think we should...

**Mary (Analyst):** I agree, but also consider...

## Round 2

**Winston (Architect):** Good point. Let me revise...

**Mary (Analyst):** That addresses my concern. I recommend...

## Final Recommendation

Consensus reached: The team recommends implementing Option B with...
```

Alternative format (some LLMs):

```text
**Round 1**

Winston: ...
Mary: ...

---

**Round 2**

Winston: ...
Mary: ...

---

**Consensus:** All agents agree that...
```

### Consensus Phrase Patterns (ordered by confidence)

```python
# HIGH confidence — explicit consensus language
_CONSENSUS_PHRASES_HIGH = [
    r"\bconsensus\s+reached\b",
    r"\ball\s+agents?\s+agree\b",
    r"\bunanimous(?:ly)?\b",
    r"\bwe\s+(?:all\s+)?agree\b",
    r"\bagreed\s+recommendation\b",
]

# MEDIUM confidence — recommendation language (only counts if no disagreement)
_CONSENSUS_PHRASES_MEDIUM = [
    r"\bfinal\s+recommendation\b",
    r"\bteam\s+recommends?\b",
    r"\bconsensus:\s*",
    r"\brecommendation:\s*",
]

# DISAGREEMENT — overrides any consensus detection
_DISAGREEMENT_PHRASES = [
    r"\bno\s+consensus\b",
    r"\bagents?\s+disagree\b",
    r"\bunresolved\b",
    r"\bcannot\s+(?:reach|find)\s+(?:consensus|agreement)\b",
    r"\bsplit\s+(?:decision|opinion)\b",
    r"\bno\s+agreement\b",
]
```

### Summary Extraction Strategy

When a consensus phrase is found:
1. Find the line containing the match
2. If the phrase is followed by `:` or `—` on the same line, extract text after it
3. Otherwise, take the next paragraph (text until double newline or end)
4. Strip markdown formatting (`**`, `##`, etc.) from the summary
5. Cap at 500 chars, truncating at sentence boundary if possible

### Edge Cases to Handle

1. **Multiple consensus phrases** — use the LAST one (final conclusion after debate)
2. **Consensus phrase inside a quote/code block** — ignore (it's being discussed, not declared)
3. **"Consensus" as a topic being discussed** — "We need to reach consensus" is NOT a detection; require the phrase to be declarative (followed by `:`, or preceded by "reached"/"achieved")
4. **Unicode/emoji in output** — agent names may include emoji; don't let that break regex
5. **Very long output (>50KB)** — only scan the last 10KB for consensus (it's always at the end)

### Architecture Compliance

- **Module:** `auto/party.py` — pure functions, no side effects
- **No new dependencies** — only `re` and `dataclasses` from stdlib
- **Naming:** Follow `auto/findings.py` pattern — module-level compiled regex, public functions with docstrings
- **Type hints:** All functions fully typed with return annotations
- **Docstrings:** Google-style, one-line summary + details for complex functions

### Files Being Modified — Current State

**`auto/party.py`** (may or may not exist depending on 7-1 status):
- If exists: contains `PartyModeResult` dataclass, possibly stub `count_rounds`/`detect_consensus`
- If not exists: create fresh with all content

**No other files are modified by this story.** The executor/runner integration is story 7-1's responsibility.

## Testing Requirements

### Test File: `tests/test_auto_party.py`

All tests are synchronous (pure functions, no async needed).

```python
import pytest
from auto.party import count_rounds, detect_consensus, ConsensusResult
```

**Round counting tests:**

1. `test_count_rounds_markdown_headers` — "## Round 1\n...\n## Round 2\n..." → 2
2. `test_count_rounds_bold_markers` — "**Round 1**\n...\n**Round 2**" → 2
3. `test_count_rounds_colon_format` — "Round 1:\n...\nRound 2:" → 2
4. `test_count_rounds_mixed_formats` — mix of ##, **, plain → highest number
5. `test_count_rounds_no_markers_returns_min` — unstructured text, min_rounds=2 → 2
6. `test_count_rounds_empty_input` — "" → 0 (or min_rounds if provided)
7. `test_count_rounds_high_number` — "## Round 5" with no earlier rounds → 5 (trust the number)

**Consensus detection tests:**

8. `test_consensus_explicit_phrase` — "Consensus reached: do X" → detected=True, summary="do X"
9. `test_consensus_all_agree` — "All agents agree that..." → detected=True
10. `test_consensus_final_recommendation` — "## Final Recommendation\n\nDo Y" → detected=True, summary="Do Y"
11. `test_consensus_not_detected_no_phrases` — normal discussion → detected=False
12. `test_consensus_not_detected_round_1_only` — consensus phrase but rounds=1 → detected=False
13. `test_consensus_overridden_by_disagreement` — "consensus reached" + "no consensus" later → detected=False
14. `test_consensus_summary_capped_at_500` — long text after phrase → truncated with "…"
15. `test_consensus_summary_strips_markdown` — "**bold** text" → "bold text"
16. `test_consensus_empty_input` — "" → detected=False
17. `test_consensus_uses_last_occurrence` — multiple "consensus:" → uses the last one

**Edge case tests:**

18. `test_count_rounds_agent_turns_heuristic` — agent name headers without "Round N" → reasonable estimate
19. `test_consensus_inside_code_block_ignored` — ````consensus reached```` → not detected
20. `test_consensus_large_output_scans_tail` — 60KB output, consensus at end → still detected

## Previous Story Intelligence

### From Story 7-1 (predecessor)

Story 7-1 establishes:
- `PartyModeResult` dataclass in `auto/party.py`
- The executor protocol change (`run_party_mode` → returns `PartyModeResult`)
- The runner's handling of `PartyModeResult` (YOLO auto-accept vs AUTO pause)
- Basic stubs for `count_rounds()` and `detect_consensus()`

This story replaces those stubs with production-quality implementations.

### From `auto/findings.py` (architectural sibling)

The findings detection module is the closest architectural analog:
- Same pattern: compiled regex at module level, public function with simple interface
- Same philosophy: false positives acceptable, false negatives are the failure mode
- Same testing style: pure function tests with string inputs

**Key difference:** Findings detection is binary (has/doesn't have). Consensus detection is richer — it extracts a summary and respects round count as a precondition.

### Git Intelligence

Recent commits show the auto module follows a consistent pattern:
- Pure logic in dedicated modules (`findings.py`, `flow.py`, `state.py`)
- I/O and Telegram in `executor.py`
- State machine in `runner.py`
- Tests mirror source: `test_auto_findings.py`, `test_auto_flow.py`, etc.

## File Structure

```
auto/
├── party.py             # THIS STORY: PartyModeResult, ConsensusResult, count_rounds(), detect_consensus()
└── findings.py          # REFERENCE: architectural sibling — same pattern to follow

tests/
└── test_auto_party.py   # THIS STORY: 20 tests covering all heuristics
```

## Implementation Notes

### Suggested Implementation Order

1. Define `ConsensusResult` dataclass
2. Implement `count_rounds()` with compiled regex patterns
3. Implement `detect_consensus()` with phrase matching + disagreement override
4. Implement `_extract_summary()` helper for text extraction after consensus phrase
5. Write all tests
6. Verify edge cases pass

### Performance Consideration

Party-mode output can be large (10-50KB for multi-round debates). The detection functions should:
- Compile regex at module level (not per-call)
- For consensus detection on large output, only scan the last 10KB (consensus is always declared at the end)
- Use `re.MULTILINE` for `^` anchored patterns (round headers)

## Completion Checklist

- [ ] `auto/party.py` contains `ConsensusResult` dataclass
- [ ] `count_rounds()` handles all marker formats (##, **, plain, colon)
- [ ] `count_rounds()` floors at `min_rounds` when markers aren't found
- [ ] `detect_consensus()` detects high-confidence phrases
- [ ] `detect_consensus()` detects medium-confidence phrases
- [ ] `detect_consensus()` respects disagreement overrides
- [ ] `detect_consensus()` requires rounds >= 2
- [ ] `_extract_summary()` caps at 500 chars with sentence-boundary truncation
- [ ] `tests/test_auto_party.py` has 20+ tests covering all ACs
- [ ] All tests pass (`pytest tests/test_auto_party.py`)
- [ ] No async, no I/O, no Telegram imports in `auto/party.py`

---

*Ultimate context engine analysis completed — comprehensive developer guide created*
