from backend.app.config import Settings
from backend.app.auth import HybridAuthClassifier
from backend.app.bedrock import BedrockRuntime
from backend.app.ingestion import IngestionService
from backend.app.models import SourceDocument


def test_parse_openapi_yaml_and_normalize_endpoints():
    yaml_spec = """
openapi: 3.1.0
info:
  title: Demo API
paths:
  /users:
    get:
      summary: List users
  /users/{id}:
    get:
      summary: Get user
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
"""
    settings = Settings(bedrock_text_model_id="test-model")
    runtime = BedrockRuntime(Settings(bedrock_text_model_id="test-model"))
    classifier = HybridAuthClassifier(runtime)
    service = IngestionService(settings, runtime, classifier)
    spec = service._parse_openapi(yaml_spec)

    assert spec is not None
    endpoints = service._normalize_openapi(spec)
    assert len(endpoints) == 2
    assert endpoints[0].path == "/users"
    assert endpoints[1].parameters[0].name == "id"


def test_seed_inputs_without_docs_keep_paths_minimal_and_model_hints_available():
    settings = Settings(bedrock_text_model_id="test-model")
    runtime = BedrockRuntime(settings)
    classifier = HybridAuthClassifier(runtime)
    service = IngestionService(settings, runtime, classifier)

    runtime.converse_text = lambda **kwargs: '{"resource_hints":["woodway","users"]}'

    seeded_paths = service._seed_candidate_paths(
        "https://woodway.example.com",
        sources=[],
        existing_paths=[],
    )
    resource_hints = service._analyze_repo_level_hints(
        "https://woodway.example.com",
        sources=[],
        chunks=[],
        candidate_paths=seeded_paths,
    )

    assert seeded_paths == ["/"]
    assert "woodway" in resource_hints
    assert "users" in resource_hints


def test_source_analysis_sanitizes_malformed_model_lists():
    settings = Settings(bedrock_text_model_id="test-model")
    runtime = BedrockRuntime(settings)
    classifier = HybridAuthClassifier(runtime)
    service = IngestionService(settings, runtime, classifier)

    runtime.converse_text = lambda **kwargs: """
    {
      "candidate_paths": ["/users", 42, "", "/users"],
      "resource_hints": ["Users", null, " metrics "],
      "follow_up_urls": ["https://docs.example.com/openapi.json", {"bad": true}],
      "auth_signals": [{"signal_type":"bearer","confidence":0.9,"source":"model","details":{}}, "bad"]
    }
    """
    source = SourceDocument(
        source_id="src-1",
        url="https://api.example.com",
        source_type="seed",
        content_type="text/html",
        status_code=200,
    )

    analysis = service._analyze_source_with_model(source, "<html></html>")

    assert analysis["candidate_paths"] == ["/users"]
    assert analysis["resource_hints"] == ["users", "metrics"]
    assert analysis["follow_up_urls"] == ["https://docs.example.com/openapi.json"]
    assert len(analysis["auth_signals"]) == 1


def test_follow_up_fetch_queue_allows_same_domain_subdomains():
    settings = Settings(bedrock_text_model_id="test-model")
    runtime = BedrockRuntime(settings)
    classifier = HybridAuthClassifier(runtime)
    service = IngestionService(settings, runtime, classifier)

    queued = service._follow_up_fetch_queue(
        "https://api.example.com",
        ["https://docs.example.com/openapi.json", "https://evil.com/spec.json"],
        visited_urls=set(),
    )

    assert queued == [("https://docs.example.com/openapi.json", "discovered")]
