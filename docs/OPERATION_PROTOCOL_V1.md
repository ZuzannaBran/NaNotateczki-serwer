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
- `actorSeq`: positive (at most `2^53-1`), strictly increasing **per document-scoped actor**;
  persisted with the pending operation.
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
| Objects | `object.create`, `object.set`, `object.transform`, `object.reorder`, `object.move`, `object.delete` |
| Rich text | `text.insert`, `text.delete`, `text.format` |
| Assets | `asset.attach`, `asset.detach` |
| Folders | `folder.create`, `folder.set`, `folder.move`, `folder.delete` |

Payload contracts for the current v2 reducers (the validators must agree
between Flutter and FastAPI; production editor cutover is still pending):

- `*.create`: `{"value": {...}}` (an empty object is permitted for a blank entity);
  any supplied `id`/`uid` must match the stable target identity.
- `*.set`, `object.transform`, `object.reorder`, `folder.move`:
  `{"properties": {"propertyName": value}}`. Each property is versioned
  separately so moving an object does not discard a concurrent color edit.
  `id` and `uid` are immutable and forbidden in `properties`.
- `*.delete`: `{}`, persistent tombstone for the exact stable ID.
- Supported v2 payloads contain exactly the specified top-level key; extra
  keys are rejected rather than silently ignored.
- `text.insert`: `{"afterAtomId": null | "<atom UUID>", "atoms":
  [{"id": "<atom UUID>", "text": "..."}]}`.
- `text.delete`: `{"atomIds": ["<atom UUID>"]}`. Atom IDs are never reused.
- `text.format`: `{"atomIds": ["<UUID>"], "attributes": {"bold": true}}`;
  setting an attribute to `null` removes it, while per-attribute Lamport
  versions resolve concurrent format edits without replacing the whole delta.
- `object.move`: `{"toPageId": "existing-page", "sortIndex": 0}`.
  Source page and object IDs remain in the operation envelope. The projector
  moves the object atomically and refuses missing/tombstoned destinations.
- `asset.attach`: `{"assetId":"image-1", "sha256":"<64 lowercase hex>",
  "sizeBytes":123, "mediaType":"image/png"}`; permitted types are PNG,
  JPEG, WebP and PDF; bytes must already be fully uploaded and verified.
- `asset.detach`: `{"assetId":"image-1"}`. Detach does not delete the
  underlying content-addressed blob. Asset references use operation precedence.

Stage 6 implements a deterministic sequence CRDT for `text.insert` and
`text.delete` in `api/app/text_sequence.py`. An editable text object must
be created with `{"value": {"objectType": "text", "text": ""}}`, and all
inserted atoms must have immutable UUIDs and anchors. Siblings are ordered by
descending (Lamport clock, actor ID, actor sequence, operation ID, atom
index), so inserting at the caret precedes an existing successor.
Text tombstones remain valid insertion anchors; `object.set` cannot replace
CRDT text or change `objectType`. Existing nonempty text requires explicit
atom migration. Quill inserts and formatting now have a per-attribute
CRDT layer. Embedded binary Quill delta inserts still require a verified
asset reference and are rejected by the adapter. These helpers do not
activate production Share:
stage 11 must migrate legacy UID state before enabling v2 writers.

## Document-scoped actors and Share v2

A v2 actor identity is bound to one account/share principal and one document.
The pure actor clock records `documentId` in its checkpoint after its first
local edit and rejects a different document after restart. An old nonempty
checkpoint without a document binding must be migrated, not guessed.
An actor must not reuse its actorSeq stream for another document. Client
SyncEngine creates a stable actorId hash using its actor identity, checkpoint
namespace and documentId. Direct dependencies must belong to the same
document; dependencies on unrelated owner documents would be invisible to a
Share guest. Undo/Redo must include revertsOperationId in dependencies.
The Python and Dart envelope validators reject missing inverse dependencies,
control characters and identifiers over 128 characters.

Share REST operations use X-Share-Id with the guest ticket. The v2 WebSocket
authenticate message uses shareId with the same ticket. These transport
capabilities do not activate production Share until client reducers and
the v1→v2 migration are ready.

### Compact filtered frontier

