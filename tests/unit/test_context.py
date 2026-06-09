"""Unit tests for shared/context.py — coercion, strip, trim."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from shared.context import (
    _coerce_agent_finding,
    _strip_markdown_json,
    _trim_context_if_needed,
    after_agent_callback,
    INCIDENT_CONTEXT_KEY,
)
from models import AgentFinding, IncidentContext, SyncStatusSignal


# ── _coerce_agent_finding ──────────────────────────────────────────────────────

def _base_finding() -> dict:
    return {
        "agent_id": "connector",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:01:00Z",
        "status": "complete",
        "root_cause_hypothesis": "Auth token expired.",
        "confidence": 0.9,
        "evidence": [],
        "actions_taken": [],
        "actions_queued_for_approval": [],
        "upstream_root_cause": None,
        "upstream_root_cause_confidence": None,
        "handoff_context": None,
        "validation_signal": None,
        "reasoning_trace": [],
    }


class TestCoerceAgentFinding:
    def test_valid_upstream_passes_through(self):
        raw = _base_finding()
        raw["upstream_root_cause"] = "schema"
        _coerce_agent_finding(raw)
        assert raw["upstream_root_cause"] == "schema"

    def test_invalid_upstream_nulled(self):
        raw = _base_finding()
        raw["upstream_root_cause"] = "fivetran_api"
        _coerce_agent_finding(raw)
        assert raw["upstream_root_cause"] is None

    def test_none_upstream_passes_through(self):
        raw = _base_finding()
        raw["upstream_root_cause"] = None
        _coerce_agent_finding(raw)
        assert raw["upstream_root_cause"] is None

    def test_invalid_agent_id_reset_to_connector(self):
        raw = _base_finding()
        raw["agent_id"] = "fivetran_mcp"
        _coerce_agent_finding(raw)
        assert raw["agent_id"] == "connector"

    def test_valid_agent_id_passes_through(self):
        for agent_id in ("connector", "schema", "transformation", "data_quality"):
            raw = _base_finding()
            raw["agent_id"] = agent_id
            _coerce_agent_finding(raw)
            assert raw["agent_id"] == agent_id

    def test_validation_signal_with_none_description_nulled(self):
        raw = _base_finding()
        raw["validation_signal"] = {
            "description": None,
            "observed": False,
            "observed_value": None,
            "baseline_value": None,
            "recovery_confirmed": False,
        }
        _coerce_agent_finding(raw)
        assert raw["validation_signal"] is None

    def test_validation_signal_with_none_observed_nulled(self):
        raw = _base_finding()
        raw["validation_signal"] = {
            "description": "rows_synced > 0",
            "observed": None,
            "observed_value": None,
            "baseline_value": None,
            "recovery_confirmed": False,
        }
        _coerce_agent_finding(raw)
        assert raw["validation_signal"] is None

    def test_validation_signal_valid_passes_through(self):
        raw = _base_finding()
        raw["validation_signal"] = {
            "description": "rows_synced > 0",
            "observed": True,
            "observed_value": 1000,
            "baseline_value": 5000,
            "recovery_confirmed": True,
        }
        _coerce_agent_finding(raw)
        assert raw["validation_signal"]["description"] == "rows_synced > 0"

    def test_handoff_context_valid_connector_to_schema_passes_through(self):
        raw = _base_finding()
        raw["handoff_context"] = {
            "connector_is_healthy": True,
            "last_successful_sync_at": "2026-01-01T00:00:00Z",
            "rows_synced": 1000,
            "connection_ids_confirmed_syncing": ["conn_1"],
            "connection_ids_in_error": [],
            "error_codes_observed": [],
        }
        _coerce_agent_finding(raw)
        assert raw["handoff_context"] is not None

    def test_handoff_context_junk_nulled(self):
        raw = _base_finding()
        raw["handoff_context"] = {"completely": "wrong", "structure": True}
        _coerce_agent_finding(raw)
        assert raw["handoff_context"] is None


# ── _strip_markdown_json ───────────────────────────────────────────────────────

class TestStripMarkdownJson:
    def test_strips_json_fence(self):
        text = '```json\n{"key": "value"}\n```'
        assert _strip_markdown_json(text) == '{"key": "value"}'

    def test_strips_plain_fence(self):
        text = '```\n{"key": "value"}\n```'
        assert _strip_markdown_json(text) == '{"key": "value"}'

    def test_plain_json_unchanged(self):
        text = '{"key": "value"}'
        assert _strip_markdown_json(text) == '{"key": "value"}'

    def test_whitespace_stripped(self):
        text = '   {"key": "value"}   '
        assert _strip_markdown_json(text) == '{"key": "value"}'

    def test_multiline_json_preserved(self):
        text = '```json\n{\n  "key": "value"\n}\n```'
        result = _strip_markdown_json(text)
        assert '"key"' in result
        assert result.startswith("{")


# ── _trim_context_if_needed ────────────────────────────────────────────────────

def _make_finding(agent_id: str = "connector", n: int = 0) -> AgentFinding:
    return AgentFinding(
        agent_id=agent_id,
        dispatched_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
        status="complete",
        root_cause_hypothesis=f"Hypothesis {n}",
        confidence=0.8,
        evidence=[],
        actions_taken=[],
        actions_queued_for_approval=[],
        reasoning_trace=[],
    )


def _make_context(n_findings: int, agent_id: str = "connector") -> IncidentContext:
    signal = SyncStatusSignal(
        observed_at=datetime.utcnow(),
        severity="error",
        status="error",
        connection_id="conn_1",
    )
    ctx = IncidentContext(initial_signals=[signal])
    ctx.agent_findings = [_make_finding(agent_id, i) for i in range(n_findings)]
    return ctx


class TestTrimContextIfNeeded:
    def test_under_cap_unchanged(self):
        ctx = _make_context(5)
        _trim_context_if_needed(ctx)
        assert len(ctx.agent_findings) == 5

    def test_at_cap_unchanged(self):
        ctx = _make_context(12)
        _trim_context_if_needed(ctx)
        assert len(ctx.agent_findings) == 12

    def test_over_cap_truncated_to_12(self):
        ctx = _make_context(15)
        # Mark last one so we can verify it's kept
        ctx.agent_findings[-1].root_cause_hypothesis = "LATEST"
        _trim_context_if_needed(ctx)
        assert len(ctx.agent_findings) == 12
        assert ctx.agent_findings[-1].root_cause_hypothesis == "LATEST"

    def test_oldest_dropped_not_newest(self):
        ctx = _make_context(14)
        ctx.agent_findings[0].root_cause_hypothesis = "OLDEST"
        _trim_context_if_needed(ctx)
        hypotheses = [f.root_cause_hypothesis for f in ctx.agent_findings]
        assert "OLDEST" not in hypotheses

    def test_size_trim_compacts_duplicates(self):
        ctx = _make_context(8)
        # Force large evidence on every finding to exceed 6KB
        for f in ctx.agent_findings:
            f.evidence = [
                type("E", (), {
                    "__dict__": {"tool_called": f"tool_{i}", "observation": "x" * 300, "supports_hypothesis": True}
                })()
                for i in range(5)
            ]
            f.reasoning_trace = [f"step {i}: " + "x" * 100 for i in range(10)]
        # Build proper Evidence objects
        from models import Evidence
        for f in ctx.agent_findings:
            f.evidence = [Evidence(tool_called=f"tool_{i}", observation="x" * 300, supports_hypothesis=True) for i in range(5)]
            f.reasoning_trace = [f"step {i}: " + "x" * 100 for i in range(10)]
        original_count = len(ctx.agent_findings)
        _trim_context_if_needed(ctx)
        # Count is preserved; older duplicate-agent entries are compacted
        assert len(ctx.agent_findings) == original_count
        # Latest (last) finding should still have full evidence
        assert len(ctx.agent_findings[-1].evidence) == 5


# ── after_agent_callback — empty string guard ──────────────────────────────────

class TestAfterAgentCallbackEmptyGuard:
    def _make_callback_context(self, output_value):
        ctx = MagicMock()
        ctx.agent_name = "connector_agent"
        ctx.state.get.return_value = output_value
        return ctx

    def test_none_output_returns_none(self):
        ctx = self._make_callback_context(None)
        result = after_agent_callback(ctx)
        assert result is None

    def test_empty_string_returns_none(self):
        ctx = self._make_callback_context("")
        result = after_agent_callback(ctx)
        assert result is None
