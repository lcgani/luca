from backend.app.auth import HybridAuthClassifier
from backend.app.bedrock import BedrockRuntime
from backend.app.config import Settings
from backend.app.models import AuthSignal


def test_model_auth_classifier_returns_notion_headers():
    settings = Settings(bedrock_text_model_id="test-model")
    runtime = BedrockRuntime(settings)
    classifier = HybridAuthClassifier(runtime)

    runtime.converse_text = lambda **kwargs: """
    {
      "auth_type": "notion",
      "confidence": 0.95,
      "rationale": "The evidence consistently points to a bearer token plus Notion-Version header.",
      "required_headers": {
        "Authorization": "Bearer <token>",
        "Notion-Version": "2022-06-28"
      },
      "required_query_params": []
    }
    """

    result = classifier.classify(
        [
            AuthSignal(signal_type="notion", confidence=0.92, source="docs"),
            AuthSignal(signal_type="bearer", confidence=0.88, source="headers"),
        ],
        context="Docs mention Notion-Version and a bearer token.",
    )

    assert result.auth_type == "notion"
    assert "Authorization" in result.required_headers
    assert "Notion-Version" in result.required_headers
