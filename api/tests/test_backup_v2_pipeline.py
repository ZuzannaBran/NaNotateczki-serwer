"""Crash-safe v2 backup upload/finalize regression tests (no production DB)."""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import backups
from test_backup_v2 import fixture, sign


class FakeSession:
    def __init__(self, record, *, operations=None):
        self.record = record
        self.operations = operations or []
        self.commits = 0

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.record)

    async def stream_scalars(self, statement):
        async def rows():
            for cursor, operation in self.operations:
                yield SimpleNamespace(cursor=cursor, envelope=operation)
        return rows()

    async def commit(self):
        self.commits += 1


class UploadRequest:
    def __init__(self, chunks):
        self.chunks = chunks

    async def stream(self):
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def bypass_v2_protocol(monkeypatch):
    async def confirmed_protocol(session, user_id, *, v2):
        assert v2

    monkeypatch.setattr(backups, "_require_backup_protocol", confirmed_protocol)


def test_interrupted_upload_leaves_no_final_or_partial_file(
    tmp_path, monkeypatch, bypass_v2_protocol,
):
    monkeypatch.setattr(backups, "backup_root", lambda: tmp_path)
    record, envelope = fixture(cursor=5)
    sign(envelope)
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    record.sha256 = hashlib.sha256(raw).hexdigest()
    record.size_bytes = len(raw)
    session = FakeSession(record)
    user = SimpleNamespace(id=record.owner_id)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(backups._upload_content(
            record.id, UploadRequest([raw[:10]]), record.sha256,
            user, session, v2=True,
        ))
    assert raised.value.status_code == 422
    target = tmp_path / record.storage_key
    assert not target.exists()
    assert not target.with_suffix(".upload").exists()
    assert record.status == "pending"


def test_corrupt_v2_finalize_rejects_file_without_publishing_it(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(backups, "backup_root", lambda: tmp_path)
    record, envelope = fixture(cursor=5)
    sign(envelope)
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    record.sha256 = hashlib.sha256(raw).hexdigest()
    record.size_bytes = len(raw)
    target = tmp_path / record.storage_key
    target.parent.mkdir(parents=True)
    temporary = target.with_suffix(".upload")
    temporary.write_bytes(raw.replace(b"hello", b"BAD!!"))
    session = FakeSession(record, operations=[(5, envelope["payload"]["operationHistory"][0]["operation"])])
    user = SimpleNamespace(id=record.owner_id)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(backups._finalize_backup(record.id, user, session, v2=True))
    assert raised.value.status_code == 422
    assert record.status == "rejected"
    assert session.commits == 1
    assert not temporary.exists()
    assert not target.exists()


def test_verified_archive_is_published_atomically(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(backups, "backup_root", lambda: tmp_path)
    record, envelope = fixture(cursor=5)
    sign(envelope)
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    record.sha256 = hashlib.sha256(raw).hexdigest()
    record.size_bytes = len(raw)
    target = tmp_path / record.storage_key
    target.parent.mkdir(parents=True)
    target.with_suffix(".upload").write_bytes(raw)
    operation = envelope["payload"]["operationHistory"][0]["operation"]
    session = FakeSession(record, operations=[(5, operation)])
    user = SimpleNamespace(id=record.owner_id)

    result = asyncio.run(
        backups._finalize_backup(record.id, user, session, v2=True)
    )
    assert result["status"] == "verified"
    assert result["sha256"] == record.sha256
    assert target.read_bytes() == raw
    assert not target.with_suffix(".upload").exists()
    assert record.status == "verified"
