"""Autonomous flow orchestration for RemoClaw.

Lets a user kick off a multi-step BMAD workflow with a single command and
have the bot drive every step — switching models, opening fresh sessions,
running skills, and only pausing when a real human decision is needed.

Two run modes:
  /auto <flow>  — pause at every human-review gate (cautious)
  /yolo <flow>  — auto-approve every gate, only stop on hard failure

Public surface:
  load_flow(name)            — parse a YAML flow into a Flow object
  AutoRunner(flow, ...)      — orchestrator state machine
  AutoState                  — persistent run state (mirrors SQLite row)
"""

from auto.driver import is_driver_active, start_run, stop_driver
from auto.executor import (
    AutoExecutor,
    has_pending_decision,
    resolve_pending_decision,
    resolve_pending_party_decision,
)
from auto.flow import Flow, FlowStep, GateType, RunMode, list_available_flows, load_flow
from auto.party import PartyModeResult, count_rounds, detect_consensus
from auto.runner import AutoRunner, RunStatus
from auto.state import AutoState

__all__ = [
    "AutoExecutor",
    "AutoRunner",
    "AutoState",
    "Flow",
    "FlowStep",
    "GateType",
    "PartyModeResult",
    "RunMode",
    "RunStatus",
    "count_rounds",
    "detect_consensus",
    "has_pending_decision",
    "is_driver_active",
    "list_available_flows",
    "load_flow",
    "resolve_pending_decision",
    "resolve_pending_party_decision",
    "start_run",
    "stop_driver",
]
