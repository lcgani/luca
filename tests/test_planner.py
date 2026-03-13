from backend.app.auth import AuthInput
from backend.app.auth import HybridAuthClassifier
from backend.app.bedrock import BedrockRuntime
from backend.app.config import Settings
from backend.app.models import AuthSignal
from backend.app.planner import DiscoveryPlanner, DiscoveryToolState


class StubResponse:
    def __init__(self, status_code, headers=None, json_payload=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_payload = json_payload
        self.text = text
        self.content = text.encode("utf-8") if text else b"{}"

    def json(self):
        if self._json_payload is None:
            raise ValueError("No JSON payload")
        return self._json_payload


def test_nova_planner_chooses_auth_retry_after_401():
    settings = Settings(bedrock_text_model_id="test-model")
    runtime = BedrockRuntime(settings)
    classifier = HybridAuthClassifier(runtime)
    planner = DiscoveryPlanner(settings, runtime, classifier)

    def fake_converse_text(**kwargs):
        user_prompt = kwargs.get("user_prompt", "")
        if '"variant": "bearer"' in user_prompt:
            return '{"headers":{"Authorization":"Bearer <token>"},"query_params":{}}'
        return '{"signals":[{"signal_type":"bearer","confidence":0.91,"source":"model","details":{}}]}'

    runtime.converse_text = fake_converse_text

    responses = iter(
        [
            {
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "1",
                                    "name": "probe_endpoint",
                                    "input": {"path": "/users", "method": "GET"},
                                }
                            }
                        ]
                    }
                }
            },
            {
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "2",
                                    "name": "test_auth_variant",
                                    "input": {"path": "/users", "variant": "bearer"},
                                }
                            }
                        ]
                    }
                }
            },
            {
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "3",
                                    "name": "stop_discovery",
                                    "input": {"reason": "enough evidence"},
                                }
                            }
                        ]
                    }
                }
            },
        ]
    )
    runtime.converse = lambda **kwargs: next(responses)

    def fake_request(method, url, timeout=None, allow_redirects=None, headers=None, params=None, json=None, data=None):
        if headers:
            return StubResponse(200, {"content-type": "application/json"}, {"id": "123", "href": "/users/123"}, text='{"id":"123"}')
        return StubResponse(401, {"WWW-Authenticate": "Bearer realm=test"}, text="unauthorized")

    planner.http.request = fake_request
    state = DiscoveryToolState(
        api_url="https://api.example.com",
        auth_input=AuthInput(token="secret"),
        sources=[],
        chunks=[],
        chunk_vectors=[],
        candidate_paths=["/users"],
        resource_hints=["users"],
        discovered_endpoints=[],
        auth_signals=[AuthSignal(signal_type="bearer", confidence=0.9, source="test")],
    )

    result = planner.run(state, probe_budget=3)

    assert result.planner_trace[0]["result"]["mode"] == "nova"
    assert any(trace["action"] == "test_auth_variant" for trace in result.planner_trace)
    assert any(endpoint.requires_auth for endpoint in result.discovered_endpoints)
    auth_retry_trace = next(trace for trace in result.planner_trace if trace["action"] == "test_auth_variant")
    assert auth_retry_trace["result"]["status_code"] == 200
    assert "/users/123" in auth_retry_trace["result"]["response_excerpt"]


def test_probe_endpoint_reports_401_without_auto_retry():
    settings = Settings(bedrock_text_model_id="test-model")
    runtime = BedrockRuntime(settings)
    classifier = HybridAuthClassifier(runtime)
    planner = DiscoveryPlanner(settings, runtime, classifier)
    runtime.converse_text = lambda **kwargs: '{"signals":[{"signal_type":"bearer","confidence":0.91,"source":"model","details":{}}]}'

    planner.http.request = lambda method, url, timeout=None, allow_redirects=None, headers=None, params=None, json=None, data=None: StubResponse(
        401,
        {"WWW-Authenticate": "Bearer realm=test"},
        text="unauthorized",
    )

    state = DiscoveryToolState(
        api_url="https://api.example.com",
        auth_input=AuthInput(token="secret"),
        sources=[],
        chunks=[],
        chunk_vectors=[],
        candidate_paths=["/users"],
        resource_hints=["users"],
        discovered_endpoints=[],
        auth_signals=[],
    )

    result = planner._probe_endpoint(state, "/users", "GET")

    assert result["status_code"] == 401
    assert "auth_retry" not in result


def test_probe_endpoint_passes_query_headers_and_json_body():
    settings = Settings(bedrock_text_model_id="test-model")
    runtime = BedrockRuntime(settings)
    classifier = HybridAuthClassifier(runtime)
    planner = DiscoveryPlanner(settings, runtime, classifier)

    captured = {}

    def fake_request(method, url, timeout=None, allow_redirects=None, headers=None, params=None, json=None, data=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
                "data": data,
            }
        )
        return StubResponse(200, {"content-type": "application/json"}, {"ok": True}, text='{"ok":true}')

    runtime.converse_text = lambda **kwargs: '{"signals":[]}'
    planner.http.request = fake_request

    state = DiscoveryToolState(
        api_url="https://api.example.com",
        auth_input=None,
        sources=[],
        chunks=[],
        chunk_vectors=[],
        candidate_paths=["/events/search"],
        resource_hints=["events"],
        discovered_endpoints=[],
        auth_signals=[],
    )

    result = planner._probe_endpoint(
        state,
        "/events/search",
        "POST",
        headers={"x-test": "1"},
        query_params={"limit": "10"},
        json_body={"query": "zone2"},
    )

    assert result["status_code"] == 200
    assert captured["method"] == "POST"
    assert captured["headers"] == {"x-test": "1"}
    assert captured["params"] == {"limit": "10"}
    assert captured["json"] == {"query": "zone2"}
