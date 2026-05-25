"""Integration tests for db.auto_run helpers — round-trip via SQLite."""

import pytest

from auto.flow import RunMode
from auto.state import AutoState, StepRecord
from db import (
    DB_PATH,
    delete_auto_run,
    get_auto_run,
    init_db,
    upsert_auto_run,
)


@pytest.fixture
async def db_path(tmp_path):
    p = str(tmp_path / "test_auto.db")
    await init_db(path=p, default_project_dir="/tmp/x")
    return p


class TestAutoRunRepository:
    @pytest.mark.asyncio
    async def test_get_returns_none_when_no_row(self, db_path):
        row = await get_auto_run(thread_id=999, path=db_path)
        assert row is None

    @pytest.mark.asyncio
    async def test_upsert_then_get(self, db_path):
        s = AutoState(thread_id=42, flow_id="quick-dev", mode=RunMode.YOLO)
        s.history.append(StepRecord(
            step_id="dev", phase_id="develop", skill="bmad-quick-dev",
            status="success", attempts=1,
        ))
        s.ask_once_asked.add("ux")

        await upsert_auto_run(s.to_db_row(), path=db_path)

        row = await get_auto_run(thread_id=42, path=db_path)
        assert row is not None
        assert row["flow_id"] == "quick-dev"
        assert row["mode"] == "yolo"

        # Round-trip back to AutoState
        s2 = AutoState.from_db_row(row)
        assert s2.thread_id == 42
        assert s2.mode == RunMode.YOLO
        assert len(s2.history) == 1
        assert s2.history[0].step_id == "dev"
        assert s2.ask_once_asked == {"ux"}

    @pytest.mark.asyncio
    async def test_upsert_replaces_existing(self, db_path):
        s = AutoState(thread_id=1, flow_id="quick-dev", mode=RunMode.AUTO)
        await upsert_auto_run(s.to_db_row(), path=db_path)

        # Modify and upsert again
        s.status = "done"
        s.flow_id = "full"
        await upsert_auto_run(s.to_db_row(), path=db_path)

        row = await get_auto_run(thread_id=1, path=db_path)
        assert row["status"] == "done"
        assert row["flow_id"] == "full"

    @pytest.mark.asyncio
    async def test_delete_removes_row(self, db_path):
        s = AutoState(thread_id=1, flow_id="quick-dev", mode=RunMode.AUTO)
        await upsert_auto_run(s.to_db_row(), path=db_path)
        assert await get_auto_run(thread_id=1, path=db_path) is not None

        await delete_auto_run(thread_id=1, path=db_path)
        assert await get_auto_run(thread_id=1, path=db_path) is None

    @pytest.mark.asyncio
    async def test_multi_thread_rows_isolated(self, db_path):
        s1 = AutoState(thread_id=1, flow_id="quick-dev", mode=RunMode.AUTO)
        s2 = AutoState(thread_id=2, flow_id="full", mode=RunMode.YOLO)
        await upsert_auto_run(s1.to_db_row(), path=db_path)
        await upsert_auto_run(s2.to_db_row(), path=db_path)

        r1 = await get_auto_run(thread_id=1, path=db_path)
        r2 = await get_auto_run(thread_id=2, path=db_path)
        assert r1["flow_id"] == "quick-dev"
        assert r2["flow_id"] == "full"
        assert r1["mode"] == "auto"
        assert r2["mode"] == "yolo"
