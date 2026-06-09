"""Unit tests for orchestrator/agent.py — _normalize_blast_radius."""
from __future__ import annotations

import pytest

from orchestrator.agent import _normalize_blast_radius


class TestNormalizeBlastRadius:
    def test_direct_connections_affected_preserved(self):
        br = {
            "connections_affected": ["conn_1", "conn_2"],
            "transformations_at_risk": ["xform_1"],
            "score": 3,
            "severity": "medium",
            "estimated_data_staleness_hours": 2.0,
            "assessment_reasoning": "Two connections failing.",
        }
        result = _normalize_blast_radius(br)
        assert result["connections_affected"] == ["conn_1", "conn_2"]

    def test_connections_total_expanded_to_list(self):
        br = {"connections_total": 3, "transformations_at_risk": []}
        result = _normalize_blast_radius(br)
        assert len(result["connections_affected"]) == 3
        assert "connections_total" not in result

    def test_num_connections_alias_works(self):
        br = {"num_connections": 2, "transformations_at_risk": []}
        result = _normalize_blast_radius(br)
        assert len(result["connections_affected"]) == 2

    def test_affected_connections_alias_works(self):
        br = {"affected_connections": 1, "transformations_at_risk": []}
        result = _normalize_blast_radius(br)
        assert len(result["connections_affected"]) == 1

    def test_transformations_total_expanded(self):
        br = {"connections_affected": ["conn_1"], "transformations_total": 4}
        result = _normalize_blast_radius(br)
        assert len(result["transformations_at_risk"]) == 4
        assert "transformations_total" not in result

    def test_score_computed_from_counts_if_missing(self):
        # 5 connections → 2 pts; 3 transformations → 2 pts → score = 4
        br = {
            "connections_affected": [f"c{i}" for i in range(5)],
            "transformations_at_risk": ["t1", "t2", "t3"],
        }
        result = _normalize_blast_radius(br)
        assert result["score"] == 4

    def test_score_single_connection_no_transformations(self):
        br = {
            "connections_affected": ["conn_1"],
            "transformations_at_risk": [],
        }
        result = _normalize_blast_radius(br)
        assert result["score"] == 0

    def test_severity_derived_from_score(self):
        test_cases = [
            (0, "low"),
            (2, "low"),
            (3, "medium"),
            (4, "medium"),
            (5, "high"),
            (6, "high"),
            (7, "critical"),
        ]
        for score, expected_severity in test_cases:
            br = {
                "connections_affected": ["c1"],
                "transformations_at_risk": [],
                "score": score,
            }
            result = _normalize_blast_radius(br)
            assert result["severity"] == expected_severity, f"score={score}"

    def test_existing_score_not_overwritten(self):
        br = {
            "connections_affected": ["c1"],
            "transformations_at_risk": [],
            "score": 99,
        }
        result = _normalize_blast_radius(br)
        assert result["score"] == 99

    def test_staleness_defaults_to_zero(self):
        br = {"connections_affected": ["c1"], "transformations_at_risk": []}
        result = _normalize_blast_radius(br)
        assert result["estimated_data_staleness_hours"] == 0.0

    def test_assessment_reasoning_defaults_set(self):
        br = {"connections_affected": ["c1"], "transformations_at_risk": []}
        result = _normalize_blast_radius(br)
        assert "assessment_reasoning" in result
        assert isinstance(result["assessment_reasoning"], str)

    def test_original_dict_not_mutated(self):
        br = {"connections_total": 2, "transformations_at_risk": []}
        original = dict(br)
        _normalize_blast_radius(br)
        # Function copies with dict(br), so original should not change
        assert br == original
