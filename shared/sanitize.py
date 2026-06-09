from __future__ import annotations

import copy
import logging
import re

logger = logging.getLogger(__name__)

# ── Max lengths ────────────────────────────────────────────────────────────────
_MAX_FREE_TEXT = 500     # notes, error_message — arbitrary user prose
_MAX_IDENTIFIER = 128    # connection_id, transformation_id, group_id
_MAX_NAME = 255          # schema_name, table_name, column_name, value fields

# Fields that accept arbitrary prose and are the highest-risk injection vectors
_FREE_TEXT_FIELDS = {"notes", "error_message", "assessment_reasoning", "description"}

# Fields that are structured identifiers — low semantic risk, just cap them
_IDENTIFIER_FIELDS = {
    "connection_id", "group_id", "transformation_id",
    "paused_by", "reporter",
}

# ── Injection pattern detection ───────────────────────────────────────────────
# Matches common LLM prompt-hijacking phrases. Case-insensitive.
_INJECTION_RE = re.compile(
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?"
    r"|you\s+are\s+now\s+(a|an)\s"
    r"|forget\s+(everything|all(\s+you\s+know)?|above|prior)"
    r"|new\s+(system\s+)?role\s*:"
    r"|<\s*/?\s*system\s*>"
    r"|\bact\s+as\b.{0,30}\bai\b"
    r"|\bjailbreak\b",
    re.IGNORECASE,
)

# Strip non-printable control characters (null bytes, escape sequences, etc.)
# Keep tabs (\x09), newlines (\x0a), carriage returns (\x0d) as they're legitimate.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_trigger(payload: dict) -> dict:
    """
    Return a sanitized deep copy of the trigger payload.

    - Truncates all string fields to context-appropriate max lengths.
    - Strips non-printable control characters from every string.
    - Scans free-text fields (notes, error_message) for prompt injection patterns
      and replaces the offending content with a redaction notice.

    Never raises — if anything unexpected happens the original payload is returned
    unmodified so the run is not blocked by the sanitizer.
    """
    try:
        payload = copy.deepcopy(payload)
        _sanitize_dict(payload, path="trigger")
        return payload
    except Exception as exc:
        logger.warning("sanitize_trigger: unexpected error (%s) — using raw payload", exc)
        return payload


def _sanitize_dict(d: dict, path: str) -> None:
    for key, value in d.items():
        if isinstance(value, str):
            d[key] = _sanitize_field(key, value, path=f"{path}.{key}")
        elif isinstance(value, dict):
            _sanitize_dict(value, path=f"{path}.{key}")
        elif isinstance(value, list):
            _sanitize_list(value, path=f"{path}.{key}")


def _sanitize_list(lst: list, path: str) -> None:
    for i, item in enumerate(lst):
        if isinstance(item, dict):
            _sanitize_dict(item, path=f"{path}[{i}]")
        elif isinstance(item, str):
            lst[i] = _sanitize_field("", item, path=f"{path}[{i}]")


def _sanitize_field(key: str, value: str, path: str) -> str:
    # 1. Strip control characters
    value = _CONTROL_RE.sub("", value)

    # 2. Truncate to field-appropriate max
    if key in _FREE_TEXT_FIELDS:
        max_len = _MAX_FREE_TEXT
    elif key in _IDENTIFIER_FIELDS:
        max_len = _MAX_IDENTIFIER
    else:
        max_len = _MAX_NAME

    if len(value) > max_len:
        logger.info("sanitize_trigger: truncated %s (%d → %d chars)", path, len(value), max_len)
        value = value[:max_len]

    # 3. Injection scan — only for free-text fields (structured names won't match)
    if key in _FREE_TEXT_FIELDS and _INJECTION_RE.search(value):
        logger.warning("sanitize_trigger: injection pattern detected in %s — redacting", path)
        value = "[redacted: content matched prompt injection pattern]"

    return value
