"""Unit tests for orchestrator/classifier.py — score_layers and classify."""
from __future__ import annotations

import pytest

from orchestrator.classifier import score_layers, classify, LAYERS


# ── score_layers ───────────────────────────────────────────────────────────────

class TestScoreLayers:
    def test_returns_all_four_layers(self):
        result = score_layers([])
        assert set(result.keys()) == set(LAYERS)

    def test_empty_signals_all_zero(self):
        result = score_layers([])
        assert all(v == 0.0 for v in result.values())

    # sync_status

    def test_sync_status_error_scores_connector(self):
        s = [{"signal_type": "sync_status", "status": "error"}]
        scores = score_layers(s)
        assert scores["connector"] > 0
        assert scores["schema"] == 0

    def test_sync_status_auth_error_scores_connector(self):
        s = [{"signal_type": "sync_status", "status": "error", "error_code": "401"}]
        scores = score_layers(s)
        assert scores["connector"] >= 6  # 3 (error) + 3 (auth)

    def test_sync_status_complete_subtracts_connector(self):
        s = [{"signal_type": "sync_status", "status": "complete"}]
        scores = score_layers(s)
        assert scores["connector"] < 0

    def test_sync_status_incomplete_adds_connector(self):
        s = [{"signal_type": "sync_status", "status": "incomplete"}]
        scores = score_layers(s)
        assert scores["connector"] > 0

    def test_sync_status_rate_limit_scores_connector(self):
        s = [{"signal_type": "sync_status", "status": "error", "error_message": "429 rate limit exceeded"}]
        scores = score_layers(s)
        assert scores["connector"] >= 6

    # schema_diff

    def test_schema_diff_type_changed_scores_schema(self):
        s = [{"signal_type": "schema_diff", "diff_type": "type_changed"}]
        scores = score_layers(s)
        assert scores["schema"] == 3

    def test_schema_diff_column_removed_scores_schema(self):
        s = [{"signal_type": "schema_diff", "diff_type": "column_removed"}]
        scores = score_layers(s)
        assert scores["schema"] == 3

    def test_schema_diff_column_added_light_score(self):
        s = [{"signal_type": "schema_diff", "diff_type": "column_added"}]
        scores = score_layers(s)
        assert scores["schema"] == 1

    # transformation_event

    def test_transformation_sql_error_scores_transformation(self):
        s = [{"signal_type": "transformation_event", "status": "failed", "error_type": "sql_error"}]
        scores = score_layers(s)
        assert scores["transformation"] > 0

    def test_transformation_missing_relation_scores_schema(self):
        s = [{"signal_type": "transformation_event", "status": "failed", "error_type": "missing_relation"}]
        scores = score_layers(s)
        assert scores["schema"] > scores["transformation"]

    def test_transformation_type_mismatch_scores_schema(self):
        s = [{"signal_type": "transformation_event", "status": "failed", "error_type": "type_mismatch"}]
        scores = score_layers(s)
        assert scores["schema"] > 0
        assert scores["transformation"] < 0  # penalised

    def test_transformation_stale_scores_transformation(self):
        s = [{"signal_type": "transformation_event", "status": "stale"}]
        scores = score_layers(s)
        assert scores["transformation"] == 3

    # data_quality

    def test_dq_duplicate_rate_scores_data_quality(self):
        s = [{"signal_type": "data_quality_metric", "metric_type": "duplicate_rate",
              "current_value": 0.15, "baseline_value": 0.01, "threshold": 0.05}]
        scores = score_layers(s)
        assert scores["data_quality"] == 3

    def test_dq_null_rate_with_column_scores_dq_and_schema(self):
        s = [{"signal_type": "data_quality_metric", "metric_type": "null_rate",
              "column_name": "revenue", "current_value": 0.5, "baseline_value": 0.0, "threshold": 0.1}]
        scores = score_layers(s)
        assert scores["data_quality"] == 3
        assert scores["schema"] == 1

    # row_count_delta

    def test_row_count_large_delta_scores_dq(self):
        s = [{"signal_type": "row_count_delta", "delta_pct": 1.0}]
        scores = score_layers(s)
        assert scores["data_quality"] == 3

    def test_row_count_small_delta_light_score(self):
        s = [{"signal_type": "row_count_delta", "delta_pct": 0.2}]
        scores = score_layers(s)
        assert scores["data_quality"] == 1

    # cross-signal confounders

    def test_dq_plus_type_changed_boosts_schema(self):
        signals = [
            {"signal_type": "data_quality_metric", "metric_type": "null_rate",
             "current_value": 0.5, "baseline_value": 0.0, "threshold": 0.1},
            {"signal_type": "schema_diff", "diff_type": "type_changed"},
        ]
        scores = score_layers(signals)
        # schema should outpoint data_quality
        assert scores["schema"] > scores["data_quality"]

    def test_dq_plus_column_excluded_boosts_schema(self):
        signals = [
            {"signal_type": "data_quality_metric", "metric_type": "null_rate",
             "current_value": 0.5, "baseline_value": 0.0, "threshold": 0.1},
            {"signal_type": "schema_diff", "diff_type": "column_excluded"},
        ]
        scores = score_layers(signals)
        assert scores["schema"] > scores["data_quality"]

    def test_transformation_sql_error_penalises_dq(self):
        signals = [
            {"signal_type": "transformation_event", "status": "failed", "error_type": "sql_error"},
            {"signal_type": "data_quality_metric", "metric_type": "duplicate_rate",
             "current_value": 0.2, "baseline_value": 0.01, "threshold": 0.05},
        ]
        base_dq = score_layers([signals[1]])["data_quality"]
        with_xform = score_layers(signals)
        assert with_xform["data_quality"] < base_dq


