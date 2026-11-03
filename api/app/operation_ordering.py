"""Pure causal ordering for protocol v1; no database writes or v1 route changes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .operation_protocol import OperationEnvelope


class OperationOrderError(ValueError):
    """A fork, invalid causality, or exhausted pending queue."""


def precedence(operation: OperationEnvelope) -> tuple[int, str, int, str]:
    """Lamport + stable tie-breaks, never client wall-clock time."""
    return (
        operation.logical_clock,
        operation.actor_id,
        operation.actor_seq,
        operation.operation_id,
    )


@dataclass
class CausalOperationBuffer:
    """Hold packets until every dependency and actor predecessor is known.

    Checkpoint actor sequences and known IDs must be committed together.
    Server persistence/authorization is introduced only in stage 4.
    """

    actor_sequences: dict[str, int] = field(default_factory=dict)
    known_ids: set[str] = field(default_factory=set)
    maximum_pending: int = 10000
    pending: dict[str, OperationEnvelope] = field(default_factory=dict)
    accepted: dict[str, OperationEnvelope] = field(default_factory=dict)
    sequence_ids: dict[str, dict[int, str]] = field(default_factory=dict)
    latest_actor_operation: dict[str, OperationEnvelope] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self.actor_sequences = dict(self.actor_sequences)
        self.known_ids = set(self.known_ids)
        if self.maximum_pending < 1 or any(
            value < 0 for value in self.actor_sequences.values()
        ):
            raise ValueError("Invalid checkpoint.")

    def missing_dependencies(self, operation: OperationEnvelope) -> list[str]:
        return [item for item in operation.dependencies if item not in self.known_ids]

    def ingest(self, operation: OperationEnvelope) -> list[OperationEnvelope]:
        known = self.pending.get(operation.operation_id) or self.accepted.get(
            operation.operation_id
        )
        if known is not None:
            if known.to_dict() != operation.to_dict():
                raise OperationOrderError("Operation ID was reused.")
            return []
        if operation.operation_id in self.known_ids:
            return []
        if operation.actor_seq <= self.actor_sequences.get(operation.actor_id, 0):
            raise OperationOrderError("Sequence precedes checkpoint.")
        actor_keys = self.sequence_ids.setdefault(operation.actor_id, {})
        old_id = actor_keys.get(operation.actor_seq)
        if old_id is not None and old_id != operation.operation_id:
            raise OperationOrderError("Actor sequence was forked.")
        if len(self.pending) >= self.maximum_pending:
            raise OperationOrderError("Causal queue is full.")
        actor_keys[operation.actor_seq] = operation.operation_id
        self.pending[operation.operation_id] = operation
        if self._has_cycle(operation.operation_id, set(), set()):
            del self.pending[operation.operation_id]
            del actor_keys[operation.actor_seq]
            raise OperationOrderError("Causal dependency cycle.")
        return self._drain()

    def _has_cycle(
        self, operation_id: str, visiting: set[str], finished: set[str]
    ) -> bool:
        if operation_id in visiting:
            return True
        if operation_id in finished:
            return False
        finished.add(operation_id)
        visiting.add(operation_id)
        operation = self.pending.get(operation_id)
        if operation:
            for parent_id in operation.dependencies:
                if parent_id in self.pending and self._has_cycle(
                    parent_id, visiting, finished
                ):
                    return True
        visiting.remove(operation_id)
        return False

    def _drain(self) -> list[OperationEnvelope]:
        applied: list[OperationEnvelope] = []
        while True:
            ready = sorted(
                (
                    op
                    for op in self.pending.values()
                    if op.actor_seq == self.actor_sequences.get(op.actor_id, 0) + 1
                    and all(parent in self.known_ids for parent in op.dependencies)
                ),
                key=precedence,
            )
            if not ready:
                return applied
            for operation in ready:
                last = self.latest_actor_operation.get(operation.actor_id)
                if last and (
                    last.operation_id not in operation.dependencies
                    or operation.logical_clock <= last.logical_clock
                ):
                    raise OperationOrderError("Invalid actor causality.")
                for parent_id in operation.dependencies:
                    parent = self.accepted.get(parent_id)
                    if parent and operation.logical_clock <= parent.logical_clock:
                        raise OperationOrderError("Invalid logical clock.")
                del self.pending[operation.operation_id]
                self.known_ids.add(operation.operation_id)
                self.accepted[operation.operation_id] = operation
                self.actor_sequences[operation.actor_id] = operation.actor_seq
                self.latest_actor_operation[operation.actor_id] = operation
                applied.append(operation)


class OperationPropertyRegister:
    """Convergent scalar-property LWW registers with permanent tombstones.

    Text atoms, transformed geometry and binary assets require dedicated
    reducers and are deliberately rejected here, not silently flattened.
    """

    def __init__(self) -> None:
        self.values: dict[tuple, dict[str, Any]] = {}
        self.versions: dict[tuple, dict[str, OperationEnvelope]] = {}
        self.tombstones: set[tuple] = set()

    @staticmethod
    def _key(operation: OperationEnvelope) -> tuple:
        if operation.kind.startswith("document."):
            return (operation.document_id, "document")
        if operation.kind.startswith("page."):
            return (operation.document_id, "page", operation.object_id)
        if operation.kind.startswith("folder."):
            return ("@library", "folder", operation.object_id)
        return (
            operation.document_id,
            "object",
            operation.page_id,
            operation.object_id,
        )

    def _deleted(self, operation: OperationEnvelope) -> bool:
        if (operation.document_id, "document") in self.tombstones:
            return True
        if operation.page_id is not None and (
            operation.document_id, "page", operation.page_id
        ) in self.tombstones:
            return True
        return self._key(operation) in self.tombstones

    def apply(self, operation: OperationEnvelope) -> None:
        if operation.kind.endswith(".delete") and operation.kind not in {
            "text.delete", "asset.detach"
        }:
            self.tombstones.add(self._key(operation))
            return
        if operation.kind.startswith("text.") or operation.kind.startswith(
            "asset."
        ):
            raise NotImplementedError("Domain-specific reducer required.")
        if self._deleted(operation):
            return
        fields = operation.payload.get(
            "properties", operation.payload.get("value")
        )
        if not isinstance(fields, dict):
            raise ValueError("Projection needs properties or value.")
        key = self._key(operation)
        values = self.values.setdefault(key, {})
        versions = self.versions.setdefault(key, {})
        for name, value in fields.items():
            if not isinstance(name, str):
                raise ValueError("Property name must be a string.")
            previous = versions.get(name)
            if previous is None or precedence(operation) > precedence(previous):
                values[name] = value
                versions[name] = operation

    def fields_for(self, operation: OperationEnvelope) -> dict | None:
        if self._deleted(operation):
            return None
        current = self.values.get(self._key(operation))
        return None if current is None else dict(current)
