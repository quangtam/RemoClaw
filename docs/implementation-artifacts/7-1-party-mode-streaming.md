# Story 7.1: Party-Mode Streaming & Round Tracking

## Story

**As a** user running an autonomous flow,
**I want** party-mode debates to stream to Telegram in real-time with visible round tracking,
**So that** I can follow the multi-agent discussion as it happens and know when consensus is reached.

## Status

**Status:** in-progress
**Story ID:** 7.1
**Epic:** 7 — Autonomous Mode: Party-Mode Integration (Sprint D)
**Sprint:** D
**Estimated:** ~300 LOC + 10 tests
**Baseline commit:** 9a39a7953fee38ebba1533d7456eac6ea55149dd

## Business Context

Party-mode is triggered when a code-review step surfaces findings (issues/blockers). The `AutoExecutor.run_party_mode()` method already exists and streams the `bmad-party-mode` skill output to Telegram. However, it currently:
1. Doesn't track individual rounds (just trusts `min_rounds` was honored)
2. Doesn't detect consensus in the output
3. Doesn't give the user control after consensus is reached (AUTO mode)
4. Doesn't auto-accept consensus (YOLO mode)

This story wires the full party-mode integration: real round counting from output, consensus detection, and mode-appropriate gate behavior after the debate concludes.

## Acceptance Criteria (BDD)

### AC1: Round counting from streamed output

**Given** a code-review step produces findings and `on_findings: party-mode` is configured
**When** `run_party_mode` streams the `bmad-party-mode` skill output
**Then** the executor counts rounds by detecting round markers in the output (e.g., "## Round 1", "## Round 2", "---" separators between agent turns, or "Round N" headers)
**And** the returned round count reflects the actual number of rounds observed (not just `min_rounds`)

### AC2: Minimum 2 rounds enforced

**Given** party-mode is triggered with `min_rounds=2`
**When** the skill produces output with only 1 round marker detected
**Then** the round count returned is still at least `min_rounds` (trust the skill honored it)
**And** no error is raised — the skill is authoritative on round execution

### AC3: Consensus detection after round 2+

**Given** party-mode output has completed at least 2 rounds
**When** the output contains consensus phrases ("consensus reached", "all agents agree", "unanimous", "we agree", "recommendation:", "final recommendation")
**Then** `run_party_mode` returns a `PartyModeResult` with `consensus=True` and `consensus_summary` extracted from the output
**And** the consensus summary is the text following the consensus phrase (up to 500 chars)

### AC4: No consensus detected

**Given** party-mode output has completed 2+ rounds
**When** no consensus phrases are found in the output
**Then** `run_party_mode` returns `PartyModeResult` with `consensus=False`
**And** the flow continues normally (no special gate behavior)

### AC5: YOLO mode auto-accepts consensus

**Given** the run is in YOLO mode
**When** party-mode returns with `consensus=True`
**Then** the runner auto-accepts the consensus and continues to the next step
**And** a notification is sent: "🤝 Party-mode consensus reached (auto-accepted in yolo mode)"
**And** no user interaction is required

### AC6: AUTO mode presents consensus for user decision

**Given** the run is in AUTO mode
**When** party-mode returns with `consensus=True`
**Then** the executor sends a message with the consensus summary and an inline keyboard:
  - [✅ Accept & Continue] — accepts consensus, advances to next step
  - [🔄 More Rounds] — re-runs party-mode with the same context
  - [✋ Override & Abort] — aborts the run
**And** the run pauses at `STATUS_PAUSED_PARTY` until the user responds

### AC7: AUTO mode "More Rounds" re-triggers party-mode

**Given** the user clicks [🔄 More Rounds] on a party-mode consensus gate
**When** the decision is processed
**Then** `run_party_mode` is called again with the same context
**And** the new output is streamed to Telegram
**And** consensus detection runs again on the combined/new output

### AC8: Party-mode with no findings skips entirely

**Given** a code-review step completes with `findings=False`
**When** `on_findings: party-mode` is configured on the step
**Then** party-mode is NOT triggered (existing behavior — regression guard)
**And** the step proceeds normally through its gate

