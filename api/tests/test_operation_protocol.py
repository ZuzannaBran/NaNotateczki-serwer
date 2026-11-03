"""Protocol v1 parity checks independent of database and HTTP."""

import pytest

from app.operation_protocol import InvalidOperation, KINDS, OperationEnvelope

FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


def sample(**overrides):
    raw = {
        "schemaVersion": 1,
        "operationId": FIRST,
        "actorId": "device-1",
        "actorSeq": 1,
        "documentId": "document-1",
        "pageId": "page-1",
        "objectId": "stroke-1",
        "type": "stroke.create",
        "payload": {"value": {"points": [1, 2]}},
        "dependencies": [],
        "logicalClock": 7,
    }
    raw.update(overrides)
    return raw


def test_roundtrip_and_immutable_payload_copy():
    raw = sample()
    operation = OperationEnvelope.from_dict(raw)
    raw["payload"]["value"]["points"].clear()
    assert operation.to_dict()["payload"] == {"value": {"points": [1, 2]}}
    assert OperationEnvelope.from_dict(operation.to_dict()) == operation
    with pytest.raises(TypeError):
        operation.payload["value"]["points"][0] = 123


def test_every_operation_kind_has_a_target():
    for kind, target in KINDS.items():
        fields = {"type": kind}
        if target == "document":
            fields.update(objectId=None, pageId=None)
        elif target == "page":
            fields.update(objectId="page-1", pageId=None)
        elif target == "folder":
            fields.update(documentId="@library", objectId="folder-1", pageId=None)
        assert (
            OperationEnvelope.from_dict(sample(**fields)).kind == kind
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"schemaVersion": 2},
        {"type": "document.replace"},
        {"dependencies": [FIRST]},
        {"dependencies": [SECOND, SECOND]},
        {"actorSeq": 0},
        {"actorSeq": True},
        {"actorSeq": 9007199254740992},
        {"logicalClock": 9007199254740992},
        {"logicalClock": -1},
        {"pageId": None},
        {"type": "document.delete", "objectId": "", "pageId": None},
        {"type": "page.create", "objectId": "page-1", "pageId": ""},
        {"operationId": "not-a-uuid"},
        {"payload": {"bad": float("nan")}},
        {"actorId": "actor\nspoof"},
        {"documentId": "doc\u0000bad"},
        {"pageId": "p" * 129},
        {"objectId": "x\u007f"},
        {"revertsOperationId": SECOND},
    ],
)
def test_rejects_malformed_or_unsafe_operations(changes):
    with pytest.raises(InvalidOperation):
        OperationEnvelope.from_dict(sample(**changes))


def test_undo_is_new_operation_not_rewritten_history():
    undo = sample(
        operationId=SECOND,
        actorSeq=2,
        type="object.delete",
        payload={},
        dependencies=[FIRST],
        logicalClock=8,
        revertsOperationId=FIRST,
    )
    assert OperationEnvelope.from_dict(undo).reverts_operation_id == FIRST
