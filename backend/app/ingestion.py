from __future__ import annotations

import json
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import requests
import yaml
from bs4 import BeautifulSoup

from .auth import HybridAuthClassifier
from .bedrock import BedrockRuntime, BedrockUnavailableError
from .config import Settings
from .model_io import coerce_string_list, parse_model_json, validate_model_list
from .models import AuthInput, AuthSignal, DocumentChunk, EndpointParameter, EndpointRecord, SourceDocument


@dataclass
class IngestionResult:
    sources: list[SourceDocument]
    chunks: list[DocumentChunk]
    endpoints: list[EndpointRecord]
    auth_signals: list[AuthSignal]
    candidate_paths: list[str]
    resource_hints: list[str]
    raw_spec: dict[str, Any] | None = None


class IngestionService:
    def __init__(self, settings: Settings, runtime: BedrockRuntime, auth_classifier: HybridAuthClassifier):
        self.settings = settings
        self.runtime = runtime
        self.auth_classifier = auth_classifier
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "LUCA/1.0 (+https://amazon-nova.devpost.com)"})

    def ingest(self, api_url: str, docs_url: str | None = None, auth_input: AuthInput | None = None) -> IngestionResult:
        sources: list[SourceDocument] = []
        chunks: list[DocumentChunk] = []
        endpoints: list[EndpointRecord] = []
        auth_signals: list[AuthSignal] = []
        candidate_paths: list[str] = []
        resource_hints: list[str] = []
        raw_spec: dict[str, Any] | None = None

        visited_urls: set[str] = set()
        queued_urls: list[tuple[str, str]] = [(api_url, "seed")]
        if docs_url and docs_url != api_url:
            queued_urls.append((docs_url, "seed"))

        while queued_urls and len(sources) < 8:
            url, source_type = queued_urls.pop(0)
            if url in visited_urls:
                continue
            fetched = self._fetch_source(url, source_type, auth_input)
            if fetched:
                source, body = fetched
                visited_urls.add(url)
                parsed_spec = self._parse_openapi(body)
                if parsed_spec and ("openapi" in parsed_spec or "swagger" in parsed_spec):
                    source.source_type = "openapi"
                    raw_spec = parsed_spec
                sources.append(source)
                chunks.extend(self._chunk_source(source, body))
                source_analysis = self._analyze_source_with_model(source, body)
                auth_signals.extend(source_analysis["auth_signals"])
                candidate_paths.extend(source_analysis["candidate_paths"])
                resource_hints.extend(source_analysis["resource_hints"])
                queued_urls.extend(self._follow_up_fetch_queue(api_url, source_analysis["follow_up_urls"], visited_urls))

                if parsed_spec and ("openapi" in parsed_spec or "swagger" in parsed_spec):
                    endpoints.extend(self._normalize_openapi(parsed_spec))
                    candidate_paths.extend([endpoint.path for endpoint in endpoints])

        candidate_paths.extend(self._seed_candidate_paths(api_url, sources, candidate_paths))
        candidate_paths = self._dedupe_paths(candidate_paths)
        resource_hints.extend(self._analyze_repo_level_hints(api_url, sources, chunks, candidate_paths))
        resource_hints = self._dedupe_strings(resource_hints)
        return IngestionResult(
            sources=sources,
            chunks=chunks,
            endpoints=self._dedupe_endpoints(endpoints),
            auth_signals=auth_signals,
            candidate_paths=candidate_paths,
            resource_hints=resource_hints,
            raw_spec=raw_spec,
        )

    def _fetch_source(
        self,
        url: str,
        source_type: str,
        auth_input: AuthInput | None,
    ) -> tuple[SourceDocument, str] | None:
        headers: dict[str, str] = {}
        query_params: dict[str, str] = {}
        if auth_input:
            headers, query_params = self.auth_classifier.build_auth_attempt(
                auth_input,
                variant="initial_source_fetch",
                path=urlparse(url).path or "/",
                context="Build the most sensible request for initial source or docs retrieval.",
            )
        try:
            response = self.session.get(
                url,
                headers=headers,
                params=query_params,
                timeout=self.settings.request_timeout_seconds,
                allow_redirects=True,
            )
        except (requests.RequestException, BedrockUnavailableError):
            return None

        content_type = response.headers.get("content-type", "application/octet-stream").split(";")[0]
        text = response.text if response.text else response.content.decode("utf-8", errors="ignore")
        summary = self._summarize_text(text)
        title = None
        if content_type.startswith("text/html"):
            soup = BeautifulSoup(text, "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()

        source = SourceDocument(
            source_id=str(uuid4()),
            url=response.url,
            source_type=source_type,
            content_type=content_type,
            status_code=response.status_code,
            title=title,
            summary=summary,
        )
        return source, text

    @staticmethod
    def _parse_openapi(body: str) -> dict[str, Any] | None:
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            try:
                parsed = yaml.safe_load(body)
                return parsed if isinstance(parsed, dict) else None
            except yaml.YAMLError:
                return None

    def _normalize_openapi(self, spec: dict[str, Any]) -> list[EndpointRecord]:
        endpoints: list[EndpointRecord] = []
        for path, methods in spec.get("paths", {}).items():
            if not isinstance(methods, dict):
                continue
            for method, details in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                parameters = []
                for parameter in details.get("parameters", []):
                    schema = parameter.get("schema", {}) if isinstance(parameter, dict) else {}
                    parameters.append(
                        EndpointParameter(
                            name=parameter.get("name", "param"),
                            location=parameter.get("in", "query"),
                            required=bool(parameter.get("required", False)),
                            description=parameter.get("description", ""),
                            schema_type=schema.get("type", "string"),
                        )
                    )
                endpoints.append(
                    EndpointRecord(
                        method=method.upper(),
                        path=path,
                        summary=details.get("summary", ""),
                        description=details.get("description", ""),
                        parameters=parameters,
                        source="openapi",
                    )
                )
        return endpoints

    def _seed_candidate_paths(
        self,
        api_url: str,
        sources: list[SourceDocument],
        existing_paths: list[str],
    ) -> list[str]:
        seeded: list[str] = ["/"]
        seeded.extend(existing_paths)

        for source in sources:
            source_path = urlparse(source.url).path
            if source_path and source_path not in seeded:
                seeded.append(source_path)
        parsed = urlparse(api_url)
        if parsed.path and parsed.path not in seeded:
            seeded.append(parsed.path)
        return seeded

    def _chunk_source(self, source: SourceDocument, body: str) -> list[DocumentChunk]:
        text = unescape(body)
        if not text.strip():
            return []
        chunk_size = self.settings.chunk_size
        overlap = self.settings.chunk_overlap
        chunks: list[DocumentChunk] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_size)
            snippet = text[start:end].strip()
            if snippet:
                chunks.append(
                    DocumentChunk(
                        chunk_id=str(uuid4()),
                        source_id=source.source_id,
                        text=snippet,
                        keywords=[],
                    )
                )
            if end == len(text):
                break
            start = max(end - overlap, start + 1)
        return chunks[:24]

    def _analyze_source_with_model(self, source: SourceDocument, body: str) -> dict[str, Any]:
        if not self.runtime.text_model_enabled:
            raise BedrockUnavailableError("LUCA source analysis requires Amazon Nova.")

        raw = self.runtime.converse_text(
            system_prompt=(
                "You extract reverse-engineering hints from source material for LUCA. "
                "Return only JSON with candidate_paths, resource_hints, follow_up_urls, and auth_signals. "
                "Use only evidence from the source. Candidate paths should be concrete or strongly evidenced. "
                "follow_up_urls should be source URLs worth fetching next."
            ),
            user_prompt=json.dumps(
                {
                    "source": source.model_dump(mode="json"),
                    "body_excerpt": body[:5000],
                },
                indent=2,
            ),
            max_tokens=900,
        )
        payload = parse_model_json(raw)
        return {
            "candidate_paths": coerce_string_list(payload.get("candidate_paths", []), limit=24) if isinstance(payload, dict) else [],
            "resource_hints": coerce_string_list(payload.get("resource_hints", []), limit=24, lower=True) if isinstance(payload, dict) else [],
            "follow_up_urls": coerce_string_list(payload.get("follow_up_urls", []), limit=8) if isinstance(payload, dict) else [],
            "auth_signals": [item for item in validate_model_list(payload.get("auth_signals", []), AuthSignal, limit=12)] if isinstance(payload, dict) else [],
        }

    def _analyze_repo_level_hints(
        self,
        api_url: str,
        sources: list[SourceDocument],
        chunks: list[DocumentChunk],
        candidate_paths: list[str],
    ) -> list[str]:
        if not self.runtime.text_model_enabled:
            raise BedrockUnavailableError("LUCA hint extraction requires Amazon Nova.")

        raw = self.runtime.converse_text(
            system_prompt=(
                "You identify likely API resource names from partial reverse-engineering evidence. "
                "Return only JSON with resource_hints as a list of short resource labels."
            ),
            user_prompt=json.dumps(
                {
                    "api_url": api_url,
                    "source_urls": [source.url for source in sources[:12]],
                    "candidate_paths": candidate_paths[:24],
                    "chunk_keywords": [chunk.keywords for chunk in chunks[:12]],
                },
                indent=2,
            ),
            max_tokens=500,
        )
        payload = parse_model_json(raw)
        if not isinstance(payload, dict):
            return []
        return coerce_string_list(payload.get("resource_hints", []), limit=24, lower=True)

    @staticmethod
    def _summarize_text(text: str) -> str:
        normalized = " ".join(text.split())
        return normalized[:240]

    @staticmethod
    def _strip_code_fences(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                return "\n".join(lines[1:-1]).strip()
        return stripped

    def _follow_up_fetch_queue(
        self,
        api_url: str,
        follow_up_urls: list[str],
        visited_urls: set[str],
    ) -> list[tuple[str, str]]:
        base_host = urlparse(api_url).hostname or ""
        queued: list[tuple[str, str]] = []
        for candidate in follow_up_urls[:8]:
            absolute = urljoin(api_url.rstrip("/") + "/", str(candidate).strip())
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"}:
                continue
            candidate_host = parsed.hostname or ""
            if candidate_host and not self._hosts_compatible(base_host, candidate_host):
                continue
            if absolute in visited_urls:
                continue
            queued.append((absolute, "discovered"))
        return queued

    @staticmethod
    def _hosts_compatible(base_host: str, candidate_host: str) -> bool:
        if not base_host or not candidate_host:
            return False
        if base_host == candidate_host:
            return True
        if candidate_host.endswith(f".{base_host}") or base_host.endswith(f".{candidate_host}"):
            return True

        base_parts = base_host.split(".")
        candidate_parts = candidate_host.split(".")
        if len(base_parts) < 2 or len(candidate_parts) < 2:
            return False
        return ".".join(base_parts[-2:]) == ".".join(candidate_parts[-2:])

    @staticmethod
    def _dedupe_paths(paths: list[str]) -> list[str]:
        cleaned: list[str] = []
        for path in paths:
            parsed = urlparse(path)
            value = path
            if parsed.scheme and parsed.netloc:
                value = parsed.path or "/"
            value = value.strip()
            if not value.startswith("/"):
                continue
            if value not in cleaned:
                cleaned.append(value)
        return cleaned[:64]

    @staticmethod
    def _dedupe_strings(values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            normalized = value.strip().lower()
            if not normalized:
                continue
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned[:32]

    @staticmethod
    def _dedupe_endpoints(endpoints: list[EndpointRecord]) -> list[EndpointRecord]:
        seen: set[tuple[str, str]] = set()
        unique: list[EndpointRecord] = []
        for endpoint in endpoints:
            key = (endpoint.method.upper(), endpoint.path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(endpoint)
        return unique
