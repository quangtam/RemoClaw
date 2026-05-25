"""Tests for tier-based model resolution.

Flow YAMLs use portable tier names (strong/balanced/fast). Each provider
maps those to its actual model names. This decouples flow definitions
from the specific CLI provider, so the same flow runs on Kiro, Claude,
Gemini, or Codex without modification.
"""

import pytest

from cli_providers import create_provider
from cli_providers.base import CliProvider, CliProviderConfig


class TestResolveTier:
    def _make(self, provider_name: str) -> CliProvider:
        return create_provider(provider_name=provider_name)

    def test_kiro_tiers(self):
        p = self._make("kiro")
        assert p.resolve_tier("strong") == "claude-opus-4.7"
        assert p.resolve_tier("balanced") == "claude-sonnet-4.6"
        assert p.resolve_tier("fast") == "claude-haiku-4.5"

    def test_claude_tiers(self):
        p = self._make("claude")
        assert p.resolve_tier("strong") == "opus"
        assert p.resolve_tier("balanced") == "sonnet"
        assert p.resolve_tier("fast") == "haiku"

    def test_gemini_tiers(self):
        p = self._make("gemini")
        assert p.resolve_tier("strong") == "gemini-2.5-pro"
        assert p.resolve_tier("balanced") == "gemini-2.5-flash"
        assert p.resolve_tier("fast") == "gemini-2.5-flash-lite"

    def test_codex_tiers(self):
        p = self._make("codex")
        assert p.resolve_tier("strong") == "gpt-5"
        # Empty mapping → None means "use CLI default"
        assert p.resolve_tier("balanced") is None
        assert p.resolve_tier("fast") is None

    def test_real_model_passes_through(self):
        # If user passes an actual model name (not a tier), it should pass through
        p = self._make("kiro")
        assert p.resolve_tier("claude-opus-4.5") == "claude-opus-4.5"

    def test_none_returns_none(self):
        p = self._make("kiro")
        assert p.resolve_tier(None) is None

    def test_empty_string_returns_none(self):
        p = self._make("kiro")
        assert p.resolve_tier("") is None

    def test_tier_resolution_is_classmethod(self):
        # Should work without instantiation when used internally
        from cli_providers.kiro import KiroProvider
        assert KiroProvider.resolve_tier("strong") == "claude-opus-4.7"


class TestFlowsUseTiers:
    """Ensure the shipped flow YAMLs use tiers, not provider-specific names."""

    def test_quick_dev_uses_only_tiers(self):
        from pathlib import Path

        from auto.flow import load_flow

        flows_dir = Path(__file__).resolve().parent.parent / "_bmad" / "flows"
        if not flows_dir.is_dir():
            pytest.skip("flows dir not found")

        flow = load_flow("quick-dev", flows_dir=flows_dir)
        valid_tiers = {None, "", "strong", "balanced", "fast"}
        for phase in flow.phases:
            for step in phase.steps:
                # `model` should be None or one of the tier names
                assert step.model in valid_tiers, (
                    f"Step '{step.id}' uses non-tier model '{step.model}'. "
                    f"Use 'strong', 'balanced', or 'fast' instead."
                )

    def test_full_uses_only_tiers(self):
        from pathlib import Path

        from auto.flow import load_flow

        flows_dir = Path(__file__).resolve().parent.parent / "_bmad" / "flows"
        if not flows_dir.is_dir():
            pytest.skip("flows dir not found")

        flow = load_flow("full", flows_dir=flows_dir)
        valid_tiers = {None, "", "strong", "balanced", "fast"}
        for phase in flow.phases:
            for step in phase.steps:
                assert step.model in valid_tiers
                # Also check substeps for loop steps
                for sub in step.substeps:
                    assert sub.model in valid_tiers