# ── classify ───────────────────────────────────────────────────────────────────

class TestClassify:
    def test_all_zero_scores_ambiguous(self):
        result = classify({l: 0.0 for l in LAYERS})
        assert result["layer"] == "ambiguous"
        assert result["flag"] == "AMBIGUOUS"

    def test_high_connector_score(self):
        scores = {"connector": 9.0, "schema": 1.0, "transformation": 0.0, "data_quality": 0.0}
        result = classify(scores)
        assert result["layer"] == "connector"
        assert result["flag"] == "HIGH"

    def test_medium_confidence_flag(self):
        # connector 65%, schema 35% → connector MEDIUM
        scores = {"connector": 6.5, "schema": 3.5, "transformation": 0.0, "data_quality": 0.0}
        result = classify(scores)
        assert result["layer"] == "connector"
        assert result["flag"] == "MEDIUM"

    def test_tiebreak_prefers_lower_layer(self):
        # connector and schema nearly tied — connector wins (lower in stack)
        scores = {"connector": 5.0, "schema": 4.8, "transformation": 0.0, "data_quality": 0.0}
        result = classify(scores)
        assert result["layer"] == "connector"

    def test_tiebreak_schema_vs_transformation(self):
        # schema and transformation nearly tied — schema wins
        scores = {"connector": 0.0, "schema": 5.0, "transformation": 4.8, "data_quality": 0.0}
        result = classify(scores)
        assert result["layer"] == "schema"

    def test_large_gap_no_tiebreak(self):
        # transformation clearly wins — no tiebreak
        scores = {"connector": 0.0, "schema": 1.0, "transformation": 8.0, "data_quality": 0.0}
        result = classify(scores)
        assert result["layer"] == "transformation"

    def test_low_confidence_flag(self):
        # Very even distribution → low confidence
        scores = {"connector": 3.0, "schema": 2.5, "transformation": 2.0, "data_quality": 1.5}
        result = classify(scores)
        # Total = 9, connector = 3/9 = 0.33 → AMBIGUOUS
        assert result["flag"] in ("AMBIGUOUS", "LOW")

    def test_confidence_in_range(self):
        scores = {"connector": 9.0, "schema": 1.0, "transformation": 0.0, "data_quality": 0.0}
        result = classify(scores)
        assert 0.0 <= result["confidence"] <= 1.0
