"""Tests for auto.flow — flow YAML parsing and validation."""

from pathlib import Path

import pytest

from auto.flow import (
    Flow,
    FlowStep,
    GateType,
    LoopKind,
    list_available_flows,
    load_flow,
)


@pytest.fixture
def flows_dir(tmp_path: Path) -> Path:
    """Return a tmp dir for flow YAMLs."""
    d = tmp_path / "flows"
    d.mkdir()
    return d


def _write(p: Path, content: str) -> None:
    p.write_text(content, encoding="utf-8")


class TestLoadFlow:
    def test_minimal_flow(self, flows_dir):
        _write(flows_dir / "tiny.yaml", """
id: tiny
name: Tiny Flow
description: Smallest valid flow
phases:
  - id: only
    description: only phase
    steps:
      - id: hello
        skill: bmad-quick-dev
""")
        flow = load_flow("tiny", flows_dir=flows_dir)
        assert flow.id == "tiny"
        assert flow.name == "Tiny Flow"
        assert len(flow.phases) == 1
        assert flow.phases[0].steps[0].skill == "bmad-quick-dev"
        assert flow.phases[0].steps[0].gate == GateType.AUTO
        assert flow.phases[0].steps[0].max_retries == 3

    def test_step_with_all_fields(self, flows_dir):
        _write(flows_dir / "full.yaml", """
id: full
name: Full
phases:
  - id: p1
    steps:
      - id: s1
        skill: bmad-create-prd
        model: opus
        new_session: true
        gate: human-review
        on_findings: party-mode
        max_retries: 5
        condition: previous_succeeded
        optional: true
        ask_once: "Run this?"
""")
        flow = load_flow("full", flows_dir=flows_dir)
        s = flow.phases[0].steps[0]
        assert s.skill == "bmad-create-prd"
        assert s.model == "opus"
        assert s.new_session is True
        assert s.gate == GateType.HUMAN_REVIEW
        assert s.on_findings == "party-mode"
        assert s.max_retries == 5
        assert s.condition == "previous_succeeded"
        assert s.optional is True
        assert s.ask_once == "Run this?"

    def test_sprint_c_skip_fields_default_false(self, flows_dir):
        _write(flows_dir / "tiny.yaml", """
id: tiny
name: Tiny
phases:
  - id: p
    steps:
      - id: s
        skill: x
""")
        flow = load_flow("tiny", flows_dir=flows_dir)
        s = flow.phases[0].steps[0]
        assert s.skip_if_validated is False
        assert s.skip_if_artifact is None

    def test_sprint_c_skip_fields_parsed(self, flows_dir):
        _write(flows_dir / "skip.yaml", """
id: skip
name: Skip
phases:
  - id: p
    steps:
      - id: validate
        skill: bmad-validate-prd
        skip_if_validated: true
      - id: prd
        skill: bmad-create-prd
        skip_if_artifact: docs/planning-artifacts/prd.md
        ask_once: "Recreate?"
""")
        flow = load_flow("skip", flows_dir=flows_dir)
        validate = flow.find_step("validate")
        prd = flow.find_step("prd")
        assert validate.skip_if_validated is True
        assert validate.skip_if_artifact is None
        assert prd.skip_if_validated is False
        assert prd.skip_if_artifact == "docs/planning-artifacts/prd.md"
        assert prd.ask_once == "Recreate?"

    def test_loop_step(self, flows_dir):
        _write(flows_dir / "loopy.yaml", """
id: loopy
name: Loopy
phases:
  - id: impl
    steps:
      - id: per-story
        loop: per-story
        new_session_each: true
        substeps:
          - id: dev
            skill: bmad-dev-story
          - id: review
            skill: bmad-code-review
""")
        flow = load_flow("loopy", flows_dir=flows_dir)
        loop_step = flow.phases[0].steps[0]
        assert loop_step.loop == LoopKind.PER_STORY
        assert loop_step.new_session_each is True
        assert len(loop_step.substeps) == 2
        assert loop_step.substeps[0].id == "dev"
        assert loop_step.substeps[1].id == "review"

    def test_builtin_step(self, flows_dir):
        _write(flows_dir / "b.yaml", """
id: b
name: B
phases:
  - id: end
    steps:
      - id: present
        builtin: present-summary
""")
        flow = load_flow("b", flows_dir=flows_dir)
        s = flow.phases[0].steps[0]
        assert s.builtin == "present-summary"
        assert s.skill is None

    def test_missing_file(self, flows_dir):
        with pytest.raises(FileNotFoundError):
            load_flow("nope", flows_dir=flows_dir)

    def test_missing_id_or_name(self, flows_dir):
        _write(flows_dir / "bad.yaml", """
description: Missing id and name
phases:
  - id: p
    steps:
      - id: s
        skill: bmad-quick-dev
""")
        with pytest.raises(ValueError, match="missing required keys"):
            load_flow("bad", flows_dir=flows_dir)

    def test_no_phases(self, flows_dir):
        _write(flows_dir / "empty.yaml", """
id: empty
name: Empty
""")
        with pytest.raises(ValueError, match="no phases"):
            load_flow("empty", flows_dir=flows_dir)

    def test_step_must_have_skill_or_builtin(self, flows_dir):
        _write(flows_dir / "noop.yaml", """
id: noop
name: NoOp
phases:
  - id: p
    steps:
      - id: ghost
        gate: auto
""")
        with pytest.raises(ValueError, match="must define either"):
            load_flow("noop", flows_dir=flows_dir)

    def test_step_cant_have_both_skill_and_builtin(self, flows_dir):
        _write(flows_dir / "both.yaml", """
id: both
name: Both
phases:
  - id: p
    steps:
      - id: bad
        skill: x
        builtin: y
""")
        with pytest.raises(ValueError, match="both"):
            load_flow("both", flows_dir=flows_dir)

    def test_loop_step_needs_substeps(self, flows_dir):
        _write(flows_dir / "loop-no-sub.yaml", """
id: lns
name: LNS
phases:
  - id: p
    steps:
      - id: dangling
        loop: per-story
""")
        with pytest.raises(ValueError, match="substeps"):
            load_flow("loop-no-sub", flows_dir=flows_dir)

    def test_unknown_gate(self, flows_dir):
        _write(flows_dir / "u.yaml", """
id: u
name: U
phases:
  - id: p
    steps:
      - id: s
        skill: x
        gate: hyper-review
""")
        with pytest.raises(ValueError, match="unknown gate"):
            load_flow("u", flows_dir=flows_dir)


