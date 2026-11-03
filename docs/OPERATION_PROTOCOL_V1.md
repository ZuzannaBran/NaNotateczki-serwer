# Operation Protocol v1 — NaNotateczki

Status: **protocol defined; backend v2 staged, clients not switched**.
Stage 4 adds transactional PostgreSQL persistence and v2 read/write/WebSocket
endpoints. Legacy snapshot/backup and Share APIs stay active until the
controlled client migration and rollout. See
[OPERATIONS_V2_BACKEND.md](OPERATIONS_V2_BACKEND.md).

## Scope and identity

Every operation is a durable, immutable user intent. It does **not** carry the
entire notebook, backup, zoom, viewport, cursor, selection, or transient pen
preview. State is rebuilt from a checkpoint followed by accepted operations.

- `schemaVersion`: integer, exactly `1`.
- `operationId`: UUID (generated once, reused unchanged on retry).
- `actorId`: persistent actor/installation identity scoped to one account
  namespace or one Share guest session. Changing storage namespace creates a
  new actor; actor IDs must never be reused across unrelated namespaces.
- `actorSeq`: positive (at most `2^53-1`), strictly increasing **per actor and namespace**,
  across all documents in that namespace; persisted with the pending operation.
- `documentId`: stable document UID; the reserved literal `@library`
  denotes folder operations.
- `pageId` and `objectId`: stable IDs; for page operations `objectId`
  is the affected page ID and `pageId` is absent; for normal page objects
  both are required; document operations require neither.
- `type`: one of the types below.
- `payload`: JSON object, no inline binary assets or screen-local state.
- `dependencies`: unique UUIDs of operations observed before this intent;
  may include an operation from another actor. No self-references.
- `logicalClock`: positive Lamport counter (at most `2^53-1`). Monotonically increased when
  creating an operation or observing any remote clock. Must be strictly larger
  than the clocks of its direct dependencies and previous actor operation.
- `revertsOperationId` (optional): the operation being undone/redone.
  Undo/Redo **append an inverse/new operation**; they never erase history.

Stable IDs already exist for document, page, ink, text and image entities in
the domain model. New folder IDs must be introduced before folder operations
are emitted (stage 8). Renaming an entity does not change its ID.

### Example

```json
{
  "schemaVersion": 1,
  "operationId": "a267f93b-0f10-4c08-a906-5076f0a78f3c",
  "actorId": "device-a",
  "actorSeq": 42,
  "documentId": "note-uid",
  "pageId": "page-uid",
  "objectId": "stroke-uid",
  "type": "stroke.create",
  "payload": {
    "value": {"points": [{"dx": 1, "dy": 2, "pressure": 1}]}
  },
  "dependencies": ["589c2825-2193-4c19-a0df-4e0dcb4f28f6"],
  "logicalClock": 106
}
```

## Operation types

| Target | Types |
| --- | --- |
| Document | `document.create`, `document.set`, `document.delete` |
| Page | `page.create`, `page.set`, `page.delete` |
| Ink | `stroke.create` |
| Objects | `object.create`, `object.set`, `object.transform`, `object.reorder`, `object.delete` |
| Rich text | `text.insert`, `text.delete` |
| Assets | `asset.attach`, `asset.detach` |
| Folders | `folder.create`, `folder.set`, `folder.move`, `folder.delete` |

Planned payload contracts for reducers (the envelope currently validates JSON
shape and identity; semantic validation will be added before activating API v2):

- `*.create`: `{"value": {...}}`, complete initial object with stable ID.
- `*.set`, `object.transform`, `object.reorder`, `folder.move`:
  `{"properties": {"propertyName": value}}`. Each property is versioned
  separately so moving an object does not discard a concurrent color edit.
- `*.delete`: `{}`, persistent tombstone for the exact stable ID.
- `text.insert`: `{"afterAtomId": null | "<atom UUID>", "atoms":
  [{"id": "<atom UUID>", "text": "..."}]}`.
- `text.delete`: `{"atomIds": ["<atom UUID>"]}`. Atom IDs are never reused.
- `asset.attach`: `{"assetId": "...", "sha256": "..."}`; actual image/PDF
  bytes travel via a separate verified asset channel.

