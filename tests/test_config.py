from backend.app.config import Settings


def test_settings_accept_field_name_for_aliased_model_ids():
    settings = Settings(
        bedrock_text_model_id="test-text-model",
        bedrock_embed_model_id="test-embed-model",
    )

    assert settings.bedrock_text_model_id == "test-text-model"
    assert settings.bedrock_embed_model_id == "test-embed-model"
