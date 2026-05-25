"""Tests for auto.state — AutoState serialization round-trips."""

from auto.flow import RunMode
from auto.state import (
    STATUS_DONE,
    STATUS_PAUSED_FAIL,
    STATUS_PAUSED_GATE,
    STATUS_RUNNING,
    AutoState,
    StepRecord,
)


class TestAutoStateSerialization:
    def test_round_trip_minimal(self):
        s = AutoState(thread_id=42, flow_id="quick-dev", mode=RunMode.AUTO)
        row = s.to_db_row()
        s2 = AutoState.from_db_row(row)
        assert s2.thread_id == 42
        assert s2.flow_id == "quick-dev"
        assert s2.mode == RunMode.AUTO
        assert s2.status == STATUS_RUNNING
        assert s2.history == []
        assert s2.ask_once_asked == set()

    def test_round_trip_with_history(self):
        s = AutoState(thread_id=1, flow_id="full", mode=RunMode.YOLO)
        s.history.append(StepRecord(
            step_id="prd", phase_id="planning", skill="bmad-create-prd",
            status="success", attempts=2, findings=False,
            party_mode_rounds=0, notes="ok",
        ))
        s.history.append(StepRecord(
            step_id="review", phase_id="planning", skill="bmad-code-review",
            status="success", attempts=1, findings=True,
            party_mode_rounds=2, notes="findings discussed",
        ))
        s.ask_once_asked.add("ux")
        s.ask_once_asked.add("brief")

        row = s.to_db_row()
        s2 = AutoState.from_db_row(row)

        assert len(s2.history) == 2
        assert s2.history[0].step_id == "prd"
        assert s2.history[0].attempts == 2
        assert s2.history[1].findings is True
        assert s2.history[1].party_mode_rounds == 2
        assert s2.ask_once_asked == {"ux", "brief"}

    def test_round_trip_paused_state(self):
        s = AutoState(
            thread_id=10, flow_id="quick-dev", mode=RunMode.AUTO,
            status=STATUS_PAUSED_GATE, pending_gate_step_id="prd",
        )
        s2 = AutoState.from_db_row(s.to_db_row())
        assert s2.status == STATUS_PAUSED_GATE
        assert s2.pending_gate_step_id == "prd"

    def test_round_trip_with_error(self):
        s = AutoState(
            thread_id=10, flow_id="quick-dev", mode=RunMode.YOLO,
            status=STATUS_PAUSED_FAIL, last_error="CLI timeout",
        )
        s2 = AutoState.from_db_row(s.to_db_row())
        assert s2.status == STATUS_PAUSED_FAIL
        assert s2.last_error == "CLI timeout"

    def test_corrupted_history_falls_back_to_empty(self):
        row = {
            "thread_id": 1, "flow_id": "x", "mode": "auto",
            "history": "not-json{",
        }
        s = AutoState.from_db_row(row)
        assert s.history == []

    def test_corrupted_ask_once_falls_back_to_empty(self):
        row = {
            "thread_id": 1, "flow_id": "x", "mode": "auto",
            "ask_once_asked": "garbage",
        }
        s = AutoState.from_db_row(row)
        assert s.ask_once_asked == set()


class TestAutoStateHelpers:
    def test_is_active_true_for_running(self):
        s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO)
        assert s.is_active() is True

    def test_is_active_true_for_paused(self):
        s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO, status=STATUS_PAUSED_GATE)
        assert s.is_active() is True

    def test_is_active_false_for_done(self):
        s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO, status=STATUS_DONE)
        assert s.is_active() is False

    def test_is_paused_true_for_paused_states(self):
        for status in (STATUS_PAUSED_GATE, STATUS_PAUSED_FAIL):
            s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO, status=status)
            assert s.is_paused() is True

    def test_is_paused_false_for_running(self):
        s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO)
        assert s.is_paused() is False

    def test_attempts_for_counts_consecutive(self):
        s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO)
        s.history.extend([
            StepRecord(step_id="prd", phase_id="p", skill="x", status="failed"),
            StepRecord(step_id="prd", phase_id="p", skill="x", status="failed"),
        ])
        assert s.attempts_for("prd") == 2

    def test_attempts_for_resets_on_other_step(self):
        s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO)
        s.history.extend([
            StepRecord(step_id="prd", phase_id="p", skill="x", status="failed"),
            StepRecord(step_id="ux", phase_id="p", skill="y", status="success"),
            StepRecord(step_id="prd", phase_id="p", skill="x", status="failed"),
        ])
        # Only the latest contiguous run counts
        assert s.attempts_for("prd") == 1

    def test_attempts_for_zero_when_no_history(self):
        s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO)
        assert s.attempts_for("prd") == 0

    def test_attempts_for_ignores_success_records(self):
        s = AutoState(thread_id=1, flow_id="x", mode=RunMode.AUTO)
        s.history.append(StepRecord(
            step_id="prd", phase_id="p", skill="x", status="success",
        ))
        assert s.attempts_for("prd") == 0
