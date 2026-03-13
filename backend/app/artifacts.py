from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

import boto3

from .config import Settings
from .models import SessionArtifact


class ArtifactStore(ABC):
    @abstractmethod
    def put_text(self, session_id: str, name: str, content: str, content_type: str) -> SessionArtifact:
        raise NotImplementedError

    @abstractmethod
    def put_json(self, session_id: str, name: str, payload: object) -> SessionArtifact:
        raise NotImplementedError

    @abstractmethod
    def get_bytes(self, session_id: str, name: str) -> tuple[bytes, str]:
        raise NotImplementedError

    @abstractmethod
    def list_artifacts(self, session_id: str) -> list[SessionArtifact]:
        raise NotImplementedError


class LocalArtifactStore(ArtifactStore):
    def __init__(self, root: str = ".luca_local/artifacts"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put_text(self, session_id: str, name: str, content: str, content_type: str) -> SessionArtifact:
        path = self.root / session_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return SessionArtifact(
            name=name,
            content_type=content_type,
            size=len(content.encode("utf-8")),
            storage_key=str(path),
        )

    def put_json(self, session_id: str, name: str, payload: object) -> SessionArtifact:
        return self.put_text(session_id, name, json.dumps(payload, indent=2), "application/json")

    def get_bytes(self, session_id: str, name: str) -> tuple[bytes, str]:
        path = self.root / session_id / name
        content_type = "application/octet-stream"
        if path.suffix == ".json":
            content_type = "application/json"
        elif path.suffix == ".py":
            content_type = "text/x-python"
        elif path.suffix == ".md":
            content_type = "text/markdown"
        elif path.suffix == ".txt":
            content_type = "text/plain"
        return path.read_bytes(), content_type

    def list_artifacts(self, session_id: str) -> list[SessionArtifact]:
        directory = self.root / session_id
        if not directory.exists():
            return []
        artifacts: list[SessionArtifact] = []
        for path in sorted(directory.iterdir()):
            if path.is_file():
                artifacts.append(
                    SessionArtifact(
                        name=path.name,
                        content_type=self.get_bytes(session_id, path.name)[1],
                        size=path.stat().st_size,
                        storage_key=str(path),
                    )
                )
        return artifacts


class S3ArtifactStore(ArtifactStore):
    def __init__(self, settings: Settings):
        self.bucket = settings.artifacts_bucket
        self.client = boto3.client("s3", region_name=settings.aws_region)

    @staticmethod
    def _key(session_id: str, name: str) -> str:
        return f"sessions/{session_id}/{name}"

    def put_text(self, session_id: str, name: str, content: str, content_type: str) -> SessionArtifact:
        key = self._key(session_id, name)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType=content_type,
        )
        return SessionArtifact(
            name=name,
            content_type=content_type,
            size=len(content.encode("utf-8")),
            storage_key=key,
        )

    def put_json(self, session_id: str, name: str, payload: object) -> SessionArtifact:
        return self.put_text(session_id, name, json.dumps(payload, indent=2), "application/json")

    def get_bytes(self, session_id: str, name: str) -> tuple[bytes, str]:
        key = self._key(session_id, name)
        result = self.client.get_object(Bucket=self.bucket, Key=key)
        return result["Body"].read(), result.get("ContentType", "application/octet-stream")

    def list_artifacts(self, session_id: str) -> list[SessionArtifact]:
        prefix = f"sessions/{session_id}/"
        result = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        artifacts: list[SessionArtifact] = []
        for item in result.get("Contents", []):
            name = item["Key"].removeprefix(prefix)
            if not name:
                continue
            head = self.client.head_object(Bucket=self.bucket, Key=item["Key"])
            artifacts.append(
                SessionArtifact(
                    name=name,
                    content_type=head.get("ContentType", "application/octet-stream"),
                    size=item["Size"],
                    storage_key=item["Key"],
                )
            )
        return artifacts


def build_artifact_store(settings: Settings) -> ArtifactStore:
    if settings.artifact_mode == "s3" and settings.artifacts_bucket:
        return S3ArtifactStore(settings)
    return LocalArtifactStore()
