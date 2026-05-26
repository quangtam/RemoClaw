"""Tests for party-mode pure logic and runner integration (Story 7.1).

Covers:
  - count_rounds() — header markers, separator fallback, min_rounds floor.
  - detect_consensus() — phrase matching, summary extraction, round-2 gate.
  - AutoRunner integration — YOLO auto-accept, AUTO pause-on-consensus,
    no-consensus continue, party-mode crash degrades cleanly.

The pure-function tests don't need an executor. The runner integration
tests use a small in-file FakeExecutor that implements the Executor
protocol — kept self-contained so test_auto_party.py doesn't import
from sibling test modules.
"""

from __future__ import annotations

import pytest

from auto.flow import Flow, FlowPhase, FlowStep, GateType, RunMode
from auto.party import PartyModeResult, count_rounds, detect_consensus
from auto.runner import AutoRunner, RunStatus, StepResult
from auto.state import (
    STATUS_PAUSED_PARTY,
    AutoState,
)


# ── Pure-function tests: count_rounds ────────────────────────────────


class TestCountRounds:
    def test_count_rounds_with_header_markers(self):
        """Explicit '## Round N' headers are the most reliable signal."""
        output = (
            "Some preamble.\n\n"
            "## Round 1\n"
            "Agent A says X.\n"
            "Agent B says Y.\n\n"
            "## Round 2\n"
            "Agent A clarifies.\n"
            "Agent B agrees.\n"
        )
        assert count_rounds(output) == 2

    def test_count_rounds_with_separator_markers(self):
        """When there are no header markers, '---' separators count as 2 per round."""
        output = (
            "---\n"
            "Round 1 turn A\n"
            "---\n"
            "Round 1 turn B\n"
            "---\n"
            "Round 2 turn A\n"
            "---\n"
            "Round 2 turn B\n"
            "---\n"
        )
        # 5 separators bracket 2 rounds; integer-division by 2 → 2.
        assert count_rounds(output) == 2

    def test_count_rounds_returns_min_rounds_when_fewer_detected(self):
        """The skill is authoritative — never go below the caller's min_rounds."""
        # Output with only ONE explicit round header
        output = "## Round 1\nA single round of debate.\n"
        # Caller says min_rounds=2 — trust the skill, return at least 2
        assert count_rounds(output, min_rounds=2) == 2
        # And empty output also floors at min_rounds
        assert count_rounds("", min_rounds=2) == 2
        # If detected count exceeds min_rounds, take detected value
        big_output = "## Round 1\n## Round 2\n## Round 3\n## Round 4\n"
        assert count_rounds(big_output, min_rounds=2) == 4

    def test_count_rounds_takes_max_observed_round_number(self):
        """Re-stating 'Round 2' in a nested context shouldn't double-count."""
        output = (
            "## Round 1\n"
            "Discussion.\n"
            "## Round 2\n"
            "Recap of round 2 below.\n"
            "## Round 2\n"  # restated header — must not become 3
            "More discussion.\n"
        )
        assert count_rounds(output) == 2

    def test_count_rounds_handles_bold_round_markers(self):
        """**Round N** style headers (no leading '#') are also valid."""
        output = (
            "**Round 1**\n"
            "Agent A: ...\n"
            "**Round 2**\n"
            "Agent B: ...\n"
        )
        assert count_rounds(output) == 2

    def test_count_rounds_empty_output_returns_zero(self):
        """Defensive — no input shouldn't blow up."""
        assert count_rounds("") == 0
        assert count_rounds("   \n\n   ") == 0


# ── Pure-function tests: detect_consensus ────────────────────────────


