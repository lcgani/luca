from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache

import boto3

from .artifacts import ArtifactStore, build_artifact_store
from .auth import HybridAuthClassifier
from .bedrock import BedrockRuntime
from .config import Settings, get_settings
from .discovery import DiscoveryService
from .generation import GenerationService
from .ingestion import IngestionService
from .planner import DiscoveryPlanner
from .storage import SessionRepository, build_repository


class WorkflowLauncher(ABC):
    @abstractmethod
    def start_discovery(self, session_id: str, probe_budget: int) -> tuple[str, str]:
        raise NotImplementedError

    @abstractmethod
    def start_generation(self, session_id: str) -> tuple[str, str]:
        raise NotImplementedError


class InlineWorkflowLauncher(WorkflowLauncher):
    def __init__(self, discovery_service: DiscoveryService, generation_service: GenerationService):
        self.discovery_service = discovery_service
        self.generation_service = generation_service

    def start_discovery(self, session_id: str, probe_budget: int) -> tuple[str, str]:
        self.discovery_service.discover_session(session_id, probe_budget)
        return "running", "inline"

    def start_generation(self, session_id: str) -> tuple[str, str]:
        self.generation_service.generate_session(session_id)
        return "running", "inline"


class StepFunctionsWorkflowLauncher(WorkflowLauncher):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = boto3.client("stepfunctions", region_name=settings.aws_region)

    def start_discovery(self, session_id: str, probe_budget: int) -> tuple[str, str]:
        self.client.start_execution(
            stateMachineArn=self.settings.discovery_state_machine_arn,
            input=json.dumps({"operation": "discover", "session_id": session_id, "probe_budget": probe_budget}),
        )
        return "queued", "stepfunctions"

    def start_generation(self, session_id: str) -> tuple[str, str]:
        self.client.start_execution(
            stateMachineArn=self.settings.generation_state_machine_arn,
            input=json.dumps({"operation": "generate", "session_id": session_id}),
        )
        return "queued", "stepfunctions"


@dataclass
class ServiceContainer:
    settings: Settings
    repository: SessionRepository
    artifact_store: ArtifactStore
    runtime: BedrockRuntime
    ingestion: IngestionService
    planner: DiscoveryPlanner
    auth_classifier: HybridAuthClassifier
    discovery_service: DiscoveryService
    generation_service: GenerationService
    workflow_launcher: WorkflowLauncher


@lru_cache(maxsize=1)
def get_container() -> ServiceContainer:
    settings = get_settings()
    repository = build_repository(settings)
    artifact_store = build_artifact_store(settings)
    runtime = BedrockRuntime(settings)
    auth_classifier = HybridAuthClassifier(runtime)
    ingestion = IngestionService(settings, runtime, auth_classifier)
    planner = DiscoveryPlanner(settings, runtime, auth_classifier)
    discovery_service = DiscoveryService(settings, repository, artifact_store, ingestion, planner, auth_classifier, runtime)
    generation_service = GenerationService(settings, repository, artifact_store, runtime)

    if (
        settings.workflow_mode == "stepfunctions"
        and settings.discovery_state_machine_arn
        and settings.generation_state_machine_arn
    ):
        workflow_launcher: WorkflowLauncher = StepFunctionsWorkflowLauncher(settings)
    else:
        workflow_launcher = InlineWorkflowLauncher(discovery_service, generation_service)

    return ServiceContainer(
        settings=settings,
        repository=repository,
        artifact_store=artifact_store,
        runtime=runtime,
        ingestion=ingestion,
        planner=planner,
        auth_classifier=auth_classifier,
        discovery_service=discovery_service,
        generation_service=generation_service,
        workflow_launcher=workflow_launcher,
    )