### AC9: Streaming preview shows round progress

**Given** party-mode is streaming output to Telegram
**When** a new round marker is detected in the stream
**Then** the streaming preview header updates to show: "⚙️ Running bmad-party-mode (Round N)"
**And** the user can see which round is currently active

### AC10: Party-mode crash is non-fatal

**Given** party-mode is triggered
**When** the `bmad-party-mode` skill crashes or times out
**Then** `run_party_mode` returns `PartyModeResult(rounds=0, consensus=False)`
**And** the step is still recorded as successful (findings were detected, party-mode was attempted)
**And** the flow continues past the gate normally (degraded but not blocked)

## Technical Requirements

### Files to CREATE (new)

| File | Purpose |
|------|---------|
| `auto/party.py` | `PartyModeResult` dataclass + `detect_consensus()` + `count_rounds()` pure functions |
| `tests/test_auto_party.py` | Unit tests for consensus detection, round counting, and runner integration |

### Files to UPDATE (existing)

| File | Changes |
|------|---------|
| `auto/executor.py` | Refactor `run_party_mode()` to use `auto/party.py` functions, return `PartyModeResult`, handle consensus gate UX |
| `auto/runner.py` | Update party-mode divert section to handle `PartyModeResult`, implement YOLO auto-accept vs AUTO pause logic |
| `auto/state.py` | Add `STATUS_PAUSED_PARTY` handling in `is_paused()` (already exists but unused) |
| `auto/__init__.py` | Export `PartyModeResult` if needed |

### Architecture Compliance

- **Module boundary:** Pure logic (consensus detection, round counting) lives in `auto/party.py`. Telegram UX (inline keyboards, message formatting) stays in `auto/executor.py`.
- **Executor protocol:** `run_party_mode` return type changes from `int` (round count) to `PartyModeResult` dataclass. This is a breaking change to the protocol — update `Executor` protocol in `runner.py` and the `FakeExecutor` in tests.
- **State machine:** `STATUS_PAUSED_PARTY` already exists in `auto/state.py` but is never set. This story activates it for AUTO mode consensus gates.
- **Callback data format:** Follow existing pattern: `auto:party-accept:{step_id}`, `auto:party-more:{step_id}`, `auto:party-abort:{step_id}`

## Developer Context & Guardrails

### CRITICAL: Existing Behavior to Preserve

1. **`run_party_mode` already streams output** — Sprint B/C already wired streaming via `runner_ref.execute_stream()`. Do NOT rewrite the streaming logic. Only ADD round counting and consensus detection on top.

2. **`has_findings()` in `auto/findings.py` gates party-mode entry** — The runner only calls `run_party_mode` when `result.findings == True AND step.on_findings == "party-mode"`. Do NOT change this gating logic.

3. **`StepRecord.party_mode_rounds` already persists** — The history record already stores round count. Just make sure the new `PartyModeResult.rounds` value flows into it correctly.

4. **`STATUS_PAUSED_PARTY` already defined** — It's in `auto/state.py` and `is_paused()` already checks for it. The `resume()` method already handles it (same as `STATUS_PAUSED_GATE`). Just SET it when appropriate.

5. **Callback handler pattern** — `remoclaw.py` already has `auto:approve:{step_id}` and `auto:abort:{step_id}` handlers that call `resolve_pending_decision()`. Follow the same pattern for party-mode buttons.

### Current `run_party_mode` Implementation (executor.py)

```python
async def run_party_mode(self, *, thread_id, context, min_rounds, timeout_seconds=None) -> int:
    # Sends "bmad-party-mode" skill with findings as input
    # Streams output to Telegram via progressive edits
    # Returns min_rounds (trusts skill honored it)
    # On crash: returns 0
```

**What to change:**
- Return `PartyModeResult` instead of `int`
- After streaming completes, run `count_rounds(output)` and `detect_consensus(output)`
- If consensus detected AND mode is AUTO: send consensus gate keyboard, return result
- If consensus detected AND mode is YOLO: send notification, return result

