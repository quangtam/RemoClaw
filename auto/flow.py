"""Flow definition models — parses _bmad/flows/*.yaml into typed objects.

Flow YAML schema (intentionally permissive — extra keys are ignored so the
schema can grow without breaking older runs):

    name: <human label>
    id: <slug>
    description: <one-liner>
    default_model_strategy: fast | balanced | strong
    default_step_timeout_seconds: <int>      # per-step max runtime, default 1800 (30min)
    default_decision_timeout_seconds: <int>  # max wait for user gate reply, default 86400 (24h)
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
            timeout_seconds: <int>             # per-step override
            skip_if_validated: true|false      # skip when executor reports
                                               # validation already passed
                                               # (see Executor.is_validation_passed)
            skip_if_artifact: <path>           # skip if artifact exists at
                                               # path (relative to project_dir).
                                               # In yolo: silently skip.
                                               # In auto: if `ask_once` is also
                                               # set, ask user (yes=run anyway,
                                               # no=skip). Otherwise skip.
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
    # Per-step timeout. None → use Flow.default_step_timeout_seconds.
    # Big skills like bmad-quick-dev can take 30+ min, so the default is
    # generous (1800s); smaller checks should override down.
    timeout_seconds: int | None = None
    # Skip when a separate validation report already covers this step.
    # The Executor decides what counts as "validated" (e.g. presence of a
    # `validationStatus: COMPLETE` validation report). Used for steps that
    # are pure double-checks of an earlier artifact (validate-prd, readiness).
    skip_if_validated: bool = False
    # Skip when the named artifact already exists (relative to project_dir).
    # In yolo mode the skip is silent; in auto mode, if `ask_once` is also
    # defined, the user is asked whether to regenerate or skip.
    skip_if_artifact: str | None = None

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
    # Defaults applied to any step that doesn't specify its own value.
    # Generous defaults because autonomous runs are long-running.
    default_step_timeout_seconds: int = 1800       # 30 min per step
    default_decision_timeout_seconds: int = 86400  # 24h waiting for human

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
        timeout_seconds=int(raw["timeout_seconds"]) if "timeout_seconds" in raw else None,
        skip_if_validated=bool(raw.get("skip_if_validated", False)),
        skip_if_artifact=raw.get("skip_if_artifact"),
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
        default_step_timeout_seconds=int(raw.get("default_step_timeout_seconds", 1800)),
        default_decision_timeout_seconds=int(raw.get("default_decision_timeout_seconds", 86400)),
    )


def list_available_flows(flows_dir: Path | None = None) -> list[str]:
    """Return the list of flow ids discoverable in `flows_dir`."""
    if flows_dir is None:
        flows_dir = Path.cwd() / "_bmad" / "flows"
    if not flows_dir.is_dir():
        return []
    return sorted(p.stem for p in flows_dir.glob("*.yaml"))
