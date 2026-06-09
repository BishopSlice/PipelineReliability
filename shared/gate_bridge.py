"""Per-session gate resources for request_approval.

request_approval is an async function that blocks until the human decides.
It reads the emit function and gate queue from this module, keyed by session_id.
runner.py registers resources before each run and unregisters after.
"""
from __future__ import annotations

import asyncio

_bridges: dict[str, dict] = {}


def register(session_id: str, emit, gate_queue: asyncio.Queue) -> None:
    _bridges[session_id] = {"emit": emit, "gate_queue": gate_queue}


def unregister(session_id: str) -> None:
    _bridges.pop(session_id, None)


def get(session_id: str) -> dict | None:
    return _bridges.get(session_id)
