# Epic 7 Context: Autonomous Mode — Party-Mode Integration (Sprint D)

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

When a code-review step in an autonomous run surfaces findings, the bot diverts
into a multi-agent "party-mode" debate that streams to Telegram in real-time.
The user follows the discussion as it happens, sees explicit round tracking,
and (after a minimum of two rounds) gets a consensus decision they can accept,
extend, or override — automatically in YOLO mode, with a gate keyboard in
AUTO mode. This closes the autonomous loop: instead of just halting on
findings, the bot orchestrates a structured debate and lands on a
recommendation.

## Stories

- Story 7.1: Party-Mode Streaming & Round Tracking
- Story 7.2: Consensus Detection (deeper heuristics, future)
- Story 7.3: Party-Mode Gate UX (polish, future)

## Requirements & Constraints

- Party-mode triggers ONLY when a code-review step has `findings == True` and
  the step's `on_findings` is set to `party-mode`. No findings → party-mode is
  skipped (regression guard).
- Skill output streams to Telegram via the same progressive-edit mechanism as
  normal skill execution. Streaming infrastructure must NOT be rewritten —
  only augmented with round detection and consensus extraction.
- The skill is authoritative on round execution. The runner counts rounds
  from observed output but trusts `min_rounds` (default 2) when fewer markers
  are detected — never errors on under-counting.
- Consensus detection runs only after round 2+. Phrases below round 2 are
  ignored to avoid premature exits on warm-up chatter.
- YOLO mode auto-accepts consensus and continues (single notification). AUTO
  mode pauses at `STATUS_PAUSED_PARTY` and presents an inline keyboard with
  three choices: Accept, More Rounds, Override (Abort).
- Party-mode failures (skill crash, timeout) are non-fatal: returns zero
  rounds, no consensus, run continues past the gate degraded but unblocked.
- No new dependencies. Detection uses stdlib `re` and `dataclasses`.

## Technical Decisions

- **Module boundary**: pure logic (consensus phrase matching, round
  counting, dataclasses) lives in `auto/party.py`. All Telegram UX
  (inline keyboards, message formatting, streaming) stays in
  `auto/executor.py`.
- **Executor protocol change**: `Executor.run_party_mode` returns a
  `PartyModeResult` dataclass instead of `int`. This is a breaking change
  to the protocol — `FakeExecutor` in tests must be updated.
- **State machine**: `STATUS_PAUSED_PARTY` already exists in
  `auto/state.py`; `is_paused()` and `resume()` already handle it. This
  epic activates it.
- **"More Rounds" implementation**: handled at the callback layer, not
  inside the runner state machine. The callback handler re-invokes
  `run_party_mode` with the same context, then re-presents the gate or
  auto-continues based on the new outcome.
- **Callback data convention**: `auto:party-accept:{step_id}`,
  `auto:party-more:{step_id}`, `auto:party-abort:{step_id}` —
  parsed by the existing `auto:` callback pattern handler.
- **State persistence**: `PartyModeResult` is transient and not persisted.
  Round count flows into the existing `StepRecord.party_mode_rounds`
  column.

## UX & Interaction Patterns

- Streaming preview header updates on each new round marker:
  `⚙️ Running bmad-party-mode (Round N)`. Users always know which round
  is currently active.
- AUTO mode consensus message: shows the extracted consensus summary plus
  three buttons. The run is visibly paused (`/auto status` shows
  `paused-party`) until the user responds.
- YOLO mode consensus message: a single non-interactive notification
  ("🤝 Party-mode consensus reached (auto-accepted in yolo mode)") and
  the run continues without waiting.
- "More Rounds" keeps the conversation alive: the same context is
  re-streamed and a new gate is presented when consensus is reached again.

## Cross-Story Dependencies

- Builds directly on Sprint A/B/C: requires `AutoExecutor`,
  `AutoRunner`, `Driver`, `findings.has_findings`, and the existing
  streaming path.
- The streaming infrastructure landed in Sprint B (`580989d`) and
  was extended for live progress in commits `9a39a79` and `16d3a9a`.
- Stories 7.2/7.3 build on 7.1's foundations; no upstream dependency
  on other epics.