class TestDetectConsensus:
    def test_detect_consensus_finds_phrase(self):
        """A canonical 'consensus reached' line returns True + summary."""
        output = (
            "## Round 1\nDebate.\n"
            "## Round 2\nMore debate.\n"
            "Consensus reached: ship the simpler approach with logging.\n"
        )
        found, summary = detect_consensus(output, rounds_completed=2)
        assert found is True
        assert "ship the simpler approach with logging" in summary

    def test_detect_consensus_no_match(self):
        """Random unrelated text → no consensus."""
        output = (
            "## Round 1\nDisagreement.\n"
            "## Round 2\nStill disagreeing.\n"
            "We'll have to revisit this next sprint.\n"
        )
        found, summary = detect_consensus(output, rounds_completed=2)
        assert found is False
        assert summary == ""

    def test_detect_consensus_only_after_round_2(self):
        """Consensus phrase in round 1 output is treated as warm-up chatter."""
        output = (
            "## Round 1\n"
            "Consensus reached: trivial agreement before real debate.\n"
        )
        # rounds_completed=1 → ignore the phrase entirely
        found, summary = detect_consensus(output, rounds_completed=1)
        assert found is False
        assert summary == ""
        # Same input but at rounds_completed=2 → now it counts
        found2, summary2 = detect_consensus(output, rounds_completed=2)
        assert found2 is True
        assert "trivial agreement before real debate" in summary2

    def test_detect_consensus_summary_capped_at_500_chars(self):
        """Summary must fit comfortably in a Telegram inline-keyboard header."""
        long_tail = "x" * 1000
        output = f"## Round 1\n## Round 2\nFinal recommendation: {long_tail}\n"
        found, summary = detect_consensus(output, rounds_completed=2)
        assert found is True
        # 500-char cap + optional ellipsis → must not exceed 501 chars
        assert len(summary) <= 501

    def test_detect_consensus_recognizes_multiple_phrases(self):
        """All listed phrases trigger detection (sample three)."""
        for phrase, expected_in_summary in [
            ("All agents agree", "we should refactor"),
            ("Recommendation:", "use approach B"),
            ("We agree", "the bug is in module X"),
        ]:
            output = (
                f"## Round 1\nDebate.\n## Round 2\n{phrase} that {expected_in_summary}.\n"
            )
            found, summary = detect_consensus(output, rounds_completed=2)
            assert found is True, f"phrase {phrase!r} not detected"
            assert expected_in_summary in summary


# ── Runner integration tests ─────────────────────────────────────────


class _PartyFakeExecutor:
    """Minimal Executor implementation focused on party-mode integration.

    Implements every method the runner protocol calls, but only tracks
    state relevant to the party-mode tests below. Same shape as
    test_auto_runner.FakeExecutor but kept local so this file is
    self-contained.
    """

    def __init__(self):
        self.outcomes: dict[str, StepResult] = {}
        self.party_mode_result: PartyModeResult | BaseException | None = None
        self.party_mode_calls: list[dict] = []
        self.party_yolo_accepts: list[dict] = []
        self.party_gate_presents: list[dict] = []

    async def run_skill(
        self, *, thread_id, skill, model, new_session,
        prompt_override=None, timeout_seconds=None,
    ):
        return self.outcomes.get(skill, StepResult(success=True))

    async def run_builtin(self, *, thread_id, builtin, state):
        return StepResult(success=True)

    async def ask_human_review(self, *, thread_id, step, timeout_seconds=None):
        return True

    async def run_party_mode(
        self, *, thread_id, context, min_rounds, timeout_seconds=None,
    ):
        self.party_mode_calls.append({
            "thread_id": thread_id, "context": context,
            "min_rounds": min_rounds,
        })
        if isinstance(self.party_mode_result, BaseException):
            raise self.party_mode_result
        if isinstance(self.party_mode_result, PartyModeResult):
            return self.party_mode_result
        return PartyModeResult(rounds=min_rounds, consensus=False)

    async def notify_party_yolo_accept(self, *, thread_id, summary):
        self.party_yolo_accepts.append({
            "thread_id": thread_id, "summary": summary,
        })

    async def present_party_consensus_gate(self, *, thread_id, step_id, summary):
        self.party_gate_presents.append({
            "thread_id": thread_id, "step_id": step_id, "summary": summary,
        })

    async def ask_once(self, *, thread_id, step, prompt, timeout_seconds=None):
        return True

    async def list_pending_stories(self, *, thread_id):
        return []

    async def artifact_exists(self, *, thread_id, path):
        return False

    async def is_validation_passed(self, *, thread_id, skill):
        return False


def _party_flow() -> Flow:
    """Single-step flow with on_findings='party-mode' and PARTY_MODE gate.

    PARTY_MODE gate always `continue`s in `_evaluate_gate` — so the only
    pause-or-continue decision in these tests comes from party-mode's own
    consensus logic. That isolation matters: the existing AUTO_ON_PASS +
    on_findings test in test_auto_runner.py already covers the
    "AUTO_ON_PASS still pauses on findings even when party-mode runs"
    interaction. Here we want to verify the consensus-path semantics
    without that confound.
    """
    step = FlowStep(
        id="review",
        phase_id="p1",
        skill="code-review",
        gate=GateType.PARTY_MODE,
        on_findings="party-mode",
    )
    phase = FlowPhase(id="p1", description="", steps=(step,))
    return Flow(
        id="test-party", name="Test Party", description="",
        default_model_strategy="balanced",
        phases=(phase,),
    )


def _state(mode: RunMode) -> AutoState:
    return AutoState(thread_id=42, flow_id="test-party", mode=mode)


@pytest.fixture
def party_executor():
    return _PartyFakeExecutor()


