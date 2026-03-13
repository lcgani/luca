from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any

from .artifacts import ArtifactStore
from .bedrock import BedrockRuntime, BedrockUnavailableError
from .config import Settings
from .models import EndpointRecord, SessionArtifact, SessionEvent, SessionPhase, SessionRecord, SessionStatus
from .storage import SessionRepository


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]


class GenerationService:
    def __init__(
        self,
        settings: Settings,
        repository: SessionRepository,
        artifact_store: ArtifactStore,
        runtime: BedrockRuntime,
    ):
        self.settings = settings
        self.repository = repository
        self.artifact_store = artifact_store
        self.runtime = runtime

    def generate_session(self, session_id: str) -> SessionRecord:
        session = self._require_session(session_id)
        session.status = SessionStatus.RUNNING
        session.phase = SessionPhase.GENERATION
        self.repository.save_session(session)
        self._event(session.session_id, SessionPhase.GENERATION, "generation.started", "Generation started.")

        try:
            endpoints = self._load_endpoints(session.session_id)
            discovery_report = self._load_json_artifact(session.session_id, "discovery_report.json")
            bundle = self._generate_bundle(session, endpoints, discovery_report)
            validation = self._validate_bundle(bundle)

            if not validation.ok:
                self._event(
                    session.session_id,
                    SessionPhase.GENERATION,
                    "generation.validation_failed",
                    "Initial generation failed validation. Running one repair pass.",
                    {"errors": validation.errors},
                )
                bundle = self._repair_bundle(session, endpoints, discovery_report, bundle, validation.errors)
                validation = self._validate_bundle(bundle)

            if not validation.ok:
                session.status = SessionStatus.FAILED
                session.errors.extend(validation.errors)
                session.metadata["generation_validation"] = {"ok": False, "errors": validation.errors}
                self.repository.save_session(session)
                self._event(
                    session.session_id,
                    SessionPhase.GENERATION,
                    "generation.failed",
                    "Generation failed validation after repair.",
                    {"errors": validation.errors},
                )
                return session

            artifacts = self._persist_bundle(session.session_id, bundle)
            session.status = SessionStatus.COMPLETED
            session.phase = SessionPhase.COMPLETE
            session.artifacts = self._merge_artifacts(session.artifacts, artifacts)
            session.metadata["generation_validation"] = {"ok": True, "errors": []}
            session.metadata["generated_artifacts"] = [artifact.name for artifact in artifacts]
            self.repository.save_session(session)
            self._event(
                session.session_id,
                SessionPhase.GENERATION,
                "generation.completed",
                f"Generated {len(artifacts)} artifact(s).",
            )
            return session
        except Exception as exc:
            session.status = SessionStatus.FAILED
            session.errors.append(str(exc))
            self.repository.save_session(session)
            self._event(session.session_id, SessionPhase.GENERATION, "generation.failed", str(exc))
            raise

    def _generate_bundle(
        self,
        session: SessionRecord,
        endpoints: list[EndpointRecord],
        discovery_report: dict[str, Any],
    ) -> dict[str, str]:
        if not self.runtime.text_model_enabled:
            raise BedrockUnavailableError("LUCA generation requires Amazon Nova.")
        return self._generate_bundle_via_nova(session, endpoints, discovery_report)

    def _generate_bundle_via_nova(
        self,
        session: SessionRecord,
        endpoints: list[EndpointRecord],
        discovery_report: dict[str, Any],
    ) -> dict[str, str]:
        context = json.dumps(
            {
                "api_url": session.api_url,
                "auth_result": session.auth_result.model_dump(mode="json"),
                "endpoints": [endpoint.model_dump(mode="json") for endpoint in endpoints],
                "discovery_report": discovery_report,
            },
            indent=2,
        )
        prompts = {
            "client.py": (
                "Return only Python code. Build a production-friendly requests-based client class with one method per endpoint. "
                "Include auth handling based on auth_result, a shared _request helper, and docstrings."
            ),
            "usage_examples.py": (
                "Return only Python code. Show how to instantiate the client and call several representative endpoints."
            ),
            "README.md": (
                "Return only markdown. Describe setup, auth, main methods, and usage for the generated client."
            ),
            "smoke_test.py": (
                "Return only Python code. Define run_smoke_test() and patch network calls with unittest.mock so the generated client can be smoke-tested offline."
            ),
            "mcp_server.py": (
                "Return only Python code. Build a runnable FastAPI JSON-RPC server that wraps the generated client and exposes a list_tools method and invoke method."
            ),
        }
        bundle: dict[str, str] = {}
        system_prompt = (
            "You are generating artifacts for LUCA, an API reverse-engineering layer for agents. "
            "Use the supplied discovery context exactly. Do not fabricate endpoints or workflows."
        )
        for name, task_prompt in prompts.items():
            content = self.runtime.converse_text(
                system_prompt=system_prompt,
                user_prompt=f"{task_prompt}\n\nContext:\n{context}",
                max_tokens=1800,
            )
            bundle[name] = self._strip_code_fences(content)
        bundle["discovery_report.json"] = json.dumps(discovery_report, indent=2)
        return bundle

    def _repair_bundle(
        self,
        session: SessionRecord,
        endpoints: list[EndpointRecord],
        discovery_report: dict[str, Any],
        bundle: dict[str, str],
        errors: list[str],
    ) -> dict[str, str]:
        if not self.runtime.text_model_enabled:
            return bundle

        repaired = dict(bundle)
        system_prompt = "You repair Python files. Return only corrected file contents."
        for name in ("client.py", "usage_examples.py", "smoke_test.py", "mcp_server.py"):
            if name not in repaired:
                continue
            user_prompt = json.dumps(
                {
                    "file_name": name,
                    "current_contents": repaired[name],
                    "errors": errors,
                    "context": {
                        "api_url": session.api_url,
                        "auth_result": session.auth_result.model_dump(mode="json"),
                        "endpoints": [endpoint.model_dump(mode="json") for endpoint in endpoints],
                        "discovery_report": discovery_report,
                    },
                },
                indent=2,
            )
            try:
                repaired[name] = self._strip_code_fences(
                    self.runtime.converse_text(system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=1800)
                )
            except BedrockUnavailableError:
                return bundle
        return repaired

    def _persist_bundle(self, session_id: str, bundle: dict[str, str]) -> list[SessionArtifact]:
        artifacts: list[SessionArtifact] = []
        for name, content in bundle.items():
            if name.endswith(".json"):
                artifacts.append(self.artifact_store.put_text(session_id, name, content, "application/json"))
            elif name.endswith(".py"):
                artifacts.append(self.artifact_store.put_text(session_id, name, content, "text/x-python"))
            elif name.endswith(".md"):
                artifacts.append(self.artifact_store.put_text(session_id, name, content, "text/markdown"))
            else:
                artifacts.append(self.artifact_store.put_text(session_id, name, content, "text/plain"))
        return artifacts

    def _validate_bundle(self, bundle: dict[str, str]) -> ValidationResult:
        errors: list[str] = []
        module_trees: dict[str, ast.AST] = {}
        for name, content in bundle.items():
            if name.endswith(".py"):
                try:
                    module_trees[name] = ast.parse(content, filename=name)
                except SyntaxError as exc:
                    errors.append(f"{name}: {exc}")
        if errors:
            return ValidationResult(ok=False, errors=errors)

        errors.extend(self._validate_python_bundle_contracts(module_trees))

        return ValidationResult(ok=not errors, errors=errors)

    def _validate_python_bundle_contracts(self, module_trees: dict[str, ast.AST]) -> list[str]:
        errors: list[str] = []
        available_modules = {name[:-3] for name in module_trees}

        for name, tree in module_trees.items():
            imported_local_modules = self._local_import_targets(tree)
            missing = sorted(module for module in imported_local_modules if module not in available_modules)
            if missing:
                errors.append(f"{name}: missing generated module(s): {', '.join(missing)}")

        client_tree = module_trees.get("client.py")
        if client_tree and not self._has_top_level_class(client_tree):
            errors.append("client.py: expected at least one top-level client class.")

        smoke_tree = module_trees.get("smoke_test.py")
        if smoke_tree and not self._has_function(smoke_tree, "run_smoke_test"):
            errors.append("smoke_test.py: expected a run_smoke_test function.")

        mcp_tree = module_trees.get("mcp_server.py")
        if mcp_tree and not self._assigns_name(mcp_tree, "app"):
            errors.append("mcp_server.py: expected a top-level app object.")

        return errors

    def _load_endpoints(self, session_id: str) -> list[EndpointRecord]:
        payload = self._load_json_artifact(session_id, "endpoint_graph.json")
        return [EndpointRecord.model_validate(item) for item in payload]

    def _load_json_artifact(self, session_id: str, name: str) -> Any:
        raw, _ = self.artifact_store.get_bytes(session_id, name)
        return json.loads(raw.decode("utf-8"))

    def _event(self, session_id: str, phase: SessionPhase, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
        self.repository.append_event(
            session_id,
            SessionEvent(phase=phase, event_type=event_type, message=message, payload=payload or {}),
        )

    def _require_session(self, session_id: str) -> SessionRecord:
        session = self.repository.get_session(session_id)
        if not session:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    @staticmethod
    def _class_name_from_url(api_url: str) -> str:
        hostname = api_url.split("//")[-1].split("/")[0].split(":")[0]
        parts = [part.capitalize() for part in hostname.replace("-", ".").split(".") if part and part != "api"]
        return "".join(parts[:3] or ["Generated"])

    @staticmethod
    def _method_name(endpoint: EndpointRecord) -> str:
        cleaned = endpoint.path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        cleaned = cleaned.replace("-", "_") or "root"
        prefix = {
            "GET": "get",
            "POST": "create",
            "PUT": "update",
            "PATCH": "patch",
            "DELETE": "delete",
        }.get(endpoint.method.upper(), "call")
        return f"{prefix}_{cleaned}".lower()

    @staticmethod
    def _strip_code_fences(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                return "\n".join(lines[1:-1]).strip()
        return stripped

    @staticmethod
    def _merge_artifacts(existing: list[SessionArtifact], new: list[SessionArtifact]) -> list[SessionArtifact]:
        merged = {artifact.name: artifact for artifact in existing}
        for artifact in new:
            merged[artifact.name] = artifact
        return list(merged.values())

    @staticmethod
    def _local_import_targets(tree: ast.AST) -> set[str]:
        local_imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in {"client", "usage_examples", "smoke_test", "mcp_server"}:
                        local_imports.add(root)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root in {"client", "usage_examples", "smoke_test", "mcp_server"}:
                    local_imports.add(root)
        return local_imports

    @staticmethod
    def _has_top_level_class(tree: ast.AST) -> bool:
        return any(isinstance(node, ast.ClassDef) for node in getattr(tree, "body", []))

    @staticmethod
    def _has_function(tree: ast.AST, name: str) -> bool:
        return any(isinstance(node, ast.FunctionDef) and node.name == name for node in getattr(tree, "body", []))

    @staticmethod
    def _assigns_name(tree: ast.AST, name: str) -> bool:
        for node in getattr(tree, "body", []):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return True
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                return True
        return False
