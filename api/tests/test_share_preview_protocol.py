"""Bounded, non-durable drawing previews on authenticated v2 channels."""

from app.routers.operation_realtime import validate_preview


def test_share_preview_has_no_operation_or_untrusted_keys():
    message = {
        "type": "share_preview",
        "shareId": "11111111-1111-4111-8111-111111111111",
        "documentUid": "doc-1", "pageId": "page-1",
        "senderId": "22222222-2222-4222-8222-222222222222",
        "operationId": "forged", "cursor": 123,
        "stroke": {
            "tool": 0, "color": 0xff123456, "width": 3.0,
            "points": [[1.5, 2.0, 0.5], [5, 8.2, 1]],
        },
    }
    preview = validate_preview(message)
    assert preview is not None
    assert preview["stroke"]["points"] == message["stroke"]["points"]
    assert "operationId" not in preview
    assert "cursor" not in preview


def test_preview_clear_and_malformed_data():
    valid = {
        "type": "share_preview",
        "shareId": "11111111-1111-4111-8111-111111111111",
        "documentUid": "doc-1", "pageId": "page-1",
        "senderId": "22222222-2222-4222-8222-222222222222",
    }
    assert validate_preview(valid)["stroke"] is None
    bad = {**valid, "stroke": {
        "tool": 0, "color": 1, "width": 1,
        "points": [[float("inf"), 0, 1]],
    }}
    assert validate_preview(bad) is None
    assert validate_preview({**valid, "senderId": "invalid"}) is None
    assert validate_preview({**valid, "stroke": {
        "tool": 0, "color": 1, "width": 1,
        "points": [[0, 0, 0]] * 97,
    }}) is None