### Current Runner Party-Mode Section (runner.py lines ~220-230)

```python
# ── Findings → party-mode divert ─────────────────────────
rounds = 0
if result.findings and current.on_findings == "party-mode":
    min_rounds = 2
    rounds = await self.executor.run_party_mode(
        thread_id=self.state.thread_id,
        context=result.notes or f"findings from {current.id}",
        min_rounds=min_rounds,
        timeout_seconds=self.flow.default_step_timeout_seconds,
    )
```

**What to change:**
- `rounds` becomes `party_result: PartyModeResult`
- Extract `rounds = party_result.rounds`
- If `party_result.consensus` and mode is AUTO → set `STATUS_PAUSED_PARTY`, return `RunStatus.PAUSED`
- If `party_result.consensus` and mode is YOLO → continue (auto-accept)
- Record `party_mode_rounds` from `party_result.rounds` in the step record

### Consensus Detection Strategy

The `bmad-party-mode` skill produces structured output with round markers. Detection should be:

```python
# Round markers (case-insensitive)
ROUND_PATTERNS = [
    r"^##\s*Round\s+(\d+)",        # "## Round 1", "## Round 2"
    r"^---+$",                      # "---" separators between turns (count / 2)
    r"^\*\*Round\s+(\d+)\*\*",     # "**Round 1**"
]

# Consensus phrases (case-insensitive, after round 2+)
CONSENSUS_PHRASES = [
    "consensus reached",
    "all agents agree",
    "unanimous",
    "we all agree",
    "final recommendation",
    "agreed recommendation",
    "consensus:",
]
```

### Executor Protocol Change

The `Executor.run_party_mode` signature changes:

```python
# BEFORE (current)
async def run_party_mode(self, *, thread_id, context, min_rounds, timeout_seconds=None) -> int:

# AFTER (this story)
async def run_party_mode(self, *, thread_id, context, min_rounds, timeout_seconds=None) -> "PartyModeResult":
```

The `PartyModeResult` dataclass:

```python
@dataclass
class PartyModeResult:
    rounds: int                    # actual rounds detected (>= min_rounds)
    consensus: bool                # whether consensus was detected
    consensus_summary: str = ""    # extracted summary text (up to 500 chars)
```

### Callback Data for Party-Mode Gate

Follow existing pattern in `remoclaw.py`:

```python
# Existing pattern:
callback_data=f"auto:approve:{step.id}"
callback_data=f"auto:abort:{step.id}"

# New for party-mode:
callback_data=f"auto:party-accept:{step_id}"
callback_data=f"auto:party-more:{step_id}"
callback_data=f"auto:party-abort:{step_id}"
```

Handle in the same callback query handler that processes `auto:approve` / `auto:abort`.

### "More Rounds" Implementation

When user clicks "More Rounds":
1. Resolve the pending decision as `True` (so the runner unpauses)
2. But DON'T advance — instead, re-run party-mode with the same context
3. This requires the runner to know it's a "more rounds" request vs a normal resume

**Suggested approach:** Use a separate future key or a flag on the state:
- `resolve_pending_decision(thread_id, step_id, approved=True)` for Accept
- For "More Rounds": set `state.party_mode_more_rounds = True` before resolving
- Runner checks this flag after resume and re-invokes party-mode if set

Alternatively (simpler): treat "More Rounds" as a special callback that directly calls `executor.run_party_mode()` again from the callback handler, then re-presents the gate. This avoids state machine complexity.

**Recommended: the simpler approach** — handle "More Rounds" entirely in the executor/callback layer without touching the runner state machine. The callback handler:
1. Calls `run_party_mode()` again
2. If new consensus → re-present the gate keyboard
3. If no consensus → auto-continue (party-mode did its best)

## File Structure Requirements

```
auto/
├── __init__.py          # UPDATE: export PartyModeResult
├── party.py             # NEW: PartyModeResult, detect_consensus(), count_rounds()
├── executor.py          # UPDATE: refactor run_party_mode return type + consensus gate
├── runner.py            # UPDATE: handle PartyModeResult, YOLO vs AUTO logic
├── state.py             # MINOR: no code changes needed (STATUS_PAUSED_PARTY exists)
├── driver.py            # NO CHANGES
├── flow.py              # NO CHANGES
└── findings.py          # NO CHANGES

tests/
└── test_auto_party.py   # NEW: 10+ tests
```

