from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
import uuid


# ─── Signal Types ─────────────────────────────────────────────────────────────

class SyncStatusSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_type: Literal["sync_status"] = "sync_status"
    connection_id: str | None = None
    group_id: str | None = None
    observed_at: datetime
    severity: Literal["info", "warning", "error", "critical"]
    status: Literal["syncing", "scheduled", "rescheduled", "stopped", "delayed", "complete", "error", "paused", "incomplete", "broken"]
    last_sync_start: datetime | None = None
    last_sync_end: datetime | None = None
    rows_synced: int = 0
    rows_expected: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    sync_duration_seconds: int | None = None
    paused_by: str | None = None


class SchemaDiffSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_type: Literal["schema_diff"] = "schema_diff"
    connection_id: str | None = None
    group_id: str | None = None
    observed_at: datetime
    severity: Literal["info", "warning", "error", "critical"]
    diff_type: Literal[
        "column_added", "column_removed", "type_changed",
        "column_excluded", "table_added", "table_removed"
    ]
    schema_name: str
    table_name: str
    column_name: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    detected_at: datetime


class TransformationEventSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_type: Literal["transformation_event"] = "transformation_event"
    transformation_id: str | None = None
    connection_id: str | None = None
    observed_at: datetime
    severity: Literal["info", "warning", "error", "critical"]
    status: Literal["failed", "succeeded", "cancelled", "running", "stale"]
    error_message: str | None = None
    error_type: Literal[
        "sql_error", "timeout", "missing_relation",
        "type_mismatch", "ordering_violation"
    ] | None = None
    models_failed: list[str] = Field(default_factory=list)
    run_duration_ms: int | None = None
    last_success_at: datetime | None = None


class DataQualityMetricSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_type: Literal["data_quality_metric"] = "data_quality_metric"
    connection_id: str | None = None
    observed_at: datetime
    severity: Literal["info", "warning", "error", "critical"]
    metric_type: Literal["null_rate", "duplicate_rate", "value_range", "row_count"]
    table_name: str
    column_name: str | None = None
    current_value: float
    baseline_value: float
    threshold: float
    unit: str = "rate"


class RowCountDeltaSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_type: Literal["row_count_delta"] = "row_count_delta"
    connection_id: str | None = None
    observed_at: datetime
    severity: Literal["info", "warning", "error", "critical"]
    table_name: str
    actual_rows: int
    expected_rows: int
    delta_pct: float


# Union type for all signals
Signal = (
    SyncStatusSignal
    | SchemaDiffSignal
    | TransformationEventSignal
    | DataQualityMetricSignal
    | RowCountDeltaSignal
)


# ─── Incident Trigger ─────────────────────────────────────────────────────────

class IncidentTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    received_at: datetime = Field(default_factory=datetime.utcnow)
    signals: list[Signal] = Field(min_length=1)
    reporter: Literal["monitoring_system", "human", "scheduled_poll"]
    notes: str | None = None


# ─── Incident Context Components ──────────────────────────────────────────────

class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classified_layer: Literal[
        "connector", "schema", "transformation", "data_quality", "ambiguous"
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_flag: Literal["HIGH", "MEDIUM", "LOW", "AMBIGUOUS"]
    layer_scores: dict[str, float]
    reasoning: str
    committed_at: datetime | None = None


class BlastRadius(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int = Field(ge=0)
    severity: Literal["low", "medium", "high", "critical"]
    connections_affected: list[str]
    transformations_at_risk: list[str]
    estimated_data_staleness_hours: float
    assessment_reasoning: str


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_called: str
    observation: str                    # 1-2 sentences max — keep IncidentContext under 8KB
    supports_hypothesis: bool


class ActionTaken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_called: str
    parameters: dict[str, Any]
    outcome: str
    autonomous: bool


class ApprovalGateRequest(BaseModel):
    """What a sub-agent populates in actions_queued_for_approval."""
    model_config = ConfigDict(extra="forbid")
    gate_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tool_to_call: str
    parameters: dict[str, Any]
    current_state: str
    proposed_action: str
    expected_outcome: str
    risk_assessment: str
    alternative_if_denied: str | None = None


class ApprovalGate(ApprovalGateRequest):
    """Full gate object stored in IncidentContext.approval_gates (includes decision)."""
    model_config = ConfigDict(extra="forbid")
    gate_id: str
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    decided_at: datetime | None = None
    decision: Literal["approved", "denied", "pending", "timed_out"] = "pending"
    decided_by: str | None = None


class ValidationSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    observed: bool = False
    observed_value: Any = None
    baseline_value: Any = None
    recovery_confirmed: bool = False


# ─── Handoff Context Shapes ───────────────────────────────────────────────────

class ConnectorToSchemaHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_is_healthy: bool
    last_successful_sync_at: datetime | None
    rows_synced: int
    connection_ids_confirmed_syncing: list[str]
    connection_ids_in_error: list[str]
    error_codes_observed: list[str]
    suspicion: str | None = None        # why connector agent suspects schema (None when connector is the root cause)


class SchemaToTransformationHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_changes_detected: list[dict[str, Any]]   # [{connection_id, table, column, change_type, old_value, new_value}]
    transformations_likely_affected: list[str]
    schema_fix_status: Literal["applied", "pending_approval", "not_required"]
    downstream_expectation: str
    advisory: str | None = None


class SchemaToDataQualityHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    null_surge_columns: list[str]
    type_change_details: list[dict[str, str]]   # [{column, old_type, new_type, expected_impact}]


class TransformationToDataQualityHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transformation_status: Literal["resolved", "still_failing", "partial"]
    models_now_healthy: list[str]
    models_still_failing: list[str]
    output_tables_to_validate: list[str]
    expected_null_rate_after_fix: float | None = None
    last_run_row_count: int | None = None


# ─── Agent Finding ────────────────────────────────────────────────────────────

class AgentFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: Literal["connector", "schema", "transformation", "data_quality"]
    dispatched_at: datetime
    completed_at: datetime | None = None
    status: Literal["running", "complete", "escalated", "handed_off", "partial"]
    root_cause_hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence]
    actions_taken: list[ActionTaken] = Field(default_factory=list)
    actions_queued_for_approval: list[ApprovalGateRequest] = Field(default_factory=list)
    upstream_root_cause: Literal["connector", "schema", "transformation", None] = None
    upstream_root_cause_confidence: float | None = None
    handoff_context: (
        ConnectorToSchemaHandoff
        | SchemaToTransformationHandoff
        | SchemaToDataQualityHandoff
        | TransformationToDataQualityHandoff
        | None
    ) = None
    validation_signal: ValidationSignal | None = None
    reasoning_trace: list[str]          # one sentence per inference step


# ─── Full IncidentContext ─────────────────────────────────────────────────────

class IncidentContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    initial_signals: list[Signal]
    classification: Classification | None = None
    blast_radius: BlastRadius | None = None
    agent_findings: list[AgentFinding] = Field(default_factory=list)
    approval_gates: list[ApprovalGate] = Field(default_factory=list)
    iteration_count: int = 0
    max_iterations: int = 3
    resolved: bool = False
    escalated: bool = False
    resolution_summary: str | None = None
    escalation_reason: str | None = None


# ─── Incident Summary (Orchestrator output at closure) ────────────────────────

class IncidentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_id: str
    status: Literal["resolved", "escalated", "partial"]
    root_cause_layer: str
    root_cause_hypothesis: str
    confidence: float
    actions_taken: list[str]
    actions_pending: list[str]
    recovery_confirmed: bool
    resolution_summary: str | None = None
    escalation_reason: str | None = None
    total_duration_seconds: float
    reasoning_trace: list[str]