Compact `causalCursor` represents a durable prefix of the **authorized,
filtered** operation feed, not a claim that every global cursor belongs to
the shared document. REST pages mark a complete visible prefix after server
authorization and validation; missing global sequence numbers are harmless
only when they contain no visible operations. A failed visible operation
blocks checkpoint advancement. Arbitrary/custom gateways must not assert
coverage without guaranteeing this invariant. Share writers cannot claim a
cursor beyond their shared document's latest operation. Remote actor
receipts are namespaced by account/share checkpoint and actor ID in SQLite.
Undo/Redo's `revertsOperationId` remains an explicit dependency.

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
and on their immediate own predecessor. An operation may carry more than 256 independent parents; the full
serialized UUID list must fit a 128 KiB causal dependency budget (39 bytes
per ID, including JSON separators). The validator never drops parents.
Exceeding that budget or the safe integer limit fails before incrementing
the local counter. A compact persistent frontier remains future work. The actor's
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


## Stage 2 verified asset upload (2026-11-04)

For PNG/JPEG/WebP/PDF bytes the client computes SHA-256, then performs
`PUT /v2/assets/{sha256}` using a raw binary request body. The server
streams into a private temporary file, checks length, SHA-256 and magic bytes,
fsyncs it, atomically renames the file and fsyncs its directory before
committing the `verified_assets` row. The client confirms the returned
digest, media type and byte length before enqueueing `asset.attach`.

`asset.attach` checks ownership, server metadata and the stored file checksum
again. `GET /v2/assets/{sha256}` returns checked bytes; Share guests can
download only blobs already referenced in their authorized document.
The Dart client rechecks downloaded SHA-256 before exposing bytes.

Apply SQL migration `0014_verified_assets.sql` before deploying the updated
API. The new routes remain behind the existing v2 protocol and feature gates.
No account is switched to v2 by this migration. This is infrastructure,
not a production editor or Share cutover. The server uses
`/data/assets-v2/<owner-id>/<digest-prefix>/<digest>`.

## Stage 2 Quill inline image/PDF embeds (2026-11-04)

A Quill embed is one text atom with `text: "\\uFFFC"`, a stable UUID and
exactly one `embed` entry. The only supported JSON Delta inserts are
`{"image":"asset:<assetId>"}` and `{"pdf":"asset:<assetId>"}`.
The Delta snapshot renders the object insert, while its plain text uses
the Unicode object-replacement character. Formatting still applies to that
same stable atom ID.

The client must upload asset bytes and receive a verified receipt, then emit
`asset.attach` on the **same text object** before `text.insert` references
its `assetId`. The projector validates that the referenced attachment exists
and has the correct MIME: PNG/JPEG/WebP for image, PDF for pdf. External URLs,
device paths and bare replacement characters are rejected; unsupported
Quill embed kinds are rejected rather than flattened to text.

`asset.detach`, or attaching different bytes to an already referenced
asset ID, is rejected while any visible text atom uses that asset. First
emit `text.delete` for the embed, then `asset.detach`. Tombstoned text
atoms remain available as causal anchors; they do not keep the visible
attachment alive. These operations remain part of the v2 infrastructure:
production EditorController and legacy Share still use v1 pending cutover.

## Compact committed-prefix frontier (optional)

An operation may include `causalCursor` (positive safe integer). It denotes
all committed operations of the authenticated owner through that global
operation-feed cursor. Explicit `dependencies` remain mandatory for any
unacknowledged operations, later feed items and Undo references. The server
checks the owner cursor exists, prevents clocks older than included document
history, and verifies same-actor predecessor is either an explicit UUID or
covered by this cursor. No server-side memory buffer or new SQL table is
required. Legacy envelopes omit `causalCursor` and retain their semantics.

Only a contiguous, durably applied feed checkpoint may be used to generate
a compact cursor. A client must never treat a partial page, an ACK alone, or
a received event after a feed gap as covered. Deploy the server before
allowing clients to generate this optional field.


A compact cursor must identify an exact persisted operation within the
authorized owner history (or within the visible shared document for a guest).
It is not sufficient that the number be below the largest server cursor:
PostgreSQL sequences may have gaps. A missing historical cursor is rejected
with HTTP 422; a cursor beyond the latest known position is retryable.