class TestPartyModeRunnerIntegration:
    @pytest.mark.asyncio
    async def test_party_mode_yolo_auto_accepts_consensus(self, party_executor):
        """YOLO + consensus → notify_party_yolo_accept fires, run continues."""
        party_executor.outcomes["code-review"] = StepResult(
            success=True, findings=True, notes="3 issues",
        )
        party_executor.party_mode_result = PartyModeResult(
            rounds=2, consensus=True,
            consensus_summary="Use approach X with logging.",
        )
        runner = AutoRunner(_party_flow(), _state(RunMode.YOLO), party_executor)

        status = await runner.step()

        # Party-mode was invoked exactly once
        assert len(party_executor.party_mode_calls) == 1
        # YOLO notification was sent with the extracted summary
        assert len(party_executor.party_yolo_accepts) == 1
        assert party_executor.party_yolo_accepts[0]["summary"] == (
            "Use approach X with logging."
        )
        # AUTO-only path was NOT taken
        assert party_executor.party_gate_presents == []
        # Step recorded as success with the actual round count
        assert runner.state.history[-1].status == "success"
        assert runner.state.history[-1].party_mode_rounds == 2
        # YOLO bypasses human-review on AUTO_ON_PASS findings → CONTINUE
        assert status == RunStatus.CONTINUE
        # Stashed context cleared — YOLO doesn't allow re-runs
        assert runner.state.last_party_context is None

    @pytest.mark.asyncio
    async def test_party_mode_auto_pauses_on_consensus(self, party_executor):
        """AUTO + consensus → present_party_consensus_gate fires, runner pauses."""
        party_executor.outcomes["code-review"] = StepResult(
            success=True, findings=True, notes="3 issues",
        )
        party_executor.party_mode_result = PartyModeResult(
            rounds=3, consensus=True,
            consensus_summary="Refactor module Y; ship.",
        )
        runner = AutoRunner(_party_flow(), _state(RunMode.AUTO), party_executor)

        status = await runner.step()

        # AUTO-only consensus gate was presented with the summary
        assert len(party_executor.party_gate_presents) == 1
        gate_call = party_executor.party_gate_presents[0]
        assert gate_call["step_id"] == "review"
        assert gate_call["summary"] == "Refactor module Y; ship."
        # YOLO-only path was NOT taken
        assert party_executor.party_yolo_accepts == []
        # Runner is paused at PAUSED_PARTY (not PAUSED_GATE)
        assert status == RunStatus.PAUSED
        assert runner.state.status == STATUS_PAUSED_PARTY
        assert runner.state.pending_gate_step_id == "review"
        # Context stashed for "More Rounds" callback
        assert runner.state.last_party_context == "3 issues"

    @pytest.mark.asyncio
    async def test_party_mode_no_consensus_continues(self, party_executor):
        """No consensus → no UX call; PARTY_MODE gate continues."""
        party_executor.outcomes["code-review"] = StepResult(
            success=True, findings=True, notes="2 issues",
        )
        party_executor.party_mode_result = PartyModeResult(
            rounds=2, consensus=False,
        )
        runner = AutoRunner(_party_flow(), _state(RunMode.AUTO), party_executor)

        status = await runner.step()

        # Party-mode invoked, but neither consensus path fired
        assert len(party_executor.party_mode_calls) == 1
        assert party_executor.party_yolo_accepts == []
        assert party_executor.party_gate_presents == []
        # Without consensus, PARTY_MODE gate just continues
        assert status == RunStatus.CONTINUE
        # Stale context cleared on no-consensus
        assert runner.state.last_party_context is None
        # Round count from the result is recorded on the step
        assert runner.state.history[-1].party_mode_rounds == 2

    @pytest.mark.asyncio
    async def test_party_mode_crash_returns_zero_rounds(self, party_executor):
        """run_party_mode raising → degraded result, run continues past gate."""
        party_executor.outcomes["code-review"] = StepResult(
            success=True, findings=True, notes="some findings",
        )
        # Inject a crash — runner's try/except converts to PartyModeResult(0, False)
        party_executor.party_mode_result = RuntimeError("party-mode skill exploded")
        runner = AutoRunner(_party_flow(), _state(RunMode.YOLO), party_executor)

        status = await runner.step()

        # Party-mode was attempted
        assert len(party_executor.party_mode_calls) == 1
        # Neither consensus path fired (no consensus from crash result)
        assert party_executor.party_yolo_accepts == []
        assert party_executor.party_gate_presents == []
        # Step still recorded as success — findings detected, party-mode tried
        last = runner.state.history[-1]
        assert last.status == "success"
        assert last.findings is True
        assert last.party_mode_rounds == 0  # crash → zero rounds
        # YOLO bypasses AUTO_ON_PASS pause → run continues
        assert status == RunStatus.CONTINUE
