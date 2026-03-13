from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SessionStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionPhase(str, Enum):
    CREATED = "created"
    DISCOVERY = "discovery"
    GENERATION = "generation"
    COMPLETE = "complete"


class AuthInput(BaseModel):
    token: str | None = None
    header_name: str | None = None
    header_prefix: str | None = "Bearer"
    query_param: str | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)


class AuthSignal(BaseModel):
    signal_type: str
    confidence: float
    source: str
    details: dict[str, Any] = Field(default_factory=dict)


class AuthResult(BaseModel):
    auth_type: str = "unknown"
    confidence: float = 0.0
    rationale: str = ""
    required_headers: dict[str, str] = Field(default_factory=dict)
    required_query_params: list[str] = Field(default_factory=list)


class EndpointParameter(BaseModel):
    name: str
    location: str = "query"
    required: bool = False
    description: str = ""
    schema_type: str = "string"


class EndpointRecord(BaseModel):
    method: str
    path: str
    summary: str = ""
    description: str = ""
    parameters: list[EndpointParameter] = Field(default_factory=list)
    status_code: int | None = None
    source: str = "unknown"
    requires_auth: bool | None = None
    sample_fields: list[str] = Field(default_factory=list)


class SourceDocument(BaseModel):
    source_id: str
    url: str
    source_type: str
    content_type: str
    status_code: int
    title: str | None = None
    summary: str | None = None
    storage_key: str | None = None


class DocumentChunk(BaseModel):
    chunk_id: str
    source_id: str
    text: str
    keywords: list[str] = Field(default_factory=list)


class SessionArtifact(BaseModel):
    name: str
    content_type: str
    size: int
    storage_key: str
    created_at: datetime = Field(default_factory=utc_now)


class SessionEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=utc_now)
    phase: SessionPhase
    event_type: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionRecord(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    status: SessionStatus = SessionStatus.CREATED
    phase: SessionPhase = SessionPhase.CREATED
    api_url: str
    docs_url: str | None = None
    auth_input: AuthInput | None = None
    auth_result: AuthResult = Field(default_factory=AuthResult)
    probe_budget: int = 10
    endpoint_count: int = 0
    endpoints_preview: list[EndpointRecord] = Field(default_factory=list)
    artifacts: list[SessionArtifact] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(hours=24))


class CreateSessionRequest(BaseModel):
    api_url: str
    docs_url: str | None = None
    auth_input: AuthInput | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    status: SessionStatus


class DiscoverRequest(BaseModel):
    probe_budget: int | None = None


class GenerateRequest(BaseModel):
    language: str = "python"


class StartWorkflowResponse(BaseModel):
    session_id: str
    status: str
    mode: str


class SessionSummaryResponse(BaseModel):
    session_id: str
    status: SessionStatus
    phase: SessionPhase
    api_url: str
    docs_url: str | None = None
    auth_result: AuthResult
    endpoint_count: int
    endpoints_preview: list[EndpointRecord]
    artifacts: list[SessionArtifact]
    errors: list[str]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
