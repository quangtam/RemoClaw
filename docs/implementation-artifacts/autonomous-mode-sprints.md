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

## Sprint C — Smart gates & loop expansion [DONE]

**Files:** `auto/{flow,state,runner,executor}.py`, `_bmad/flows/full.yaml`,
`db.py` migration (`loop_stories` column), `tests/test_auto_runner.py`,
`tests/test_auto_state.py`, `tests/test_auto_flow.py`, `tests/test_auto_executor_helpers.py` (new).

**Built:**
- Per-story loop expansion
  - `AutoRunner._maybe_expand_loop` snapshots `Executor.list_pending_stories()`
    onto `AutoState.loop_stories` when the cursor first lands on a `loop:
    per-story` step. Empty list → loop step is recorded `skipped` and
    advanced past.
  - `_current_step` consults the snapshot for active iteration; `_advance`
    walks substep → next iteration → next phase, clearing snapshot at end.
  - `new_session_each: true` triggers `new_session=True` on the FIRST
    substep of every iteration → AutoExecutor's `runner.cancel(thread_id)`
    runs between stories.
  - `loop_stories` persists through SQLite (new `loop_stories TEXT` column,
    JSON array, with ALTER TABLE migration).
- Auto-skip when validation passed
  - New step field `skip_if_validated: bool` → runner asks executor
    `is_validation_passed(skill)` before running. Skipped silently with
    `notes="validation already passed"`.
  - AutoExecutor implementation greps `docs/planning-artifacts/*` for
    `validationStatus: COMPLETE` (validate-prd) or `readinessStatus:
    COMPLETE` / `readiness: ready` (readiness check).
  - Crash in heuristic falls back to running the validator (safe default).
- Smarter project-context detection
  - New step field `skip_if_artifact: <path>`. In yolo: silent skip.
    In auto with `ask_once`: user is asked "Recreate?" — yes runs the
    step, no skips.
  - AutoExecutor `artifact_exists(path)` resolves against the thread's
    bound `project_dir` (falls back via `project_dir_resolver` →
    `thread_config.project_dir` → cwd).
  - `full.yaml` now declares: `skip_if_artifact: docs/planning-artifacts/
    {prd,architecture,epics}.md` with ask_once prompts; `skip_if_validated`
    on validate-prd and readiness.
- Better `previous_succeeded` semantics
  - `_prev_succeeded` now looks up the immediately preceding step *in the
    current phase* (not just history's last record). First step of a phase
    has no predecessor and runs.
  - `skipped` records (e.g. optional declined) are treated as non-failure
    so dependent steps still run.

**Test count:** +40 new (8 loop expansion, 3 skip_if_validated, 5 skip_if_artifact,
4 prev_succeeded, 4 state/flow round-trip, 12 executor helpers, 4 misc).
Cumulative: 473 total.

**Cách dùng (after restart):**
```
/auto full              # cẩn thận, pause ở mọi human-review gate
/yolo full              # tự chạy hết, kể cả per-story loop
/auto status            # xem progress, có hiển thị story đang chạy
```

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
| Sprint C | +40 | 473 |
| Sprint D (planned) | +10 | ~483 |
| Sprint E (planned) | +8 | ~491 |
