import json

from backend.app.artifacts import LocalArtifactStore
from backend.app.bedrock import BedrockRuntime
from backend.app.config import Settings
from backend.app.generation import GenerationService
from backend.app.models import AuthResult, SessionRecord
from backend.app.storage import MemorySessionRepository


def test_generation_creates_bundle_from_discovery_artifacts(tmp_path):
    settings = Settings(bedrock_text_model_id="test-model")
    repository = MemorySessionRepository()
    artifact_store = LocalArtifactStore(str(tmp_path))
    runtime = BedrockRuntime(settings)
    service = GenerationService(settings, repository, artifact_store, runtime)

    def fake_converse_text(*, user_prompt, **kwargs):
        if "Build a production-friendly requests-based client class" in user_prompt:
            return """
import requests


class ExampleClient:
    def __init__(self, base_url="https://api.example.com", auth_headers=None, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.auth_headers = auth_headers or {"Authorization": "Bearer <token>"}
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method, path, **kwargs):
        response = self.session.request(method, f"{self.base_url}{path}", headers=self.auth_headers, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def get_users(self, **kwargs):
        return self._request("GET", "/users", **kwargs)
"""
        if "Show how to instantiate the client" in user_prompt:
            return 'from client import ExampleClient\n\nclient = ExampleClient()\nclient.get_users()\n'
        if "Return only markdown" in user_prompt:
            return "# Example Client\n"
        if "Define run_smoke_test()" in user_prompt:
            return """
from unittest.mock import patch

from client import ExampleClient


def run_smoke_test():
    client = ExampleClient()
    with patch("requests.sessions.Session.request") as mocked:
        mocked.return_value.status_code = 200
        mocked.return_value.content = b"{}"
        mocked.return_value.headers = {"content-type": "application/json"}
        mocked.return_value.json.return_value = {}
        client.get_users()
"""
        if "Build a runnable FastAPI JSON-RPC server" in user_prompt:
            return """
from fastapi import FastAPI

from client import ExampleClient


app = FastAPI()
client = ExampleClient()


@app.get("/tools")
def list_tools():
    return {"tools": ["get_users"]}
"""
        return user_prompt

    runtime.converse_text = fake_converse_text

    session = SessionRecord(
        api_url="https://api.example.com",
        auth_result=AuthResult(auth_type="bearer", confidence=0.9, required_headers={"Authorization": "Bearer <token>"}),
    )
    repository.create_session(session)

    artifact_store.put_json(
        session.session_id,
        "endpoint_graph.json",
        [{"method": "GET", "path": "/users", "summary": "List users"}],
    )
    artifact_store.put_text(
        session.session_id,
        "discovery_report.json",
        json.dumps({"api_url": session.api_url, "endpoint_count": 1}),
        "application/json",
    )

    result = service.generate_session(session.session_id)

    names = {artifact.name for artifact in result.artifacts}
    assert result.status == "completed"
    assert "client.py" in names
    assert "mcp_server.py" in names


def test_generation_validation_does_not_execute_generated_code(tmp_path):
    settings = Settings(bedrock_text_model_id="test-model")
    repository = MemorySessionRepository()
    artifact_store = LocalArtifactStore(str(tmp_path))
    runtime = BedrockRuntime(settings)
    service = GenerationService(settings, repository, artifact_store, runtime)

    sentinel = tmp_path / "executed.txt"
    bundle = {
        "client.py": "class ExampleClient:\n    pass\n",
        "smoke_test.py": f"def run_smoke_test():\n    open(r'{sentinel}', 'w', encoding='utf-8').write('ran')\n",
        "mcp_server.py": "app = object()\n",
    }

    validation = service._validate_bundle(bundle)

    assert validation.ok is True
    assert sentinel.exists() is False