class TestFlowMethods:
    def test_all_steps_flattens(self, flows_dir):
        _write(flows_dir / "f.yaml", """
id: f
name: F
phases:
  - id: a
    steps:
      - id: a1
        skill: x
      - id: a2
        skill: y
  - id: b
    steps:
      - id: b1
        skill: z
""")
        flow = load_flow("f", flows_dir=flows_dir)
        ids = [s.id for s in flow.all_steps()]
        assert ids == ["a1", "a2", "b1"]

    def test_find_step_by_id(self, flows_dir):
        _write(flows_dir / "f.yaml", """
id: f
name: F
phases:
  - id: p
    steps:
      - id: target
        skill: bmad-create-prd
""")
        flow = load_flow("f", flows_dir=flows_dir)
        assert flow.find_step("target").skill == "bmad-create-prd"
        assert flow.find_step("missing") is None

    def test_find_step_in_substeps(self, flows_dir):
        _write(flows_dir / "f.yaml", """
id: f
name: F
phases:
  - id: p
    steps:
      - id: outer
        loop: per-story
        substeps:
          - id: inner
            skill: bmad-dev-story
""")
        flow = load_flow("f", flows_dir=flows_dir)
        assert flow.find_step("inner").skill == "bmad-dev-story"


class TestListAvailableFlows:
    def test_lists_yaml_stems(self, flows_dir):
        _write(flows_dir / "alpha.yaml", "id: alpha\nname: A\nphases:\n - id: p\n   steps:\n    - id: s\n      skill: x\n")
        _write(flows_dir / "beta.yaml", "id: beta\nname: B\nphases:\n - id: p\n   steps:\n    - id: s\n      skill: x\n")
        _write(flows_dir / "readme.txt", "ignored")
        names = list_available_flows(flows_dir=flows_dir)
        assert names == ["alpha", "beta"]

    def test_missing_dir(self, tmp_path):
        assert list_available_flows(flows_dir=tmp_path / "does-not-exist") == []


class TestProductionFlows:
    """Smoke-test the actual quick-dev.yaml + full.yaml ship in _bmad/flows/."""

    def _project_flows_dir(self) -> Path:
        # Walk up from test file to find _bmad/flows
        here = Path(__file__).resolve().parent
        for parent in [here, *here.parents]:
            candidate = parent / "_bmad" / "flows"
            if candidate.is_dir():
                return candidate
        pytest.skip("_bmad/flows/ not found in project tree")

    def test_quick_dev_loads(self):
        flow = load_flow("quick-dev", flows_dir=self._project_flows_dir())
        assert flow.id == "quick-dev"
        assert flow.find_step("quick-dev").skill == "bmad-quick-dev"

    def test_full_loads(self):
        flow = load_flow("full", flows_dir=self._project_flows_dir())
        assert flow.id == "full"
        # Spot-check key steps exist
        assert flow.find_step("prd").skill == "bmad-create-prd"
        assert flow.find_step("architecture").skill == "bmad-create-architecture"
        # Loop step should be present
        loop = flow.find_step("story-loop")
        assert loop.loop == LoopKind.PER_STORY
        assert any(s.skill == "bmad-dev-story" for s in loop.substeps)

    def test_full_has_sprint_c_skip_markers(self):
        """Sprint C: full.yaml should declare skip_if_validated on validate
        and readiness, plus skip_if_artifact on PRD/architecture/epics."""
        flow = load_flow("full", flows_dir=self._project_flows_dir())
        prd = flow.find_step("prd")
        validate = flow.find_step("validate-prd")
        readiness = flow.find_step("readiness")
        assert prd.skip_if_artifact == "docs/planning-artifacts/prd.md"
        assert prd.ask_once is not None  # so user is asked, not silently skipped
        assert validate.skip_if_validated is True
        assert readiness.skip_if_validated is True
