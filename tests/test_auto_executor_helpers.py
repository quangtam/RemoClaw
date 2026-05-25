"""Tests for AutoExecutor's project-context helpers (Sprint C).

`list_pending_stories`, `artifact_exists`, and `is_validation_passed` all
read from the project directory bound to a thread — they form the bridge
that lets the runner's pure FSM ask "is this work already done?" without
reaching into the filesystem itself.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from auto.executor import AutoExecutor


@pytest.fixture
def make_executor(tmp_path):
    """Build an AutoExecutor with a fake project_dir bound to thread 1.

    The fake bot/runner/db/config refs are MagicMock — we only exercise
    the methods that read the filesystem (the production methods bring
    their own assertions on bot/runner/db).
    """
    project_dir = tmp_path
    db_path = str(tmp_path / "test.db")

    async def resolver(thread_id):
        return str(project_dir)

    def _build():
        ex = AutoExecutor(
            bot=AsyncMock(),
            chat_id=1,
            runner_ref=AsyncMock(),
            config_ref=AsyncMock(),
            db_ref=AsyncMock(),
            db_path=db_path,
            project_dir_resolver=resolver,
        )
        return ex, project_dir

    return _build


# ── list_pending_stories ──────────────────────────────────────────────


class TestListPendingStories:
    @pytest.mark.asyncio
    async def test_no_sprint_status_returns_empty(self, make_executor):
        ex, _ = make_executor()
        result = await ex.list_pending_stories(thread_id=1)
        assert result == []

    @pytest.mark.asyncio
    async def test_reads_sprint_status_yaml(self, make_executor):
        ex, project_dir = make_executor()
        impl_dir = project_dir / "docs" / "implementation-artifacts"
        impl_dir.mkdir(parents=True)
        (impl_dir / "sprint-status.yaml").write_text(
            """
project: Test
development_status:
  epic-1: in-progress
  1-1-done-story: done
  1-2-pending-story: backlog
  1-3-ready-story: ready-for-dev
  1-4-in-progress-story: in-progress
""",
            encoding="utf-8",
        )
        result = await ex.list_pending_stories(thread_id=1)
        assert "1-1-done-story" not in result
        assert "epic-1" not in result  # epic rollups are filtered
        assert sorted(result) == sorted([
            "1-2-pending-story", "1-3-ready-story", "1-4-in-progress-story",
        ])

    @pytest.mark.asyncio
    async def test_malformed_yaml_returns_empty(self, make_executor):
        ex, project_dir = make_executor()
        impl_dir = project_dir / "docs" / "implementation-artifacts"
        impl_dir.mkdir(parents=True)
        (impl_dir / "sprint-status.yaml").write_text(
            "this is: not: valid: yaml: at: all: [", encoding="utf-8",
        )
        result = await ex.list_pending_stories(thread_id=1)
        assert result == []

    @pytest.mark.asyncio
    async def test_missing_development_status_returns_empty(self, make_executor):
        ex, project_dir = make_executor()
        impl_dir = project_dir / "docs" / "implementation-artifacts"
        impl_dir.mkdir(parents=True)
        (impl_dir / "sprint-status.yaml").write_text(
            "project: Test\nlast_updated: 2026-05-25\n",
            encoding="utf-8",
        )
        result = await ex.list_pending_stories(thread_id=1)
        assert result == []


# ── artifact_exists ───────────────────────────────────────────────────


class TestArtifactExists:
    @pytest.mark.asyncio
    async def test_resolves_relative_to_project_dir(self, make_executor):
        ex, project_dir = make_executor()
        target = project_dir / "docs" / "planning-artifacts" / "prd.md"
        target.parent.mkdir(parents=True)
        target.write_text("# PRD", encoding="utf-8")

        assert (await ex.artifact_exists(
            thread_id=1, path="docs/planning-artifacts/prd.md",
        )) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_missing(self, make_executor):
        ex, _ = make_executor()
        assert (await ex.artifact_exists(
            thread_id=1, path="docs/planning-artifacts/prd.md",
        )) is False

    @pytest.mark.asyncio
    async def test_absolute_path_honored(self, make_executor, tmp_path):
        ex, _ = make_executor()
        absolute = tmp_path / "outside.md"
        absolute.write_text("hi", encoding="utf-8")
        assert (await ex.artifact_exists(
            thread_id=1, path=str(absolute),
        )) is True


# ── is_validation_passed ──────────────────────────────────────────────


class TestIsValidationPassed:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_planning_artifacts_dir(self, make_executor):
        ex, _ = make_executor()
        assert (await ex.is_validation_passed(
            thread_id=1, skill="bmad-validate-prd",
        )) is False

    @pytest.mark.asyncio
    async def test_returns_true_when_validation_report_complete(self, make_executor):
        ex, project_dir = make_executor()
        planning = project_dir / "docs" / "planning-artifacts"
        planning.mkdir(parents=True)
        (planning / "prd-validation-report.md").write_text(
            "---\nvalidationStatus: COMPLETE\noverallStatus: Pass\n---\n# Report\n",
            encoding="utf-8",
        )
        assert (await ex.is_validation_passed(
            thread_id=1, skill="bmad-validate-prd",
        )) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_validation_in_progress(self, make_executor):
        ex, project_dir = make_executor()
        planning = project_dir / "docs" / "planning-artifacts"
        planning.mkdir(parents=True)
        (planning / "prd-validation-report.md").write_text(
            "---\nvalidationStatus: IN_PROGRESS\n---\n",
            encoding="utf-8",
        )
        assert (await ex.is_validation_passed(
            thread_id=1, skill="bmad-validate-prd",
        )) is False

    @pytest.mark.asyncio
    async def test_readiness_skill_recognizes_marker(self, make_executor):
        ex, project_dir = make_executor()
        planning = project_dir / "docs" / "planning-artifacts"
        planning.mkdir(parents=True)
        (planning / "readiness-report.md").write_text(
            "---\nreadinessStatus: COMPLETE\n---\n",
            encoding="utf-8",
        )
        assert (await ex.is_validation_passed(
            thread_id=1, skill="bmad-check-implementation-readiness",
        )) is True

    @pytest.mark.asyncio
    async def test_unknown_skill_returns_false(self, make_executor):
        ex, _ = make_executor()
        assert (await ex.is_validation_passed(
            thread_id=1, skill="bmad-some-other-skill",
        )) is False
