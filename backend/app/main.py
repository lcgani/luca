from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from mangum import Mangum

from .models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DiscoverRequest,
    GenerateRequest,
    SessionEvent,
    SessionPhase,
    SessionRecord,
    SessionSummaryResponse,
    SessionStatus,
    StartWorkflowResponse,
)
from .workflows import get_container


app = FastAPI(title="LUCA API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "luca"}


@app.post("/api/sessions", response_model=CreateSessionResponse)
def create_session(request: CreateSessionRequest) -> CreateSessionResponse:
    container = get_container()
    session = SessionRecord(
        api_url=request.api_url.rstrip("/"),
        docs_url=request.docs_url,
        auth_input=request.auth_input,
        probe_budget=container.settings.default_probe_budget,
    )
    container.repository.create_session(session)
    container.repository.append_event(
        session.session_id,
        SessionEvent(phase=SessionPhase.CREATED, event_type="session.created", message="Session created."),
    )
    return CreateSessionResponse(session_id=session.session_id, status=session.status)


@app.get("/api/sessions/{session_id}", response_model=SessionSummaryResponse)
def get_session(session_id: str) -> SessionSummaryResponse:
    container = get_container()
    session = container.repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.artifacts = container.artifact_store.list_artifacts(session_id)
    return SessionSummaryResponse.model_validate(session.model_dump(mode="json"))


@app.get("/api/sessions/{session_id}/events", response_model=list[SessionEvent])
def list_events(session_id: str) -> list[SessionEvent]:
    container = get_container()
    session = container.repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return container.repository.list_events(session_id)


@app.post("/api/sessions/{session_id}/discover", response_model=StartWorkflowResponse)
def start_discovery(session_id: str, request: DiscoverRequest) -> StartWorkflowResponse:
    container = get_container()
    session = container.repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    probe_budget = request.probe_budget or session.probe_budget or container.settings.default_probe_budget
    session.status = SessionStatus.QUEUED
    session.phase = SessionPhase.DISCOVERY
    session.probe_budget = probe_budget
    container.repository.save_session(session)
    status, mode = container.workflow_launcher.start_discovery(session_id, probe_budget)
    return StartWorkflowResponse(session_id=session_id, status=status, mode=mode)


@app.post("/api/sessions/{session_id}/generate", response_model=StartWorkflowResponse)
def start_generation(session_id: str, request: GenerateRequest) -> StartWorkflowResponse:
    container = get_container()
    session = container.repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if request.language.lower() != "python":
        raise HTTPException(status_code=400, detail="Only Python generation is supported in the MVP.")
    session.status = SessionStatus.QUEUED
    session.phase = SessionPhase.GENERATION
    container.repository.save_session(session)
    status, mode = container.workflow_launcher.start_generation(session_id)
    return StartWorkflowResponse(session_id=session_id, status=status, mode=mode)


@app.get("/api/sessions/{session_id}/artifacts")
def list_artifacts(session_id: str) -> list[dict]:
    container = get_container()
    session = container.repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    artifacts = container.artifact_store.list_artifacts(session_id)
    return [artifact.model_dump(mode="json") for artifact in artifacts]


@app.get("/api/sessions/{session_id}/artifacts/{name}")
def get_artifact(session_id: str, name: str) -> Response:
    container = get_container()
    session = container.repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        payload, content_type = container.artifact_store.get_bytes(session_id, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


lambda_handler = Mangum(app)