## Testing Requirements

### Test File: `tests/test_auto_party.py`

**Pure function tests (auto/party.py):**
1. `test_count_rounds_with_header_markers` — "## Round 1", "## Round 2" → 2
2. `test_count_rounds_with_separator_markers` — "---" separators → count/2
3. `test_count_rounds_returns_min_rounds_when_fewer_detected` — floor at min_rounds
4. `test_detect_consensus_finds_phrase` — "consensus reached" → True + summary
5. `test_detect_consensus_no_match` — random text → False
6. `test_detect_consensus_only_after_round_2` — consensus phrase in round 1 output → False

**Runner integration tests (using FakeExecutor):**
7. `test_party_mode_yolo_auto_accepts_consensus` — YOLO + consensus → CONTINUE
8. `test_party_mode_auto_pauses_on_consensus` — AUTO + consensus → PAUSED_PARTY
9. `test_party_mode_no_consensus_continues` — no consensus → CONTINUE (both modes)
10. `test_party_mode_crash_returns_zero_rounds` — exception → PartyModeResult(0, False)

### FakeExecutor Update

Update `FakeExecutor.run_party_mode` in `tests/test_auto_runner.py` to return `PartyModeResult` instead of `int`. This affects existing tests — ensure backward compatibility.

## Previous Story Intelligence

### From Sprint C (most recent)

- **Pattern:** All Sprint C features (loop expansion, skip_if_validated, skip_if_artifact) were implemented as pure logic in the runner with executor providing the I/O bridge. Follow the same pattern.
- **Testing:** FakeExecutor pattern works well. Add new fields to FakeExecutor for party-mode results.
- **State persistence:** Any new state fields need JSON serialization in `to_db_row()` / `from_db_row()`. `PartyModeResult` doesn't need persistence — it's transient within a single step execution.

### Git Intelligence

Recent commits show:
- `9a39a79` — fresh session marking for BMAD steps
- `16d3a9a` — live progress tracking (current_step_started_at, last_progress_line)
- `15f7259` — party-mode output already streams to Telegram

The streaming infrastructure is solid. This story builds ON TOP of it, not replacing it.

## Library & Framework Requirements

- **No new dependencies.** All functionality uses existing stdlib (`re`, `dataclasses`) and project deps.
- **`re` module** for consensus phrase detection and round counting patterns.
- **`telegram` library** (already imported in executor.py) for `InlineKeyboardButton`, `InlineKeyboardMarkup`.

## Project Context Reference

- Architecture: `/docs/planning-artifacts/architecture.md`
- Sprint tracker: `/docs/implementation-artifacts/autonomous-mode-sprints.md` (Sprint D section)
- Existing tests: `tests/test_auto_runner.py` (FakeExecutor pattern)
- Flow definitions: `_bmad/flows/full.yaml`, `_bmad/flows/quick-dev.yaml`

## Completion Checklist

- [x] `auto/party.py` created with `PartyModeResult`, `count_rounds()`, `detect_consensus()`
- [x] `auto/executor.py` updated: `run_party_mode()` returns `PartyModeResult`, consensus gate UX
- [x] `auto/runner.py` updated: handles `PartyModeResult`, YOLO auto-accept, AUTO pause
- [x] `remoclaw.py` updated: callback handlers for `auto:party-accept`, `auto:party-more`, `auto:party-abort`
- [x] `tests/test_auto_party.py` created with 10+ tests (15 tests: 6 round-counting, 5 consensus-detection, 4 runner integration)
- [x] Existing tests updated (FakeExecutor returns `PartyModeResult`; added `notify_party_yolo_accept` and `present_party_consensus_gate` for new integration tests)
- [x] All tests pass (`pytest`) — 491/491 passing, no regressions

---

*Ultimate context engine analysis completed — comprehensive developer guide created*
