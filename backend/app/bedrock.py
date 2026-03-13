from __future__ import annotations

import json
import math
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import Settings


class BedrockUnavailableError(RuntimeError):
    """Raised when Bedrock access is required but not configured."""


class BedrockRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    @property
    def text_model_enabled(self) -> bool:
        return bool(self.settings.bedrock_text_model_id)

    @property
    def embeddings_enabled(self) -> bool:
        return bool(self.settings.bedrock_embed_model_id)

    def converse(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tool_config: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        if not self.text_model_enabled:
            raise BedrockUnavailableError("LUCA_BEDROCK_TEXT_MODEL_ID is not configured.")

        request: dict[str, Any] = {
            "modelId": self.settings.bedrock_text_model_id,
            "system": [{"text": system_prompt}],
            "messages": messages,
            "inferenceConfig": {"temperature": temperature, "maxTokens": max_tokens},
        }
        if tool_config:
            request["toolConfig"] = tool_config
        try:
            return self.client.converse(**request)
        except (BotoCoreError, ClientError) as exc:
            raise BedrockUnavailableError(str(exc)) from exc

    def converse_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> str:
        response = self.converse(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        message = response.get("output", {}).get("message", {})
        text_blocks = []
        for block in message.get("content", []):
            if "text" in block:
                text_blocks.append(block["text"])
        return "\n".join(text_blocks).strip()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.embeddings_enabled:
            raise BedrockUnavailableError("LUCA_BEDROCK_EMBED_MODEL_ID is not configured.")
        return [self._embed_text_bedrock(text) for text in texts]

    def _embed_text_bedrock(self, text: str) -> list[float]:
        request_body = {
            "schemaVersion": "nova-multimodal-embed-v1",
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "GENERIC_INDEX",
                "embeddingDimension": 384,
                "text": {"truncationMode": "END", "value": text},
            },
        }
        try:
            response = self.client.invoke_model(
                body=json.dumps(request_body),
                modelId=self.settings.bedrock_embed_model_id,
                accept="application/json",
                contentType="application/json",
            )
            body = json.loads(response["body"].read())
            if "embedding" in body:
                return body["embedding"]
            embeddings = body.get("embeddings") or body.get("output", {}).get("embeddings") or []
            if not embeddings:
                raise BedrockUnavailableError("Nova embeddings response did not include an embedding vector.")
            first = embeddings[0]
            return first.get("embedding", first)
        except (BotoCoreError, ClientError) as exc:
            raise BedrockUnavailableError(str(exc)) from exc


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    numerator = sum(left[i] * right[i] for i in range(size))
    left_norm = math.sqrt(sum(left[i] * left[i] for i in range(size))) or 1.0
    right_norm = math.sqrt(sum(right[i] * right[i] for i in range(size))) or 1.0
    return numerator / (left_norm * right_norm)
