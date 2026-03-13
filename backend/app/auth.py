from __future__ import annotations

import json
from typing import Any

from .bedrock import BedrockRuntime, BedrockUnavailableError
from .model_io import coerce_string_map, parse_model_json, require_object, validate_model_list
from .models import AuthInput, AuthResult, AuthSignal


def _replace_placeholders(value: str, auth_input: AuthInput | None) -> str:
    if not auth_input or not auth_input.token:
        return value

    replacements = {
        "<token>": auth_input.token,
        "<api-key>": auth_input.token,
        "<api_key>": auth_input.token,
        "<bearer-token>": auth_input.token,
        "{{TOKEN}}": auth_input.token,
        "{{API_KEY}}": auth_input.token,
    }
    rendered = value
    for placeholder, replacement in replacements.items():
        rendered = rendered.replace(placeholder, replacement)
    return rendered


class HybridAuthClassifier:
    def __init__(self, runtime: BedrockRuntime):
        self.runtime = runtime

    def signals_from_headers(self, headers: dict[str, str], status_code: int, source: str) -> list[AuthSignal]:
        return self._signals_from_evidence(
            evidence_type="response_headers",
            source=source,
            evidence={
                "status_code": status_code,
                "headers": {key.lower(): value for key, value in headers.items()},
            },
        )

    def signals_from_text(self, text: str, source: str) -> list[AuthSignal]:
        return self._signals_from_evidence(
            evidence_type="source_text",
            source=source,
            evidence={"text": text[:4000]},
        )

    def signals_from_openapi(self, spec: dict[str, Any], source: str) -> list[AuthSignal]:
        return self._signals_from_evidence(
            evidence_type="openapi_spec",
            source=source,
            evidence={"spec": spec},
        )

    def build_auth_attempt(
        self,
        auth_input: AuthInput | None,
        variant: str,
        path: str,
        context: str,
    ) -> tuple[dict[str, str], dict[str, str]]:
        if not auth_input:
            return {}, {}
        if not self.runtime.text_model_enabled:
            raise BedrockUnavailableError("LUCA auth attempt construction requires Amazon Nova.")

        user_prompt = json.dumps(
            {
                "variant": variant,
                "path": path,
                "context": context,
                "auth_input": {
                    "has_token": bool(auth_input.token),
                    "header_name": auth_input.header_name,
                    "header_prefix": auth_input.header_prefix,
                    "query_param": auth_input.query_param,
                    "extra_headers": auth_input.extra_headers,
                },
                "instruction": (
                    "Return JSON with headers and query_params only. "
                    "Use placeholders like <token> or <api-key> when the user token should be inserted."
                ),
            },
            indent=2,
        )
        raw = self.runtime.converse_text(
            system_prompt=(
                "You decide how to apply user-supplied credentials when probing an unknown API. "
                "Return only JSON with keys headers and query_params. "
                "Do not invent secrets; only reference the provided token with placeholders."
            ),
            user_prompt=user_prompt,
            max_tokens=500,
        )
        payload = require_object(parse_model_json(raw), context="Auth attempt response")
        headers_payload = coerce_string_map(payload.get("headers", {}), limit=24)
        query_payload = coerce_string_map(payload.get("query_params", {}), limit=24)
        headers = {
            str(key): _replace_placeholders(str(value), auth_input)
            for key, value in headers_payload.items()
        }
        query_params = {
            str(key): _replace_placeholders(str(value), auth_input)
            for key, value in query_payload.items()
        }
        return headers, query_params

    def classify(self, signals: list[AuthSignal], context: str) -> AuthResult:
        if not self.runtime.text_model_enabled:
            raise BedrockUnavailableError("LUCA auth classification requires Amazon Nova.")

        raw = self.runtime.converse_text(
            system_prompt=(
                "You classify API authentication from evidence gathered during reverse engineering. "
                "Return only JSON with auth_type, confidence, rationale, required_headers, required_query_params. "
                "Do not invent requirements not supported by the evidence."
            ),
            user_prompt=json.dumps(
                {
                    "signals": [signal.model_dump(mode="json") for signal in signals],
                    "context": context,
                },
                indent=2,
            ),
            max_tokens=700,
        )
        payload = require_object(parse_model_json(raw), context="Auth classification response")
        return AuthResult.model_validate(payload)

    def _signals_from_evidence(self, evidence_type: str, source: str, evidence: dict[str, Any]) -> list[AuthSignal]:
        if not self.runtime.text_model_enabled:
            raise BedrockUnavailableError("LUCA auth signal extraction requires Amazon Nova.")

        raw = self.runtime.converse_text(
            system_prompt=(
                "You extract authentication clues from API reverse-engineering evidence. "
                "Return only JSON with a top-level signals array. "
                "Each item must have signal_type, confidence, source, details. "
                "Use only evidence that is actually present."
            ),
            user_prompt=json.dumps(
                {
                    "evidence_type": evidence_type,
                    "source": source,
                    "evidence": evidence,
                },
                indent=2,
            ),
            max_tokens=700,
        )
        payload = parse_model_json(raw)
        signal_items = payload.get("signals", []) if isinstance(payload, dict) else payload
        return [item for item in validate_model_list(signal_items, AuthSignal, limit=16)]
