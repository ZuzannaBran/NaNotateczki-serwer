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