Text concurrent-insert positioning needs a tested sequence CRDT/OT reducer
(stage 6); this model does not pretend a generic property LWW reducer can
merge arbitrary Quill deltas.

## Document-scoped actors and Share v2

A v2 actor identity is bound to one account/share principal and one document.
An actor must not reuse its actorSeq stream for another document. Client
SyncEngine creates a stable actorId hash using its actor identity, checkpoint
namespace and documentId. Direct dependencies must belong to the same
document; dependencies on unrelated owner documents would be invisible to a
Share guest. Undo/Redo must include revertsOperationId in dependencies.

Share REST operations use X-Share-Id with the guest ticket. The v2 WebSocket
authenticate message uses shareId with the same ticket. These transport
capabilities do not activate production Share until client reducers and
the v1→v2 migration are ready.

## Causality and conflict policy

1. Deduplicate by `operationId`. Same ID with changed content is rejected.
   `(actorId, actorSeq)` must also be unique inside a namespace.
2. Never apply a missing dependency or a missing actor predecessor. Queue the
   operation and retry after a sync gap is filled, not by advancing time.
3. Use Lamport clocks, not device wall-clock time, for deterministic
   precedence. Among concurrent writes to **the same property**, compare
   `(logicalClock, actorId, actorSeq, operationId)` lexicographically.
   The greater key wins. All independent properties and new object IDs remain.
4. Delete wins over any edit to the deleted UID, even if that edit arrives
   later. A conscious new object uses a **new ID**, not a resurrected one.
   Document/page/folder tombstones also suppress descendants.
5. An operation must belong to the authorized account/share and target; the
   server validates ownership, actor binding, permissions, shape and limits.
6. A client keeps its own pending edits during reconnect, and never replaces
   them with an older snapshot. Server timestamps are diagnostic only.
7. Undo/Redo produce a new compensating operation and new `operationId`,
   with a dependency on the action being reversed. Undo of a deletion may
   create a replacement **with a new entity ID**, never clear a tombstone.

If two devices work offline without seeing one another, their physical gesture
order cannot be reconstructed. This contract guarantees actor ordering, causal
ordering, and eventual deterministic convergence, not invented real-time order.

## Stage-2 safety invariants

The pure buffer uses copy-on-write staging. An invalid incoming operation
never partially advances the accepted frontier. If an older buffered operation
becomes invalid when its parent arrives, that buffered operation is isolated
in a diagnostic `rejected` map and its reserved sequence is released; the
valid parent is still returned for application. Rejected IDs and reasons
are retained in a lossless checkpoint. A correction must carry a new
`operationId` because operation IDs identify immutable intents.
Dependents of a rejected ID stay blocked until explicitly reconciled.

`OperationCheckpoint` deliberately retains complete accepted envelopes,
including all causal dependencies and payloads. Replaying this history on
restore validates actor continuity, Lamport clocks and altered retransmissions.
A bare actor sequence or a set of known IDs is **not** a safe checkpoint.
Pending operations belong to the durable outbox, not to a committed snapshot.
This prototype must not compact historical envelopes until stage 3 provides
a durable ID-to-envelope lookup and a verified causal frontier.

`OperationActorClock.observe()` remembers the latest accepted remote
operation per actor. New local operations depend on those observed frontiers
and on their immediate own predecessor. Exceeding 256 dependencies or the
safe integer limit fails before incrementing the local counter. The actor's
observed frontier must be checkpointed together with its sequence and clock.

## Checkpoints and rollout

Stage 2 implements pure `CausalOperationBuffer`, Lamport comparator and
scalar-property tombstone registers in both Python and Dart; stage 3 adds durable SQLite
operations/outbox/checkpoints; stage 4 adds transactional server persistence and
API v2. Snapshot compaction must retain the actor sequence frontier and a
causal checkpoint that resolves compacted dependencies. Until then, operation
generators and buffers are **not wired into production**. Never send these
messages through existing v1 snapshot endpoints.

Canonical description is kept in `NaNotateczki-serwer`. Flutter counterparts
are `lib/data/sync/operation_protocol.dart` and later
`lib/data/sync/operation_ordering.dart`.
