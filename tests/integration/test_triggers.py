"""Integration tests: parse each trigger file → classify root cause layer.

No LLM or Fivetran credentials needed. Tests the full signal→classification
pipeline: JSON load → Pydantic model_validate → score_layers → classify.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.context import _parse_signal
from orchestrator.classifier import score_layers, classify

_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_trigger(filename: str) -> dict:
    with open(_FIXTURES / filename) as f:
        return json.load(f)


def _run_classification(trigger: dict) -> dict:
    """Parse signals and return classify() result."""
    signals_raw = trigger["signals"]
    for raw in signals_raw:
        _parse_signal(raw)
    scores = score_layers(signals_raw)
    return classify(scores)


# ── Per-trigger tests ──────────────────────────────────────────────────────────

class TestTriggerParsing:
    """All signals in every trigger file must parse without Pydantic errors."""

    @pytest.mark.parametrize("filename", [
        "trigger.json",
        "trigger_cascade.json",
        "trigger_column_excluded.json",
    ])
    def test_signals_parse_cleanly(self, filename):
        trigger = _load_trigger(filename)
        for raw in trigger["signals"]:
            signal = _parse_signal(raw)
            assert signal is not None

    @pytest.mark.parametrize("filename", [
        "trigger.json",
        "trigger_cascade.json",
        "trigger_column_excluded.json",
    ])
    def test_classification_returns_valid_layer(self, filename):
        trigger = _load_trigger(filename)
        result = _run_classification(trigger)
        valid_layers = {"connector", "schema", "transformation", "data_quality", "ambiguous"}
        assert result["layer"] in valid_layers
        assert 0.0 <= result["confidence"] <= 1.0
        assert result["flag"] in ("HIGH", "MEDIUM", "LOW", "AMBIGUOUS")


class TestTriggerClassification:
    """Assert expected root-cause layer per trigger."""

    def test_connector_failure_trigger(self):
        trigger = _load_trigger("trigger.json")
        result = _run_classification(trigger)
        assert result["layer"] == "connector"

    def test_schema_cascade_trigger(self):
        trigger = _load_trigger("trigger_cascade.json")
        result = _run_classification(trigger)
        assert result["layer"] == "schema"

    def test_column_excluded_trigger(self):
        trigger = _load_trigger("trigger_column_excluded.json")
        result = _run_classification(trigger)
        assert result["layer"] == "schema"


class TestTriggerStructure:
    """Assert required trigger fields are present."""

    @pytest.mark.parametrize("filename", [
        "trigger.json",
        "trigger_cascade.json",
        "trigger_column_excluded.json",
    ])
    def test_has_reporter_and_signals(self, filename):
        trigger = _load_trigger(filename)
        assert "reporter" in trigger
        assert "signals" in trigger
        assert len(trigger["signals"]) >= 1

    @pytest.mark.parametrize("filename", [
        "trigger.json",
        "trigger_cascade.json",
        "trigger_column_excluded.json",
    ])
    def test_all_signals_have_type_severity_observed_at(self, filename):
        trigger = _load_trigger(filename)
        for signal in trigger["signals"]:
            assert "signal_type" in signal, f"{filename}: missing signal_type"
            assert "severity" in signal, f"{filename}: missing severity"
            assert "observed_at" in signal, f"{filename}: missing observed_at"
