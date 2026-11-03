# Stage 4 — PostgreSQL operation journal and API v2

**Deployment state:** committed as an opt-in backend capability. Existing v1
notebook synchronization, device backups and Share clients stay on v1. No
automatic migration of existing notebooks occurs in this stage.

Apply migration `0010_operation_journal.sql` using the existing migration
runner **only after a verified PostgreSQL backup**. It is additive and does
not modify any historical migration.

## Transaction contract

- `document_operations` is an append-only log; operation IDs are globally
  unique and `(owner_id, actor_id, actor_seq)` is unique.
- `actor_checkpoints` binds each actor to an account or specific Share grant
  and retains its last accepted sequence, predecessor and logical clock.
- `document_states` is a JSONB materialized projection separate from v1
  `synced_notebooks`. Its `revision` advances on each accepted operation.
- `object_tombstones` permanently blocks ID recreation inside an account.
- Each batch holds the existing owner event lock and a global v2 event lock.
  Operations, actor checkpoints, tombstones, projections and event cursors
  commit together or not at all.
- Retransmitting an operation with identical envelope returns the original
  cursor and `status: "duplicate"`; a reused ID or actor sequence with a
  different payload is rejected.
- A missing predecessor or dependency produces HTTP 409 `CAUSAL_GAP`
  without committing **any** portion of that batch. The durable client outbox
  must retry using the original IDs; the backend does not persist gaps yet.
- Dependents must use larger Lamport clocks; each actor's previous operation
  must appear in `dependencies` explicitly.
- Each document/page/object/folder ID is tombstoned on delete. Stale edits
  can be committed as ignored no-ops, but they cannot recreate deleted IDs.
  Ordinary property writes resolve by stable Lamport/actor rank.
- Concurrent rich-text atoms and asset attachments deliberately return 422
  until stage 6/asset verification is implemented. They must not silently
  pass through the scalar property reducer.

## API

All REST calls require `Authorization: Bearer <access JWT>`. Guests use
`Authorization: Bearer <Share ticket>` **and**
`X-Share-Id: <Share UUID>`, with strict document scope. Grant expiry and
revocation are checked before commits. Browser CORS preflight permits
`X-Share-Id` as a request header, but does not grant authorization.

- `POST /v2/operations/batch` — body `{"operations": [<envelope>, ...]}`,
  1–50 operations, JSON request limit 1 MiB. Response:
  `{"cursor": 45, "acknowledgements": [{"operationId": "...",
  "cursor": 45, "status": "applied" | "duplicate"}]}`.
- `GET /v2/operations?after=<cursor>` — accepted operation envelopes,
  returned as `{"cursor": ..., "hasMore": true|false, "operations":
  [{"cursor": ..., "operation": <envelope>}]}`, at most 25 per page (8 MiB soft cap).
- `GET /v2/documents/{id}/snapshot` — `documentId`, `revision`,
  `cursor`, `deleted` and `state`. State and cursor are captured using
  one PostgreSQL MVCC statement.
- `GET /v2/sync/checkpoint` — latest event cursor and actor frontiers.
  The actor list is diagnostic: it must not replace replaying operations.
- `WS /v2/realtime` — first JSON message:
  `{"type": "authenticate", "token": "<access JWT>"}`, or the same
  with `"shareId": "<Share UUID>"` and a Share ticket as the token.
  Receives `operations_changed` hints, `ping`, and `ended`.
  **REST catch-up is authoritative; WebSocket messages are hints.**

## Snapshot schema (materialized projection)

```json
{
  "properties": {"title": "Notes"},
  "versions": {"title": [10, "actor-a", 1, "operation-uuid"]},
  "pages": {
    "page-id": {
      "properties": {},
      "versions": {},
      "objects": {
        "object-id": {
          "type": "stroke.create",
          "properties": {"points": []},
          "versions": {"points": [11, "actor-a", 2, "operation-uuid"]}
        }
      }
    }
  }
}
```

Folder operations use `@library` and a `folders` mapping.

## Two-protocol barrier

- API v2 refuses to write a UID present in v1 `synced_notebooks`,
  including deleted records. Import and migration are stage 11.
- Both v1 PUT/DELETE routes reject any UID already present in v2
  `document_states`, including tombstoned documents.
- Existing v1 Share links and revocation logic are unchanged. Guest access
  to v2 is scoped and ready for the stage-6 migration; the existing Share
  flow still requires a v1 document.
- This boundary prevents a snapshot client from overwriting an operational
  document while allowing production to remain on v1 during rollout.

## Verification limits

The new pure projection/API-unit tests cover tombstones, convergence,
duplicate responses, actor binding, missing causal prerequisites and the v1
barrier. PostgreSQL transaction/crash/reconnect and real two-device tests
are still required before activating v2 for production accounts (stages
11–13). Do not treat isolated Python tests as end-to-end verification.
