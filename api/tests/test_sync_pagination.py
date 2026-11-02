"""Regression tests for bounded account bootstrap and change pages."""

from types import SimpleNamespace

import pytest

from app.routers import sync


def notebook(uid: str, content: str = "Small"):
    return SimpleNamespace(
        uid=uid, revision=1, deleted_at=None,
        content={"uid": uid, "title": content},
    )


def event(index: int, content: str = "Small"):
    return SimpleNamespace(
        id=index, entity_uid=f"uid-{index}", revision=index,
        deleted=False, content={"title": content},
    )


def test_bootstrap_bounds_results_even_with_many_documents():
    records = [notebook(f"uid-{index:03}") for index in range(18)]
    page, more = sync.bounded_page(records[:sync.SYNC_PAGE_LIMIT + 1], sync.record_json)
    assert more
    assert len(page) == sync.SYNC_PAGE_LIMIT
    assert page[-1]["uid"] == "uid-004"


def test_change_feed_preserves_event_order_and_cursor():
    records = [event(index) for index in range(1, 12)]
    cursor = 0
    processed = []
    while True:
        batch = [item for item in records if item.id > cursor][
            :sync.SYNC_PAGE_LIMIT + 1
        ]
        page, more = sync.bounded_page(batch, sync.event_json)
        processed.extend(item["revision"] for item in page)
        cursor = batch[len(page) - 1].id if page else cursor
        if not more:
            break
    assert processed == list(range(1, 12))
    assert cursor == 11


def test_large_item_is_not_skipped_when_exceeding_soft_page_budget(monkeypatch):
    monkeypatch.setattr(sync, "SYNC_PAGE_BYTES", 128)
    records = [notebook("a", "x" * 1000), notebook("b", "Second")]
    page, more = sync.bounded_page(records, sync.record_json)
    assert len(page) == 1
    assert page[0]["uid"] == "a"
    assert more
    page, more = sync.bounded_page(records[1:], sync.record_json)
    assert page[0]["uid"] == "b"
    assert not more


def test_byte_bound_avoids_oversized_multi_item_responses(monkeypatch):
    monkeypatch.setattr(sync, "SYNC_PAGE_BYTES", 500)
    records = [event(index, "x" * 180) for index in range(8)]
    page, more = sync.bounded_page(records, sync.event_json)
    assert len(page) < len(records)
    assert more
    assert page[0]["uid"] == "uid-0"
