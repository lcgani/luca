from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from .config import Settings
from .models import SessionEvent, SessionRecord, utc_now


class SessionRepository(ABC):
    @abstractmethod
    def create_session(self, session: SessionRecord) -> SessionRecord:
        raise NotImplementedError

    @abstractmethod
    def get_session(self, session_id: str) -> SessionRecord | None:
        raise NotImplementedError

    @abstractmethod
    def save_session(self, session: SessionRecord) -> SessionRecord:
        raise NotImplementedError

    @abstractmethod
    def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        raise NotImplementedError

    @abstractmethod
    def list_events(self, session_id: str) -> list[SessionEvent]:
        raise NotImplementedError


@dataclass
class MemorySessionRepository(SessionRepository):
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    events: dict[str, list[SessionEvent]] = field(default_factory=dict)

    def create_session(self, session: SessionRecord) -> SessionRecord:
        self.sessions[session.session_id] = session
        self.events.setdefault(session.session_id, [])
        return session

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    def save_session(self, session: SessionRecord) -> SessionRecord:
        session.updated_at = utc_now()
        self.sessions[session.session_id] = session
        self.events.setdefault(session.session_id, [])
        return session

    def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        self.events.setdefault(session_id, []).append(event)
        return event

    def list_events(self, session_id: str) -> list[SessionEvent]:
        return sorted(self.events.get(session_id, []), key=lambda item: item.timestamp)


class DynamoDBSessionRepository(SessionRepository):
    def __init__(self, settings: Settings):
        resource = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = resource.Table(settings.ddb_table)

    @staticmethod
    def _session_pk(session_id: str) -> str:
        return f"SESSION#{session_id}"

    def create_session(self, session: SessionRecord) -> SessionRecord:
        self.table.put_item(Item=self._session_item(session))
        return session

    def get_session(self, session_id: str) -> SessionRecord | None:
        result = self.table.get_item(
            Key={"pk": self._session_pk(session_id), "sk": "SESSION"}
        )
        item = result.get("Item")
        if not item:
            return None
        return SessionRecord.model_validate(self._from_item(item))

    def save_session(self, session: SessionRecord) -> SessionRecord:
        session.updated_at = utc_now()
        self.table.put_item(Item=self._session_item(session))
        return session

    def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        item = event.model_dump(mode="json")
        item["pk"] = self._session_pk(session_id)
        item["sk"] = f"EVENT#{event.timestamp.isoformat()}#{event.event_id}"
        item["type"] = "event"
        self.table.put_item(Item=item)
        return event

    def list_events(self, session_id: str) -> list[SessionEvent]:
        result = self.table.query(
            KeyConditionExpression=Key("pk").eq(self._session_pk(session_id))
            & Key("sk").begins_with("EVENT#")
        )
        items = result.get("Items", [])
        events = [SessionEvent.model_validate(self._from_item(item)) for item in items]
        return sorted(events, key=lambda item: item.timestamp)

    def _session_item(self, session: SessionRecord) -> dict[str, Any]:
        payload = session.model_dump(mode="json")
        payload["pk"] = self._session_pk(session.session_id)
        payload["sk"] = "SESSION"
        payload["type"] = "session"
        payload["expires_at_epoch"] = int(session.expires_at.timestamp())
        return payload

    @staticmethod
    def _from_item(item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item)
        payload.pop("pk", None)
        payload.pop("sk", None)
        payload.pop("type", None)
        payload.pop("expires_at_epoch", None)
        return payload


def build_repository(settings: Settings) -> SessionRepository:
    if settings.storage_mode == "dynamodb" and settings.ddb_table:
        return DynamoDBSessionRepository(settings)
    return MemorySessionRepository()
