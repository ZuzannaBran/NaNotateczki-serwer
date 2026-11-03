"""Versioned editor operation wire format; not connected to the v1 API yet."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = 1
MAX_SAFE_INTEGER = 9007199254740991
LIBRARY_DOCUMENT_ID = "@library"
_INVALID_ID_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
KINDS: dict[str, str] = {
    "document.create": "document",
    "document.set": "document",
    "document.delete": "document",
    "page.create": "page",
    "page.set": "page",
    "page.delete": "page",
    "stroke.create": "object",
    "object.create": "object",
    "object.set": "object",
    "object.transform": "object",
    "object.reorder": "object",
    "object.delete": "object",
    "text.insert": "object",
    "text.delete": "object",
    "asset.attach": "object",
    "asset.detach": "object",
    "folder.create": "folder",
    "folder.set": "folder",
    "folder.move": "folder",
    "folder.delete": "folder",
    "task.create": "task",
    "task.set": "task",
    "task.move": "task",
    "task.delete": "task",
}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({
            key: _freeze(item) for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class InvalidOperation(ValueError):
    """Malformed or unsupported operation, never an accepted remote edit."""


@dataclass(frozen=True)
class OperationEnvelope:
    operation_id: str
    actor_id: str
    actor_seq: int
    document_id: str
    kind: str
    payload: dict[str, Any]
    dependencies: tuple[str, ...]
    logical_clock: int
    object_id: str | None = None
    page_id: str | None = None
    reverts_operation_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OperationEnvelope:
        if not isinstance(raw, dict):
            raise InvalidOperation("Operation must be an object.")
        try:
            seq = raw["actorSeq"]
            clock = raw["logicalClock"]
            version = raw["schemaVersion"]
            if any(type(value) is not int for value in (seq, clock, version)):
                raise InvalidOperation("Sequence and clock must be integers.")
            dependencies = raw["dependencies"]
            if not isinstance(dependencies, list) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise InvalidOperation("Dependencies must be a list of IDs.")
            payload = raw["payload"]
            if not isinstance(payload, dict):
                raise InvalidOperation("Payload must be an object.")
            # Make this envelope independent from the mutable caller dictionary.
            serialized = json.dumps(payload, allow_nan=False)
            payload = _freeze(json.loads(serialized))
            values = (
                raw["operationId"], raw["actorId"], raw["documentId"],
                raw["type"],
            )
            if not all(isinstance(value, str) for value in values):
                raise InvalidOperation("Identifiers and type must be strings.")
            for key in ("objectId", "pageId", "revertsOperationId"):
                if (
                    key in raw
                    and raw[key] is not None
                    and not isinstance(raw[key], str)
                ):
                    raise InvalidOperation(f"Invalid optional field: {key}")
            op = cls(
                operation_id=raw["operationId"],
                actor_id=raw["actorId"],
                actor_seq=seq,
                document_id=raw["documentId"],
                kind=raw["type"],
                payload=payload,
                dependencies=tuple(dependencies),
                logical_clock=clock,
                object_id=raw.get("objectId"),
                page_id=raw.get("pageId"),
                reverts_operation_id=raw.get("revertsOperationId"),
                schema_version=version,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, InvalidOperation):
                raise
            raise InvalidOperation("Malformed operation envelope.") from exc
        op.validate()
        return op

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidOperation("Unsupported operation schema.")
        if not isinstance(self.operation_id, str) or not _UUID.fullmatch(
            self.operation_id
        ):
            raise InvalidOperation("Invalid operation UUID.")
        if self.reverts_operation_id is not None and not _UUID.fullmatch(
            self.reverts_operation_id
        ):
            raise InvalidOperation("Invalid inverse reference.")
        if (
            not isinstance(self.actor_id, str)
            or not 1 <= len(self.actor_id) <= 128
            or not isinstance(self.document_id, str)
            or not 1 <= len(self.document_id) <= 128
            or type(self.actor_seq) is not int
            or not 1 <= self.actor_seq <= MAX_SAFE_INTEGER
            or type(self.logical_clock) is not int
            or not 1 <= self.logical_clock <= MAX_SAFE_INTEGER
        ):
            raise InvalidOperation(
                "Invalid actor, document, sequence or clock."
            )
        if (
            len(self.dependencies) > 256
            or len(set(self.dependencies)) != len(self.dependencies)
            or self.operation_id in self.dependencies
            or any(not _UUID.fullmatch(item) for item in self.dependencies)
            or (
                self.reverts_operation_id is not None
                and self.reverts_operation_id not in self.dependencies
            )
        ):
            raise InvalidOperation("Invalid causal dependencies.")
        for identifier in (
            self.actor_id, self.document_id, self.page_id, self.object_id
        ):
            if identifier is not None and (
                len(identifier) > 128
                or _INVALID_ID_CHARACTER.search(identifier) is not None
            ):
                raise InvalidOperation("Invalid target identifier.")
        target = KINDS.get(self.kind)
        if target is None:
            raise InvalidOperation("Unknown operation type.")
        if target == "document" and (
            self.object_id is not None or self.page_id is not None
        ):
            raise InvalidOperation("Document operation has no child ID.")
        if target == "page" and (
            not self.object_id or self.page_id is not None
        ):
            raise InvalidOperation("Page needs objectId but not pageId.")
        if target == "object" and (not self.object_id or not self.page_id):
            raise InvalidOperation("Object needs pageId and objectId.")
        if target in ("folder", "task") and (
            self.document_id != LIBRARY_DOCUMENT_ID
            or not self.object_id
            or self.page_id is not None
        ):
            raise InvalidOperation("Library entity needs objectId.")
        if target not in ("folder", "task") and self.document_id == LIBRARY_DOCUMENT_ID:
            raise InvalidOperation("Reserved library namespace.")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "operationId": self.operation_id,
            "actorId": self.actor_id,
            "actorSeq": self.actor_seq,
            "documentId": self.document_id,
            "type": self.kind,
            "payload": _thaw(self.payload),
            "dependencies": list(self.dependencies),
            "logicalClock": self.logical_clock,
        }
        if self.page_id is not None:
            result["pageId"] = self.page_id
        if self.object_id is not None:
            result["objectId"] = self.object_id
        if self.reverts_operation_id is not None:
            result["revertsOperationId"] = self.reverts_operation_id
        return result
