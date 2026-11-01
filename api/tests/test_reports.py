import json
import uuid

import pytest
from pydantic import ValidationError

from app.routers.reports import (
    DiagnosticsPayload,
    FeedbackPayload,
    _store_diagnostics,
    _store_feedback,
)


def test_feedback_repeated_submission_does_not_duplicate(tmp_path):
    report = FeedbackPayload(system="Linux", issue="The editor stopped saving.")
    first = _store_feedback(tmp_path, report)
    second = _store_feedback(tmp_path, report)
    records = json.loads((tmp_path / "feedback" / "reports.json").read_text())
    assert first == second
    assert len(records) == 1
    assert records[first]["occurrences"] == 2
    assert records[first]["system"] == "Linux"


def test_diagnostics_overwrites_only_same_account_device(tmp_path):
    owner = uuid.uuid4()
    device = uuid.uuid4()
    body = DiagnosticsPayload(
        deviceId=device, system="Web", errors="first",
        dataIntegrity="", optimization="",
    )
    assert _store_diagnostics(tmp_path, owner, body)
    assert not _store_diagnostics(tmp_path, owner, body)
    updated = body.model_copy(update={"errors": "second"})
    assert _store_diagnostics(tmp_path, owner, updated)
    directory = tmp_path / "diagnostics" / str(owner)
    assert len(list(directory.glob("*.json"))) == 1
    stored = json.loads((directory / f"{device}.json").read_text())
    assert stored["errors"] == "second"
    assert stored["dataIntegrity"] == ""


def test_identical_logs_from_new_device_do_not_accumulate(tmp_path):
    owner = uuid.uuid4()
    first_device = uuid.uuid4()
    initial = DiagnosticsPayload(
        deviceId=first_device, system="Linux", errors="same stack",
        dataIntegrity="", optimization="",
    )
    assert _store_diagnostics(tmp_path, owner, initial)
    second_device = uuid.uuid4()
    assert _store_diagnostics(
        tmp_path, owner, initial.model_copy(update={"deviceId": second_device})
    )
    directory = tmp_path / "diagnostics" / str(owner)
    assert [file.name for file in directory.glob("*.json")] == [
        f"{second_device}.json"
    ]


def test_clearing_logs_deletes_old_snapshot(tmp_path):
    owner = uuid.uuid4()
    device = uuid.uuid4()
    initial = DiagnosticsPayload(
        deviceId=device, system="Web", errors="old",
        dataIntegrity="", optimization="",
    )
    assert _store_diagnostics(tmp_path, owner, initial)
    cleared = initial.model_copy(update={"errors": ""})
    assert _store_diagnostics(tmp_path, owner, cleared)
    directory = tmp_path / "diagnostics" / str(owner)
    assert not list(directory.glob("*.json"))


def test_feedback_and_diagnostics_validation():
    with pytest.raises(ValidationError):
        FeedbackPayload(system="Other", issue="   ")
    with pytest.raises(ValidationError):
        FeedbackPayload(system="Linux", issue="x" * 5001)
    with pytest.raises(ValidationError):
        DiagnosticsPayload(
            deviceId=uuid.uuid4(), system="Web", errors="x" * 32001,
            dataIntegrity="", optimization="",
        )


def test_server_backup_includes_reports_directory():
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "backup-server.sh"
    ).read_text()
    assert '"$staging/device-backups.tar.gz" data' in script
