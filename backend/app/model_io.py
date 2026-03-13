from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError


class ModelResponseError(ValueError):
    """Raised when a model response cannot be parsed into the required shape."""


def strip_code_fences(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def parse_model_json(raw: str) -> Any:
    stripped = strip_code_fences(raw)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start_object = stripped.find("{")
        start_list = stripped.find("[")
        starts = [position for position in (start_object, start_list) if position >= 0]
        if not starts:
            raise ModelResponseError("Model response did not contain JSON.") from None
        start = min(starts)
        trimmed = stripped[start:]
        for end in range(len(trimmed), 0, -1):
            candidate = trimmed[:end]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise ModelResponseError("Model response did not contain valid JSON.")


def require_object(payload: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ModelResponseError(f"{context} must be a JSON object.")
    return payload


def coerce_string_list(value: Any, *, limit: int, lower: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []

    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized:
            continue
        if lower:
            normalized = normalized.lower()
        if normalized not in cleaned:
            cleaned.append(normalized)
        if len(cleaned) >= limit:
            break
    return cleaned


def coerce_string_map(value: Any, *, limit: int) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, (str, int, float, bool)):
            cleaned[key.strip()] = str(item).strip()
        if len(cleaned) >= limit:
            break
    return {key: item for key, item in cleaned.items() if key and item}


def validate_model_list(value: Any, model: type[BaseModel], *, limit: int) -> list[BaseModel]:
    if not isinstance(value, list):
        return []

    validated: list[BaseModel] = []
    for item in value:
        try:
            validated.append(model.model_validate(item))
        except ValidationError:
            continue
        if len(validated) >= limit:
            break
    return validated
