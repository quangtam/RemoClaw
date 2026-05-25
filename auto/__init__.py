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

from auto.flow import Flow, FlowStep, GateType, RunMode, load_flow
from auto.runner import AutoRunner, RunStatus
from auto.state import AutoState

__all__ = [
    "Flow",
    "FlowStep",
    "GateType",
    "RunMode",
    "load_flow",
    "AutoRunner",
    "RunStatus",
    "AutoState",
]
