"""Pure causal ordering for protocol v1, with no database writes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from .operation_protocol import (
    MAX_DEPENDENCY_BYTES, MAX_SAFE_INTEGER, OperationEnvelope,
)


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
class OperationActorClock:
    """Generate local intents with observed actor frontiers."""

    actor_id: str
    actor_seq: int = 0
    logical_clock: int = 0
    previous_operation_id: str | None = None
    observed: dict[str, OperationEnvelope] = field(default_factory=dict)
    document_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.actor_id
            or len(self.actor_id) > 128
            or not 0 <= self.actor_seq <= MAX_SAFE_INTEGER
            or not 0 <= self.logical_clock <= MAX_SAFE_INTEGER
            or (self.actor_seq > 0 and not self.previous_operation_id)
            or (self.actor_seq > 0 and not self.document_id)
            or (self.document_id is not None and not 1 <= len(self.document_id) <= 128)
        ):
            raise ValueError("Invalid actor clock.")

    def observe(self, remote: OperationEnvelope) -> None:
        """Call after applying a committed remote operation."""
        if self.document_id is not None and remote.document_id != self.document_id:
            return
        if remote.actor_id != self.actor_id:
            old = self.observed.get(remote.actor_id)
            if (
                old is not None
                and old.actor_seq == remote.actor_seq
                and old.operation_id != remote.operation_id
            ):
                raise ValueError("Forked observed actor.")
            if old is None or remote.actor_seq > old.actor_seq:
                self.observed[remote.actor_id] = remote
        self.logical_clock = max(self.logical_clock, remote.logical_clock)

    def next(
        self, *, document_id: str, kind: str, payload: dict,
        page_id: str | None = None, object_id: str | None = None,
        reverts_operation_id: str | None = None,
        dependencies: tuple[str, ...] = (),
    ) -> OperationEnvelope:
        if self.document_id is not None and document_id != self.document_id:
            raise ValueError("Actor cannot cross document scope.")
        observed = [
            item for item in self.observed.values()
            if item.document_id == document_id
        ]
        observed_ids = {item.operation_id for item in observed}
        covered_ids = {
            dependency for item in observed
            for dependency in item.dependencies
            if dependency in observed_ids
        }
        causal_ids = sorted({
            *dependencies,
            *(item for item in observed_ids if item not in covered_ids),
            *([reverts_operation_id] if reverts_operation_id else []),
            *([self.previous_operation_id] if self.previous_operation_id else []),
        })
        if (
            len(causal_ids) * 39 > MAX_DEPENDENCY_BYTES
            or self.actor_seq >= MAX_SAFE_INTEGER
            or self.logical_clock >= MAX_SAFE_INTEGER
        ):
            raise ValueError("Actor clock or dependency limit exceeded.")
        op = OperationEnvelope.from_dict({
            "schemaVersion": 1,
            "operationId": str(uuid4()),
            "actorId": self.actor_id,
            "actorSeq": self.actor_seq + 1,
            "logicalClock": self.logical_clock + 1,
            "documentId": document_id,
            "type": kind,
            "payload": payload,
            "dependencies": causal_ids,
            **({"objectId": object_id} if object_id is not None else {}),
            **({"pageId": page_id} if page_id is not None else {}),
            **({"revertsOperationId": reverts_operation_id}
               if reverts_operation_id is not None else {}),
        })
        self.actor_seq = op.actor_seq
        self.logical_clock = op.logical_clock
        self.previous_operation_id = op.operation_id
        self.document_id = document_id
        self.observed = {
            actor: remote for actor, remote in self.observed.items()
            if remote.document_id == document_id
        }
        return op

    def to_checkpoint(self) -> dict:
        return {
            "actorId": self.actor_id,
            "actorSeq": self.actor_seq,
            "logicalClock": self.logical_clock,
            **({"previousOperationId": self.previous_operation_id}
               if self.previous_operation_id is not None else {}),
            **({"documentId": self.document_id}
               if self.document_id is not None else {}),
            "observedOperations": [
                item.to_dict() for item in self.observed.values()
            ],
        }

    @classmethod
    def from_checkpoint(cls, raw: dict) -> "OperationActorClock":
        try:
            clock = cls(
                actor_id=raw["actorId"],
                actor_seq=raw["actorSeq"],
                logical_clock=raw["logicalClock"],
                previous_operation_id=raw.get("previousOperationId"),
                document_id=raw.get("documentId"),
            )
            for item in raw.get("observedOperations", []):
                remote = OperationEnvelope.from_dict(item)
                if remote.logical_clock > clock.logical_clock:
                    raise ValueError("Observed frontier exceeds actor clock.")
                clock.observe(remote)
            return clock
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid actor checkpoint.") from exc


@dataclass(frozen=True)
class OperationCheckpoint:
    """Lossless stage-2 checkpoint. No unsafe history compaction yet."""

    operations: tuple[OperationEnvelope, ...]
    schema_version: int = 1
    rejected: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "rejected", MappingProxyType(dict(self.rejected))
        )

    def to_dict(self) -> dict:
        return {
            "schemaVersion": self.schema_version,
            "operations": [
                operation.to_dict() for operation in self.operations
            ],
            "rejectedOperations": dict(self.rejected),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "OperationCheckpoint":
        if (
            not isinstance(raw, dict)
            or type(raw.get("schemaVersion")) is not int
            or raw["schemaVersion"] != 1
            or not isinstance(raw.get("operations"), list)
        ):
            raise ValueError("Invalid operation checkpoint.")
        from .operation_protocol import InvalidOperation

        try:
            operations = tuple(
                OperationEnvelope.from_dict(item) for item in raw["operations"]
            )
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Invalid checkpoint operation.") from exc
        rejected = raw.get("rejectedOperations", {})
        if (
            not isinstance(rejected, dict)
            or any(
                not isinstance(key, str) or not isinstance(reason, str)
                or not reason for key, reason in rejected.items()
            )
        ):
            raise ValueError("Invalid rejected operation records.")
        return cls(operations, rejected=dict(rejected))


@dataclass
class CausalOperationBuffer:
    """Lossless, transactional-in-memory causal ordering.

    Stage 3 must atomically persist the returned operations and checkpoint
    before acknowledging them. Compaction requires a durable ID lookup.
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
    rejected: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A bare sequence frontier cannot verify predecessor IDs, clocks
        # or retransmitted payloads. Only restore lossless checkpoints.
        if self.actor_sequences or self.known_ids:
            raise ValueError("Restore a complete OperationCheckpoint instead.")
        if self.maximum_pending < 1:
            raise ValueError("Invalid queue capacity.")

    def export_checkpoint(self) -> OperationCheckpoint:
        if self.pending:
            # Pending changes must live in the durable outbox, not a snapshot.
            raise OperationOrderError(
                "Pending operations cannot be checkpointed."
            )
        return OperationCheckpoint(
            tuple(sorted(self.accepted.values(), key=precedence)),
            rejected=dict(self.rejected),
        )

    @classmethod
    def from_checkpoint(
        cls, checkpoint: OperationCheckpoint, *, maximum_pending: int = 10000
    ) -> "CausalOperationBuffer":
        if checkpoint.schema_version != 1:
            raise ValueError("Unsupported checkpoint schema.")
        queue = cls(maximum_pending=maximum_pending)
        for operation in sorted(checkpoint.operations, key=precedence):
            queue.ingest(operation)
        if queue.pending or len(queue.accepted) != len(checkpoint.operations):
            raise ValueError("Incomplete or inconsistent causal checkpoint.")
        if any(key in queue.accepted for key in checkpoint.rejected):
            raise ValueError("Accepted and rejected operation IDs overlap.")
        queue.rejected.update(checkpoint.rejected)
        return queue

    def missing_dependencies(self, operation: OperationEnvelope) -> list[str]:
        return [
            item for item in operation.dependencies
            if item not in self.known_ids
        ]

    @staticmethod
    def _has_cycle(
        pending: dict[str, OperationEnvelope], operation_id: str,
        visiting: set[str], finished: set[str],
    ) -> bool:
        if operation_id in visiting:
            return True
        if operation_id in finished:
            return False
        visiting.add(operation_id)
        item = pending.get(operation_id)
        if item is not None:
            for parent in item.dependencies:
                if parent in pending and CausalOperationBuffer._has_cycle(
                    pending, parent, visiting, finished
                ):
                    return True
        visiting.remove(operation_id)
        finished.add(operation_id)
        return False

    def ingest(self, operation: OperationEnvelope) -> list[OperationEnvelope]:
        existing = (
            self.pending.get(operation.operation_id)
            or self.accepted.get(operation.operation_id)
        )
        if existing is not None:
            if existing.to_dict() != operation.to_dict():
                raise OperationOrderError("Operation ID was reused.")
            return []
        if operation.operation_id in self.rejected:
            raise OperationOrderError("Previously rejected operation ID.")
        if operation.operation_id in self.known_ids:
            # This should be unreachable for a complete checkpoint.
            raise OperationOrderError("Missing known operation payload.")
        if operation.actor_seq <= self.actor_sequences.get(
            operation.actor_id, 0
        ):
            raise OperationOrderError("Sequence precedes checkpoint.")
        used = self.sequence_ids.get(operation.actor_id, {}).get(
            operation.actor_seq
        )
        if used is not None:
            raise OperationOrderError("Actor sequence was forked.")
        if len(self.pending) >= self.maximum_pending:
            raise OperationOrderError("Causal queue is full.")

        # Copy-on-write: no accepted operation is lost from the return value
        # when an invalid dependent is encountered later in this drain.
        pending = dict(self.pending)
        accepted = dict(self.accepted)
        known_ids = set(self.known_ids)
        actor_sequences = dict(self.actor_sequences)
        sequence_ids = {
            actor: dict(items) for actor, items in self.sequence_ids.items()
        }
        latest = dict(self.latest_actor_operation)
        rejected = dict(self.rejected)
        sequence_ids.setdefault(operation.actor_id, {})[
            operation.actor_seq
        ] = operation.operation_id
        pending[operation.operation_id] = operation
        if self._has_cycle(pending, operation.operation_id, set(), set()):
            raise OperationOrderError("Causal dependency cycle.")

        applied: list[OperationEnvelope] = []
        while True:
            ready = sorted(
                (
                    op for op in pending.values()
                    if op.actor_seq == actor_sequences.get(op.actor_id, 0) + 1
                    and all(parent in known_ids for parent in op.dependencies)
                ),
                key=precedence,
            )
            if not ready:
                break
            for candidate in ready:
                if candidate.operation_id not in pending:
                    continue
                try:
                    last = latest.get(candidate.actor_id)
                    if last is not None and (
                        last.operation_id not in candidate.dependencies
                        or candidate.logical_clock <= last.logical_clock
                        or candidate.document_id != last.document_id
                    ):
                        raise OperationOrderError("Invalid actor causality.")
                    for parent_id in candidate.dependencies:
                        parent = accepted.get(parent_id)
                        if parent is None:
                            raise OperationOrderError(
                                "Unverifiable dependency in checkpoint."
                            )
                        if candidate.document_id != parent.document_id:
                            raise OperationOrderError(
                                "Cross-document causal dependency."
                            )
                        if candidate.logical_clock <= parent.logical_clock:
                            raise OperationOrderError("Invalid logical clock.")
                except OperationOrderError as exc:
                    if candidate.operation_id == operation.operation_id:
                        # A rejected incoming packet cannot leave any state
                        # change behind, even if earlier operations were ready.
                        raise
                    # An earlier buffered packet is now provably bad. Isolate
                    # it without swallowing the valid operations just accepted.
                    del pending[candidate.operation_id]
                    sequence_ids[candidate.actor_id].pop(candidate.actor_seq)
                    rejected[candidate.operation_id] = str(exc)
                    continue
                del pending[candidate.operation_id]
                known_ids.add(candidate.operation_id)
                accepted[candidate.operation_id] = candidate
                actor_sequences[candidate.actor_id] = candidate.actor_seq
                latest[candidate.actor_id] = candidate
                applied.append(candidate)

        self.pending = pending
        self.accepted = accepted
        self.known_ids = known_ids
        self.actor_sequences = actor_sequences
        self.sequence_ids = sequence_ids
        self.latest_actor_operation = latest
        self.rejected = rejected
        return applied


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
        if not isinstance(fields, Mapping):
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
