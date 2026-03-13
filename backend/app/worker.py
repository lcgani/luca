from __future__ import annotations

from .workflows import get_container


def lambda_handler(event, _context):
    container = get_container()
    operation = event.get("operation")
    session_id = event.get("session_id")
    if not session_id:
        raise ValueError("session_id is required")

    if operation == "discover":
        probe_budget = event.get("probe_budget") or container.settings.default_probe_budget
        session = container.discovery_service.discover_session(session_id, probe_budget)
        return {"status": session.status, "phase": session.phase}

    if operation == "generate":
        session = container.generation_service.generate_session(session_id)
        return {"status": session.status, "phase": session.phase}

    raise ValueError(f"Unsupported operation: {operation}")
