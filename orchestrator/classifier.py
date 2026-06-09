from __future__ import annotations

LAYERS = ["connector", "schema", "transformation", "data_quality"]


def score_layers(signals: list[dict]) -> dict[str, float]:
    """
    Score each layer based on signal evidence.
    Returns raw (un-normalised) scores per layer.
    Called as an ADK tool by the orchestrator LLM.
    """
    scores = {layer: 0.0 for layer in LAYERS}

    for raw in signals:
        stype = raw.get("signal_type")

        if stype == "sync_status":
            _score_sync_status(raw, scores)
        elif stype == "schema_diff":
            _score_schema_diff(raw, scores)
        elif stype == "transformation_event":
            _score_transformation_event(raw, scores)
        elif stype == "data_quality_metric":
            _score_data_quality(raw, scores)
        elif stype == "row_count_delta":
            _score_row_count_delta(raw, scores)

    # Apply cross-signal confounders (require multiple signals)
    _apply_cross_signal_confounders(signals, scores)

    return scores


def classify(scores: dict[str, float]) -> dict:
    """
    Converts raw scores to {"layer": str, "confidence": float, "flag": str}.
    Applies tiebreak rules.
    Returns a dict (not a tuple) so the LLM receives named fields from the tool response.
    """
    if not any(v > 0 for v in scores.values()):
        return {"layer": "ambiguous", "confidence": 0.0, "flag": "AMBIGUOUS"}

    total = sum(v for v in scores.values() if v > 0)
    normalised = {k: v / total for k, v in scores.items()}
    sorted_layers = sorted(normalised.items(), key=lambda x: x[1], reverse=True)

    top_layer, top_conf = sorted_layers[0]
    second_layer, second_conf = sorted_layers[1] if len(sorted_layers) > 1 else ("", 0.0)

    # Tiebreak: if gap < 0.15, apply layer cascade rule (higher layer wins)
    if top_conf - second_conf < 0.15 and second_conf > 0:
        stack_order = {"connector": 0, "schema": 1, "transformation": 2, "data_quality": 3}
        top_layer = min(top_layer, second_layer, key=lambda x: stack_order.get(x, 99))
        top_conf = normalised[top_layer]

    if top_conf >= 0.65:
        flag = "HIGH" if top_conf >= 0.80 else "MEDIUM"
    elif top_conf >= 0.45:
        flag = "LOW"
    else:
        return {"layer": "ambiguous", "confidence": top_conf, "flag": "AMBIGUOUS"}

    return {"layer": top_layer, "confidence": top_conf, "flag": flag}


# ── Private scoring helpers ────────────────────────────────────────────────────

def _score_sync_status(s: dict, scores: dict) -> None:
    status = s.get("status", "")
    error_msg = (s.get("error_message") or "").lower()
    error_code = (s.get("error_code") or "").lower()

    # Primary connector signals
    if status in ("error", "broken"):
        scores["connector"] += 3
    if status == "paused":
        # Both user-paused and system-paused (auto-paused after repeated failures) are connector issues.
        # The distinction matters for remediation (user-pause: investigate intent;
        # system-pause: auth/network failure caused the auto-pause), not for classification.
        scores["connector"] += 3
    if any(p in error_msg or p in error_code for p in
           ["401", "403", "unauthorized", "token expired", "invalid credentials", "oauth",
            "forbidden", "auth_error", "credential"]):
        scores["connector"] += 3
    if any(p in error_msg or p in error_code for p in
           ["429", "rate limit", "quota exceeded", "too many requests"]):
        scores["connector"] += 3
    if any(p in error_msg for p in
           ["connection refused", "timeout", "etimedout", "host unreachable"]):
        scores["connector"] += 3

    # Secondary connector signals
    if s.get("rows_synced", -1) == 0 and status != "complete":
        scores["connector"] += 1
    if status == "incomplete":
        scores["connector"] += 1

    # Confounder: sync completed → not connector
    if status == "complete":
        scores["connector"] -= 2


def _score_schema_diff(s: dict, scores: dict) -> None:
    diff_type = s.get("diff_type", "")

    if diff_type in ("type_changed", "column_removed"):
        scores["schema"] += 3
    if diff_type == "column_excluded":
        scores["schema"] += 3
    if diff_type in ("column_added", "table_added", "table_removed"):
        scores["schema"] += 1


def _score_transformation_event(s: dict, scores: dict) -> None:
    error_type = s.get("error_type", "")
    status = s.get("status", "")

    # Schema confounders: these error types originate upstream in the schema layer,
    # not in transformation logic.
    if error_type in ("missing_relation", "type_mismatch"):
        scores["schema"] += 3
        scores["transformation"] -= 2
        return

    # Primary transformation signals
    if status == "failed" and error_type in ("sql_error", "timeout", "ordering_violation"):
        scores["transformation"] += 3
    if status == "stale":
        scores["transformation"] += 3

    # Secondary
    if status == "running":
        scores["transformation"] += 1


def _score_data_quality(s: dict, scores: dict) -> None:
    metric_type = s.get("metric_type", "")

    if metric_type == "null_rate":
        # Column-specific null surge — likely structural
        if s.get("column_name"):
            scores["schema"] += 1
            scores["data_quality"] += 3
        else:
            scores["data_quality"] += 3

    elif metric_type == "duplicate_rate":
        scores["data_quality"] += 3

    elif metric_type in ("value_range", "row_count"):
        scores["data_quality"] += 1


def _score_row_count_delta(s: dict, scores: dict) -> None:
    delta = abs(s.get("delta_pct", 0))
    if delta > 0.5:
        scores["data_quality"] += 3
    elif delta > 0.1:
        scores["data_quality"] += 1


def _apply_cross_signal_confounders(signals: list[dict], scores: dict) -> None:
    stypes = {s.get("signal_type") for s in signals}
    diff_types = {s.get("diff_type") for s in signals if s.get("signal_type") == "schema_diff"}
    error_types = {s.get("error_type") for s in signals if s.get("signal_type") == "transformation_event"}

    # Null rate + type_changed → schema is root cause, not data_quality
    if "data_quality_metric" in stypes and "type_changed" in diff_types:
        scores["data_quality"] -= 2
        scores["schema"] += 1

    # Null rate + column_excluded → schema is root cause
    if "data_quality_metric" in stypes and "column_excluded" in diff_types:
        scores["data_quality"] -= 2
        scores["schema"] += 1

    # Transformation failed → data quality anomaly is a symptom
    if "transformation_event" in stypes and "data_quality_metric" in stypes:
        if error_types & {"sql_error", "timeout", "ordering_violation"}:
            scores["data_quality"] -= 2

    # row_count_delta coincident with a failed/incomplete sync → connector is root cause;
    # the zero-row count is a downstream symptom of the sync not completing, not a DQ anomaly.
    sync_statuses = {s.get("status") for s in signals if s.get("signal_type") == "sync_status"}
    if "row_count_delta" in stypes and sync_statuses & {"error", "incomplete", "broken", "paused"}:
        scores["data_quality"] -= 2
        scores["connector"] += 1
