from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests

from .auth import HybridAuthClassifier
from .bedrock import BedrockRuntime, BedrockUnavailableError, cosine_similarity
from .config import Settings
from .model_io import coerce_string_map
from .models import AuthInput, AuthSignal, DocumentChunk, EndpointRecord, SourceDocument


@dataclass
class DiscoveryToolState:
    api_url: str
    auth_input: AuthInput | None
    sources: list[SourceDocument]
    chunks: list[DocumentChunk]
    chunk_vectors: list[list[float]]
    candidate_paths: list[str]
    resource_hints: list[str]
    discovered_endpoints: list[EndpointRecord]
    auth_signals: list[AuthSignal]
    attempted_paths: set[str] = field(default_factory=set)
    inspected_chunks: set[str] = field(default_factory=set)
    planner_trace: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    low_yield_turns: int = 0


class DiscoveryPlanner:
    ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

    def __init__(self, settings: Settings, runtime: BedrockRuntime, auth_classifier: HybridAuthClassifier):
        self.settings = settings
        self.runtime = runtime
        self.auth_classifier = auth_classifier
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "LUCA/1.0 planner"})

    def run(self, state: DiscoveryToolState, probe_budget: int) -> DiscoveryToolState:
        if not self.runtime.text_model_enabled:
            raise BedrockUnavailableError("LUCA discovery requires Amazon Nova planning.")
        planned = self._run_with_nova(state, probe_budget)
        state.planner_trace.insert(0, {"action": "planner_mode", "result": {"mode": "nova"}})
        return planned

    def _run_with_nova(self, state: DiscoveryToolState, probe_budget: int) -> DiscoveryToolState:
        messages = [
            {
                "role": "user",
                "content": [{"text": self._state_prompt(state, probe_budget)}],
            }
        ]
        tool_config = {
            "tools": [
                self._tool_spec("inspect_source_chunk", "Inspect the next most relevant source chunk.", {"type": "object", "properties": {"query": {"type": "string"}}}),
                self._tool_spec(
                    "probe_endpoint",
                    "Probe an API endpoint and optionally supply query params, headers, or a request body.",
                    {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "method": {"type": "string"},
                            "headers": {"type": "object"},
                            "query_params": {"type": "object"},
                            "json_body": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                            "form_body": {"type": "object"},
                        },
                        "required": ["path"],
                    },
                ),
                self._tool_spec(
                    "test_auth_variant",
                    "Retry an endpoint using a specific auth style and optional request details.",
                    {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "variant": {"type": "string"},
                            "method": {"type": "string"},
                            "headers": {"type": "object"},
                            "query_params": {"type": "object"},
                            "json_body": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                            "form_body": {"type": "object"},
                        },
                        "required": ["path", "variant"],
                    },
                ),
                self._tool_spec("stop_discovery", "Stop discovery when enough evidence exists.", {"type": "object", "properties": {"reason": {"type": "string"}}}),
            ],
            "toolChoice": {"auto": {}},
        }

        for _ in range(min(probe_budget, self.settings.max_planner_turns)):
            response = self.runtime.converse(
                system_prompt=(
                    "You are LUCA, an API reverse-engineering planner for agents. Choose one or more tools to maximize real endpoint coverage, "
                    "understand auth and service behavior, and stop when additional probes are low value. Never invent results."
                ),
                messages=messages,
                tool_config=tool_config,
                temperature=0.0,
                max_tokens=700,
            )
            assistant_message = response.get("output", {}).get("message", {})
            content = assistant_message.get("content", [])
            tool_uses = [block["toolUse"] for block in content if "toolUse" in block]
            text_blocks = [block["text"] for block in content if "text" in block]
            messages.append({"role": "assistant", "content": content})

            if not tool_uses:
                state.stop_reason = "\n".join(text_blocks).strip() or "Planner stopped without issuing more tool actions."
                state.planner_trace.append({"action": "stop", "reason": state.stop_reason})
                return state

            tool_results = []
            for tool_use in tool_uses:
                result = self._execute_tool(state, tool_use.get("name", ""), tool_use.get("input", {}))
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": tool_use["toolUseId"],
                            "content": [{"json": result}],
                            "status": "success",
                        }
                    }
                )
                if result.get("stop"):
                    state.stop_reason = result.get("reason", "Planner requested stop.")
            messages.append({"role": "user", "content": tool_results})
            if state.stop_reason:
                return state

        state.stop_reason = state.stop_reason or "Planner turn budget reached."
        return state

    def _execute_tool(self, state: DiscoveryToolState, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if name == "inspect_source_chunk":
            result = self._inspect_source_chunk(state, tool_input.get("query"))
        elif name == "probe_endpoint":
            result = self._probe_endpoint(
                state,
                tool_input.get("path", "/"),
                tool_input.get("method", "GET"),
                headers=coerce_string_map(tool_input.get("headers", {}), limit=16),
                query_params=coerce_string_map(tool_input.get("query_params", {}), limit=16),
                json_body=tool_input.get("json_body"),
                form_body=coerce_string_map(tool_input.get("form_body", {}), limit=32),
            )
        elif name == "test_auth_variant":
            result = self._test_auth_variant(
                state,
                tool_input.get("path", "/"),
                tool_input.get("variant", "bearer"),
                method=tool_input.get("method", "GET"),
                headers=coerce_string_map(tool_input.get("headers", {}), limit=16),
                query_params=coerce_string_map(tool_input.get("query_params", {}), limit=16),
                json_body=tool_input.get("json_body"),
                form_body=coerce_string_map(tool_input.get("form_body", {}), limit=32),
            )
        elif name == "stop_discovery":
            result = {"stop": True, "reason": tool_input.get("reason", "Planner requested stop.")}
        else:
            result = {"stop": False, "error": f"Unknown tool: {name}"}

        trace_entry = {"action": name, "input": tool_input, "result": result}
        state.planner_trace.append(trace_entry)
        return result

    def _inspect_source_chunk(self, state: DiscoveryToolState, query: str | None) -> dict[str, Any]:
        candidate_chunks = [chunk for chunk in state.chunks if chunk.chunk_id not in state.inspected_chunks]
        if not candidate_chunks:
            return {"stop": False, "message": "No uninspected chunks remain."}

        chunk = candidate_chunks[0]
        if query:
            try:
                query_vector = self.runtime.embed_texts([query])[0]
                ranked = sorted(
                    candidate_chunks,
                    key=lambda item: cosine_similarity(
                        query_vector,
                        state.chunk_vectors[state.chunks.index(item)] if state.chunk_vectors else [],
                    ),
                    reverse=True,
                )
                if ranked:
                    chunk = ranked[0]
            except BedrockUnavailableError:
                pass

        state.inspected_chunks.add(chunk.chunk_id)
        state.low_yield_turns = 0
        return {
            "stop": False,
            "chunk_id": chunk.chunk_id,
            "excerpt": chunk.text[:320],
            "source_id": chunk.source_id,
        }

    def _probe_endpoint(
        self,
        state: DiscoveryToolState,
        path: str,
        method: str,
        *,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        json_body: Any = None,
        form_body: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        normalized_method = self._normalize_method(method)
        absolute_url = urljoin(state.api_url.rstrip("/") + "/", path.lstrip("/"))
        state.attempted_paths.add(f"{normalized_method} {path}")
        try:
            response = self.http.request(
                normalized_method,
                absolute_url,
                headers=headers,
                params=query_params,
                json=json_body if form_body is None else None,
                data=form_body or None,
                timeout=self.settings.request_timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            state.low_yield_turns += 1
            return {"stop": False, "path": path, "method": normalized_method, "error": str(exc)}

        content_type = response.headers.get("content-type", "")
        sample_fields: list[str] = []
        payload: Any = None
        if "json" in content_type:
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    sample_fields = list(payload.keys())[:8]
                elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
                    sample_fields = list(payload[0].keys())[:8]
            except ValueError:
                sample_fields = []

        if response.status_code in {200, 201, 202, 204, 400}:
            endpoint = EndpointRecord(
                method=normalized_method,
                path=path,
                status_code=response.status_code,
                source="probe",
                requires_auth=False,
                sample_fields=sample_fields,
            )
            if (endpoint.method, endpoint.path) not in {(item.method, item.path) for item in state.discovered_endpoints}:
                state.discovered_endpoints.append(endpoint)
            state.low_yield_turns = 0
        else:
            state.low_yield_turns += 1

        header_signals = self.auth_classifier.signals_from_headers(dict(response.headers), response.status_code, absolute_url)
        state.auth_signals.extend(header_signals)
        if response.text:
            state.auth_signals.extend(self.auth_classifier.signals_from_text(response.text[:500], absolute_url))

        if response.status_code in {401, 403}:
            return {
                "stop": False,
                "path": path,
                "method": normalized_method,
                "status_code": response.status_code,
                "auth_hint": [signal.signal_type for signal in header_signals] or ["unknown"],
            }

        return {
            "stop": False,
            "path": path,
            "method": normalized_method,
            "status_code": response.status_code,
            "sample_fields": sample_fields,
            "response_excerpt": self._response_excerpt(payload, response.text),
            "content_type": content_type,
        }

    def _test_auth_variant(
        self,
        state: DiscoveryToolState,
        path: str,
        variant: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        json_body: Any = None,
        form_body: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        normalized_method = self._normalize_method(method)
        absolute_url = urljoin(state.api_url.rstrip("/") + "/", path.lstrip("/"))
        auth_headers, auth_query_params = self.auth_classifier.build_auth_attempt(
            state.auth_input,
            variant=variant,
            path=path,
            context=json.dumps(
                {
                    "auth_signals": [signal.model_dump(mode="json") for signal in state.auth_signals[-12:]],
                    "discovered_endpoints": [endpoint.model_dump(mode="json") for endpoint in state.discovered_endpoints[-6:]],
                },
                indent=2,
            ),
        )
        merged_headers = {**auth_headers, **(headers or {})}
        merged_query = {**auth_query_params, **(query_params or {})}
        try:
            response = self.http.request(
                normalized_method,
                absolute_url,
                headers=merged_headers,
                params=merged_query,
                json=json_body if form_body is None else None,
                data=form_body or None,
                timeout=self.settings.request_timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            state.low_yield_turns += 1
            return {"stop": False, "path": path, "method": normalized_method, "variant": variant, "error": str(exc)}

        signals = self.auth_classifier.signals_from_headers(dict(response.headers), response.status_code, f"{absolute_url}#{variant}")
        state.auth_signals.extend(signals)
        if response.text:
            state.auth_signals.extend(self.auth_classifier.signals_from_text(response.text[:500], f"{absolute_url}#{variant}"))

        content_type = response.headers.get("content-type", "")
        sample_fields: list[str] = []
        payload: Any = None
        if "json" in content_type:
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    sample_fields = list(payload.keys())[:8]
                elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
                    sample_fields = list(payload[0].keys())[:8]
            except ValueError:
                sample_fields = []

        if response.status_code in {200, 201, 202, 204, 400}:
            endpoint = EndpointRecord(
                method=normalized_method,
                path=path,
                status_code=response.status_code,
                source=f"probe:{variant}",
                requires_auth=True,
                sample_fields=sample_fields,
            )
            if (endpoint.method, endpoint.path) not in {(item.method, item.path) for item in state.discovered_endpoints}:
                state.discovered_endpoints.append(endpoint)
            state.low_yield_turns = 0
        else:
            state.low_yield_turns += 1

        return {
            "stop": False,
            "path": path,
            "method": normalized_method,
            "variant": variant,
            "status_code": response.status_code,
            "response_excerpt": self._response_excerpt(payload, response.text),
            "content_type": content_type,
        }

    def _state_prompt(self, state: DiscoveryToolState, probe_budget: int) -> str:
        return json.dumps(
            {
                "api_url": state.api_url,
                "probe_budget": probe_budget,
                "sources": [source.model_dump(mode="json") for source in state.sources],
                "candidate_paths": state.candidate_paths[:24],
                "resource_hints": state.resource_hints[:24],
                "discovered_endpoints": [endpoint.model_dump(mode="json") for endpoint in state.discovered_endpoints[:20]],
                "auth_signals": [signal.model_dump(mode="json") for signal in state.auth_signals[:20]],
                "auth_input_available": bool(state.auth_input and state.auth_input.token),
                "auth_input_shape": {
                    "header_name": state.auth_input.header_name if state.auth_input else None,
                    "header_prefix": state.auth_input.header_prefix if state.auth_input else None,
                    "query_param": state.auth_input.query_param if state.auth_input else None,
                    "extra_headers": list((state.auth_input.extra_headers or {}).keys()) if state.auth_input else [],
                },
                "remaining_uninspected_chunks": len([chunk for chunk in state.chunks if chunk.chunk_id not in state.inspected_chunks]),
                "attempted_paths": list(state.attempted_paths)[:20],
            },
            indent=2,
        )

    @staticmethod
    def _tool_spec(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "toolSpec": {
                "name": name,
                "description": description,
                "inputSchema": {"json": schema},
            }
        }

    @staticmethod
    def _response_excerpt(payload: Any, raw_text: str) -> str:
        if payload is not None:
            try:
                return json.dumps(payload)[:600]
            except TypeError:
                pass
        return raw_text[:600]

    @classmethod
    def _normalize_method(cls, method: str) -> str:
        normalized = (method or "GET").upper()
        return normalized if normalized in cls.ALLOWED_METHODS else "GET"
