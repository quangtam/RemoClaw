# Autonomous Mode — Sprint Tracker

> Multi-sprint feature: let the bot drive a full BMAD flow end-to-end with
> `/auto <flow>` (cautious) or `/yolo <flow>` (full-throttle). Pauses only
> at human-review gates, hard failures, or party-mode decisions.

## Decisions baked in

| Topic | Decision |
| ----- | -------- |
| Order | Quick Dev first, Full second |
| Modes | `/auto` pauses at every human-review; `/yolo` auto-approves all gates |
| Retries | Max 3 per step. After 3 fails → pause and ask user |
| Party-mode | Min 2 rounds. After 2 rounds: yolo auto-decides, auto asks user |
| Multi-thread | Yes — one autonomous run per thread, threads run in parallel |

## Architecture (committed)

```
                        ┌──────────────┐
   /auto, /yolo  ──────▶│  remoclaw.py │  command handlers
                        └──────┬───────┘
                               │ creates
                               ▼
                        ┌──────────────┐
                        │ AutoExecutor │ implements Executor protocol
                        │ (Telegram +  │ talks to CliRunner + db + Bot
                        │  CliRunner)  │
                        └──────┬───────┘
                               │ injected into
                               ▼
              ┌────────────┐   ┌──────────────┐
              │ AutoRunner │◀──│  AutoState   │ persisted in SQLite
              │ (pure FSM) │   │  (auto_run)  │
              └─────┬──────┘   └──────────────┘
                    │ pumped by
                    ▼
              ┌────────────┐
              │   Driver   │ background asyncio task per thread
              └────────────┘
```

## Sprint A — Foundation [DONE — commit `b039549`]

**Files:** `_bmad/flows/{quick-dev,full}.yaml`, `auto/{flow,state,runner}.py`,
`db.py` migration + helpers, 4 test files (57 tests).

**Built:**
- Flow YAML schema + parser (Flow, FlowStep, FlowPhase, GateType, RunMode, LoopKind)
- AutoState dataclass with JSON round-trip for SQLite
- AutoRunner state machine: step / resume / abort
  - retries (configurable, default 3)
  - gate evaluation (auto, auto-on-pass, human-review, party-mode)
  - party-mode divert on findings
  - ask_once tracking
- `auto_run` SQLite table + get/upsert/delete helpers

**Test count:** 57 new, 399 total.

## Sprint B — Telegram Integration [DONE — commit `580989d`]

**Files:** `auto/{executor,driver,findings}.py`, updates to `remoclaw.py`,
2 test files (22 tests).

**Built:**
- `/auto <flow>` — start auto run with human-review pauses
- `/yolo <flow>` — start yolo run that auto-approves gates
- `/auto status|resume|abort|skip` — sub-commands
- `AutoExecutor` — implements Executor protocol (CliRunner + Telegram bridge)
- `Driver` — background asyncio task, one per thread
- `findings.py` — heuristic detection of review findings (regex + symbols)
- Inline keyboards for gate approve/abort and ask_once yes/skip
- Bot commands API updated with /auto + /yolo

**Test count:** 22 new, 421 total.

**Cách dùng (after restart):**
```
/auto quick-dev        # cẩn thận, pause ở mọi human-review gate
/yolo quick-dev        # tự chạy hết
/auto status           # xem progress
/auto resume           # tiếp tục sau khi gate approved
/auto abort            # dừng hoàn toàn
/auto skip             # bỏ qua step hiện tại, chạy tiếp
```

## Sprint C — Smart gates & loop expansion [TODO]

**Goal:** Make `full` flow's per-story loop work for real, and add smarter
gate logic that skips redundant steps.

**Tasks:**
- Per-story loop expansion
  - Read `sprint-status.yaml` to find pending stories
  - Expand `story-loop` step into N iterations at runtime
  - `new_session_each: true` → call `runner.cancel(thread_id)` between iterations
  - Track current loop iter in AutoState (already there but unused)
- Auto-skip when validation passed
  - `bmad-validate-prd` after `bmad-create-prd`: if PRD already validated, skip
  - Similar for `bmad-check-implementation-readiness`
- Better `previous_succeeded` semantics
  - Currently checks last record only — should check last record for the
    immediately preceding step in the same phase
- Project context detection
  - Check if `_bmad/bmm/config.yaml` exists → seed flow with that context
  - If `docs/planning-artifacts/prd.md` exists → skip PRD step (with ask_once)

**Estimated:** ~400 LOC + 15 tests.

## Sprint D — Party-mode integration [TODO]

**Goal:** When code-review surfaces findings, divert into party-mode that
actually streams agent debate to the user.

**Tasks:**
- Stream party-mode rounds to Telegram
  - Currently `run_party_mode` exists as a stub; wire it to actual streaming
- Round counter + min-2-rounds enforcement
  - Currently `min_rounds` is passed but the skill enforces it; verify
- Consensus detection after round 2+
  - Look for "consensus", "agreement", "all agree" phrases in agent outputs
- YOLO mode: auto-accept consensus and continue
- AUTO mode: present consensus + ask user [Continue] / [More rounds] / [Override]
- Tests with mocked party-mode output

**Estimated:** ~300 LOC + 10 tests.

## Sprint E — UX polish [TODO]

**Tasks:**
- Pretty `/auto status` with progress bar (`▰▰▰▱▱ 3/5 phases`)
- Notification message edits during long steps (typing keepalive style)
- Auto-capture screenshots when step output mentions image paths
- README section: "Autonomous Mode"
- Docs page: `docs/autonomous-mode.md` with example flows
- Optional: `/auto history` command to see past runs

**Estimated:** ~250 LOC + 8 tests + docs.

## Open questions for later

1. **Custom flow definitions** — should users be able to drop a custom YAML
   in `_bmad/flows/<my-flow>.yaml` and have it auto-discovered? (Already
   supported by `list_available_flows`, just needs README.)
2. **Resume across bot restart** — auto_run row persists, but the driver
   task does not. Should bot restart auto-resume `STATUS_RUNNING` rows?
   For now: user must manually `/auto resume`.
3. **Concurrent flows in same thread** — currently rejected. Could allow
   "stacked" flows where a `human-review` gate launches a sub-flow.

## Test status snapshot

| Phase | Tests | Cumulative |
| ----- | ----- | ---------- |
| Pre-Sprint A baseline | 342 | 342 |
| Sprint A | +57 | 399 |
| Sprint B | +22 | 421 |
| Sprint C (planned) | +15 | ~436 |
| Sprint D (planned) | +10 | ~446 |
| Sprint E (planned) | +8 | ~454 |
