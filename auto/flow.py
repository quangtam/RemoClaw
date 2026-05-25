"""Flow definition models — parses _bmad/flows/*.yaml into typed objects.

Flow YAML schema (intentionally permissive — extra keys are ignored so the
schema can grow without breaking older runs):

    name: <human label>
    id: <slug>
    description: <one-liner>
    default_model_strategy: fast | balanced | strong
    phases:
      - id: <slug>
        description: <one-liner>
        steps:
          - id: <slug>
            skill: <bmad-skill-name>           # OR builtin: <handler-id>
            model: <provider model alias>
            new_session: true|false            # /new before this step
            gate: auto | auto-on-pass | human-review | party-mode
            on_findings: party-mode | none
            max_retries: <int, default 3>
            condition: previous_succeeded | always
            optional: true|false
            ask_once: <prompt to user, only asked once when first encountered>
          - id: <slug>
            loop: per-story                    # expands at runtime
            new_session_each: true|false
            substeps: [...]                    # same shape as steps[]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


# ── Enums ────────────────────────────────────────────────────────────


class GateType(str, Enum):
    """How a step should pause (or not) before continuing."""

    AUTO = "auto"
    AUTO_ON_PASS = "auto-on-pass"
    HUMAN_REVIEW = "human-review"
    PARTY_MODE = "party-mode"


class RunMode(str, Enum):
    """Two ways to run a flow."""

    AUTO = "auto"   # pause at every human-review gate
    YOLO = "yolo"   # auto-approve every gate (still stops on hard failure)


class LoopKind(str, Enum):
    """Step expansion kinds. Currently only per-story."""

    NONE = "none"
    PER_STORY = "per-story"


# ── Models ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FlowStep:
    """A single executable step in a flow.

    Either `skill` or `builtin` is set, never both. `builtin` refers to
    runner-internal handlers (like 'present-summary') that don't shell
    out to a skill.
    """

    id: str
    phase_id: str
    skill: str | None = None
    builtin: str | None = None
    model: str | None = None
    new_session: bool = False
    gate: GateType = GateType.AUTO
    on_findings: str = "none"          # 'party-mode' | 'none'
    max_retries: int = 3
    condition: str = "always"          # 'always' | 'previous_succeeded'
    optional: bool = False
    ask_once: str | None = None
    loop: LoopKind = LoopKind.NONE
    new_session_each: bool = False
    substeps: tuple["FlowStep", ...] = ()

    def __post_init__(self) -> None:
        if self.loop == LoopKind.NONE:
            if not self.skill and not self.builtin:
                raise ValueError(
                    f"Step '{self.id}' must define either `skill` or `builtin`"
                )
            if self.skill and self.builtin:
                raise ValueError(
                    f"Step '{self.id}' has both `skill` and `builtin` — pick one"
                )
        else:
            if not self.substeps:
                raise ValueError(
                    f"Loop step '{self.id}' must define `substeps`"
                )


@dataclass(frozen=True)
class FlowPhase:
    """A named group of steps. Phases are sequential; steps within a phase too."""

    id: str
    description: str
    steps: tuple[FlowStep, ...]


@dataclass(frozen=True)
class Flow:
    """A complete, validated flow definition."""

    id: str
    name: str
    description: str
    default_model_strategy: str
    phases: tuple[FlowPhase, ...]

    def all_steps(self) -> list[FlowStep]:
        """Flatten phases → steps for sequential iteration.

        Loop steps are returned as-is; expansion happens at runtime when the
        runner knows the actual story list.
        """
        out: list[FlowStep] = []
        for phase in self.phases:
            out.extend(phase.steps)
        return out

    def find_step(self, step_id: str) -> FlowStep | None:
        for phase in self.phases:
            for step in phase.steps:
                if step.id == step_id:
                    return step
                for sub in step.substeps:
                    if sub.id == step_id:
                        return sub
        return None


# ── Loader ───────────────────────────────────────────────────────────


def _parse_step(raw: dict[str, Any], phase_id: str) -> FlowStep:
    loop_value = raw.get("loop", "none")
    try:
        loop = LoopKind(loop_value)
    except ValueError as exc:
        raise ValueError(
            f"Step '{raw.get('id', '?')}' has unknown loop '{loop_value}'"
        ) from exc

    gate_value = raw.get("gate", "auto")
    try:
        gate = GateType(gate_value)
    except ValueError as exc:
        raise ValueError(
            f"Step '{raw.get('id', '?')}' has unknown gate '{gate_value}'"
        ) from exc

    substeps_raw = raw.get("substeps") or []
    substeps = tuple(_parse_step(s, phase_id) for s in substeps_raw)

    return FlowStep(
        id=raw["id"],
        phase_id=phase_id,
        skill=raw.get("skill"),
        builtin=raw.get("builtin"),
        model=raw.get("model"),
        new_session=bool(raw.get("new_session", False)),
        gate=gate,
        on_findings=raw.get("on_findings", "none"),
        max_retries=int(raw.get("max_retries", 3)),
        condition=raw.get("condition", "always"),
        optional=bool(raw.get("optional", False)),
        ask_once=raw.get("ask_once"),
        loop=loop,
        new_session_each=bool(raw.get("new_session_each", False)),
        substeps=substeps,
    )


def _parse_phase(raw: dict[str, Any]) -> FlowPhase:
    pid = raw["id"]
    steps = tuple(_parse_step(s, pid) for s in raw.get("steps", []))
    return FlowPhase(
        id=pid,
        description=raw.get("description", ""),
        steps=steps,
    )


def load_flow(name: str, *, flows_dir: Path | None = None) -> Flow:
    """Load and validate a flow YAML.

    Args:
        name: Flow id or filename stem (e.g. 'quick-dev', 'full').
        flows_dir: Override the default search path. If None, looks in
            <project_root>/_bmad/flows/.

    Raises:
        FileNotFoundError: if the YAML doesn't exist.
        ValueError: if the YAML is malformed.
    """
    if flows_dir is None:
        # Default: project_root/_bmad/flows/ — assume cwd is project root
        flows_dir = Path.cwd() / "_bmad" / "flows"

    candidate = flows_dir / f"{name}.yaml"
    if not candidate.is_file():
        raise FileNotFoundError(f"Flow YAML not found: {candidate}")

    with candidate.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(f"Flow {candidate} must be a YAML mapping at top level")

    if "id" not in raw or "name" not in raw:
        raise ValueError(f"Flow {candidate} missing required keys: id, name")

    phases = tuple(_parse_phase(p) for p in raw.get("phases", []))
    if not phases:
        raise ValueError(f"Flow {candidate} has no phases")

    return Flow(
        id=raw["id"],
        name=raw["name"],
        description=raw.get("description", ""),
        default_model_strategy=raw.get("default_model_strategy", "balanced"),
        phases=phases,
    )


def list_available_flows(flows_dir: Path | None = None) -> list[str]:
    """Return the list of flow ids discoverable in `flows_dir`."""
    if flows_dir is None:
        flows_dir = Path.cwd() / "_bmad" / "flows"
    if not flows_dir.is_dir():
        return []
    return sorted(p.stem for p in flows_dir.glob("*.yaml"))
