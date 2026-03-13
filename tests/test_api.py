from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.workflows import get_container


def test_create_and_fetch_session():
    get_container.cache_clear()
    client = TestClient(app)

    create = client.post(
        "/api/sessions",
        json={"api_url": "https://api.github.com"},
    )
    assert create.status_code == 200
    payload = create.json()
    assert payload["status"] == "created"

    fetch = client.get(f"/api/sessions/{payload['session_id']}")
    assert fetch.status_code == 200
    summary = fetch.json()
    assert summary["api_url"] == "https://api.github.com"
    assert summary["status"] == "created"
