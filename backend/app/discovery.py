from __future__ import annotations

from typing import Any

from .artifacts import ArtifactStore
from .auth import HybridAuthClassifier
from .bedrock import BedrockRuntime, BedrockUnavailableError
from .config import Settings
from .ingestion import IngestionService
from .models import SessionEvent, SessionPhase, SessionRecord, SessionStatus
from .planner import DiscoveryPlanner, DiscoveryToolState
from .storage import SessionRepository


class DiscoveryService:
    def __init__(
        self,
        settings: Settings,
        repository: SessionRepository,
        artifact_store: ArtifactStore,
        ingestion: IngestionService,
        planner: DiscoveryPlanner,
        auth_classifier: HybridAuthClassifier,
        runtime: BedrockRuntime,
    ):
        self.settings = settings
        self.repository = repository
        self.artifact_store = artifact_store
        self.ingestion = ingestion
        self.planner = planner
        self.auth_classifier = auth_classifier
        self.runtime = runtime

    def discover_session(self, session_id: str, probe_budget: int | None = None) -> SessionRecord:
        session = self._require_session(session_id)
        session.status = SessionStatus.RUNNING
        session.phase = SessionPhase.DISCOVERY
        session.probe_budget = probe_budget or session.probe_budget or self.settings.default_probe_budget
        self.repository.save_session(session)
        self._event(session.session_id, SessionPhase.DISCOVERY, "discovery.started", "Discovery started.")

        try:
            ingestion_result = self.ingestion.ingest(session.api_url, session.docs_url, session.auth_input)
            self._event(
                session.session_id,
                SessionPhase.DISCOVERY,
                "ingestion.completed",
                f"Ingested {len(ingestion_result.sources)} sources and {len(ingestion_result.chunks)} chunks.",
            )

            chunk_vectors = []
            if ingestion_result.chunks:
                try:
                    chunk_vectors = self.runtime.embed_texts([chunk.text for chunk in ingestion_result.chunks])
                    self._event(
                        session.session_id,
                        SessionPhase.DISCOVERY,
                        "embeddings.completed",
                        f"Embedded {len(chunk_vectors)} chunks for retrieval.",
                    )
                except BedrockUnavailableError as exc:
                    session.metadata["embedding_warning"] = str(exc)
                    self._event(
                        session.session_id,
                        SessionPhase.DISCOVERY,
                        "embeddings.skipped",
                        str(exc),
                    )

            tool_state = DiscoveryToolState(
                api_url=session.api_url,
                auth_input=session.auth_input,
                sources=ingestion_result.sources,
                chunks=ingestion_result.chunks,
                chunk_vectors=chunk_vectors,
                candidate_paths=ingestion_result.candidate_paths,
                resource_hints=ingestion_result.resource_hints,
                discovered_endpoints=ingestion_result.endpoints,
                auth_signals=ingestion_result.auth_signals,
            )
            planner_state = self.planner.run(tool_state, session.probe_budget)
            auth_context = self._build_auth_context(planner_state)
            auth_result = self.auth_classifier.classify(planner_state.auth_signals, auth_context)

            sources_artifact = self.artifact_store.put_json(
                session.session_id,
                "sources.json",
                [source.model_dump(mode="json") for source in ingestion_result.sources],
            )
            chunks_artifact = self.artifact_store.put_json(
                session.session_id,
                "chunks.json",
                [chunk.model_dump(mode="json") for chunk in ingestion_result.chunks],
            )
            vectors_artifact = None
            if chunk_vectors:
                vectors_artifact = self.artifact_store.put_json(
                    session.session_id,
                    "chunk_embeddings.json",
                    chunk_vectors,
                )
            graph_artifact = self.artifact_store.put_json(
                session.session_id,
                "endpoint_graph.json",
                [endpoint.model_dump(mode="json") for endpoint in planner_state.discovered_endpoints],
            )
            report_payload = {
                "api_url": session.api_url,
                "docs_url": session.docs_url,
                "probe_budget": session.probe_budget,
                "auth_result": auth_result.model_dump(mode="json"),
                "source_count": len(ingestion_result.sources),
                "endpoint_count": len(planner_state.discovered_endpoints),
                "candidate_paths": planner_state.candidate_paths,
                "resource_hints": planner_state.resource_hints,
                "planner_trace": planner_state.planner_trace,
                "stop_reason": planner_state.stop_reason,
            }
            report_artifact = self.artifact_store.put_json(session.session_id, "discovery_report.json", report_payload)

            artifacts = [sources_artifact, chunks_artifact, graph_artifact, report_artifact]
            if vectors_artifact:
                artifacts.append(vectors_artifact)

            session.status = SessionStatus.COMPLETED
            session.auth_result = auth_result
            session.endpoint_count = len(planner_state.discovered_endpoints)
            session.endpoints_preview = planner_state.discovered_endpoints[: self.settings.preview_limit]
            session.artifacts = self._merge_artifacts(session.artifacts, artifacts)
            session.metadata.update(
                {
                    "planner_mode": (
                        planner_state.planner_trace[0].get("result", {}).get("mode")
                        if planner_state.planner_trace and planner_state.planner_trace[0].get("action") == "planner_mode"
                        else "unknown"
                    ),
                    "planner_stop_reason": planner_state.stop_reason,
                    "planner_trace_count": len(planner_state.planner_trace),
                    "candidate_path_count": len(planner_state.candidate_paths),
                    "discovery_artifacts": [artifact.name for artifact in artifacts],
                }
            )
            self.repository.save_session(session)
            self._event(
                session.session_id,
                SessionPhase.DISCOVERY,
                "discovery.completed",
                f"Discovery finished with {session.endpoint_count} endpoints.",
                {"auth_type": auth_result.auth_type, "confidence": auth_result.confidence},
            )
            return session
        except Exception as exc:
            session.status = SessionStatus.FAILED
            session.errors.append(str(exc))
            self.repository.save_session(session)
            self._event(session.session_id, SessionPhase.DISCOVERY, "discovery.failed", str(exc))
            raise

    def _require_session(self, session_id: str) -> SessionRecord:
        session = self.repository.get_session(session_id)
        if not session:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    def _event(
        self,
        session_id: str,
        phase: SessionPhase,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.repository.append_event(
            session_id,
            SessionEvent(phase=phase, event_type=event_type, message=message, payload=payload or {}),
        )

    @staticmethod
    def _merge_artifacts(existing: list, new: list):
        by_name = {artifact.name: artifact for artifact in existing}
        for artifact in new:
            by_name[artifact.name] = artifact
        return list(by_name.values())

    @staticmethod
    def _build_auth_context(state: DiscoveryToolState) -> str:
        excerpts = [chunk.text[:200] for chunk in state.chunks[:4]]
        return "\n\n".join(excerpts + [f"Stop reason: {state.stop_reason or 'n/a'}"])
