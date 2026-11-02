#!/usr/bin/env python3
"""Check the local HTTP API against PostgreSQL without leaving test records."""

import base64
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


base_url = "http://127.0.0.1:18080"
project_root = Path(__file__).resolve().parents[1]


def call(method: str, path: str, body=None, token: str | None = None,
         idempotency_key: str | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json", "X-Request-ID": str(uuid.uuid4())}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read()
        return response.status, json.loads(payload) if payload else {}



def call_raw(method: str, path: str, body: bytes | None = None,
             *, bearer: str | None = None,
             mime: str | None = None) -> tuple[int, bytes]:
    """Exchange raw PNG bodies and inspect guest ticket authorization."""
    headers = {"X-Request-ID": str(uuid.uuid4())}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    if mime is not None:
        headers["Content-Type"] = mime
    request = Request(
        base_url + path, data=body, headers=headers, method=method,
    )
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


def expect(status: int, expected: int, label: str) -> None:
    if status != expected:
        raise AssertionError(f"{label}: expected HTTP {expected}, got {status}")


def main() -> None:
    username = f"smoke_{uuid.uuid4().hex[:25]}"
    password = "Test1234"
    new_password = "Next1234"
    user_id = None
    backup_id = None
    try:
        status, registered = call(
            "POST", "/v1/auth/register", {"username": username, "password": password}
        )
        if status != 201:
            raise AssertionError(f"register: HTTP {status}, {registered}")
        user_id = str(uuid.UUID(registered["userId"]))
        token = registered["accessToken"]
        refresh_token = registered["refreshToken"]

        status, profile = call("GET", "/v1/auth/me", token=token)
        expect(status, 200, "profile")
        assert profile["userId"] == user_id

        status, bootstrap = call("GET", "/v1/sync/bootstrap", token=token)
        expect(status, 200, "sync bootstrap")
        assert bootstrap == {"cursor": 0, "notebooks": []}

        uid = str(uuid.uuid4())
        notebook = {
            "uid": uid,
            "title": "HTTP smoke test",
            "kind": 0,
            "folder": "",
            "createdAt": "2026-11-01T00:00:00.000Z",
            "updatedAt": "2026-11-01T00:00:00.000Z",
            "pages": [{
                "id": "page-1",
                "title": "Page",
                "isBookmarked": False,
                "inkStrokes": [],
                "textBlocks": [{"id": "text-1", "text": "original"}],
                "imageBlocks": [],
            }],
        }
        status, saved = call(
            "PUT",
            f"/v1/sync/notebooks/{uid}",
            {"expectedRevision": 0, "notebook": notebook},
            token=token,
            idempotency_key=str(uuid.uuid4()),
        )
        expect(status, 200, "sync write")
        assert saved["revision"] == 1

        status, changes = call(
            "GET", "/v1/sync/changes?after=0", token=token
        )
        expect(status, 200, "sync changes")
        assert changes["changes"][0]["uid"] == uid

        # Real HTTP + PostgreSQL lifecycle for password-protected sharing.
        status, missing_share = call(
            "POST", "/v1/study-shares",
            {"documentUid": "not-synced", "title": "Missing",
             "password": "x"},
            token=token,
        )
        expect(status, 409, "reject sharing an unsynced document")

        status, share = call(
            "POST", "/v1/study-shares",
            {"documentUid": uid, "title": "Live study test",
             "password": "x"},
            token=token,
        )
        expect(status, 201, "create study share")
        share_id = str(uuid.UUID(share["id"]))
        share_path = f"/v1/study-shares/{share_id}"

        expect(call(
            "POST", share_path + "/join", {"password": "wrong-password"}
        )[0], 401, "incorrect guest password")
        status, guest = call(
            "POST", share_path + "/join",
            {"password": "x"},
        )
        expect(status, 200, "guest joins without account")
        ticket = guest["ticket"]

        # Independent guest strokes from a shared base must reach the owner.
        status, second_guest = call(
            "POST", share_path + "/join", {"password": "x"}
        )
        expect(status, 200, "second guest joins")
        status, before_edit = call(
            "GET", share_path + "/document", token=ticket
        )
        expect(status, 200, "guest reads original document")
        assert before_edit["revision"] == 1
        assert before_edit["notebook"]["uid"] == uid
        for guest_ticket, stroke_id in [
            (ticket, "guest-a"),
            (second_guest["ticket"], "guest-b"),
        ]:
            candidate = json.loads(json.dumps(before_edit["notebook"]))
            candidate["pages"][0]["inkStrokes"].append({
                "id": stroke_id, "points": [{"x": 2, "y": 4}]
            })
            status, edited = call(
                "PUT", share_path + "/document", {
                    "baseRevision": before_edit["revision"],
                    "baseNotebook": before_edit["notebook"],
                    "notebook": candidate,
                }, token=guest_ticket,
            )
            expect(status, 200, f"guest merges {stroke_id}")
        assert edited["revision"] == 3
        strokes = {
            stroke["id"]
            for stroke in edited["notebook"]["pages"][0]["inkStrokes"]
        }
        assert strokes == {"guest-a", "guest-b"}, "Guest strokes were lost"

        status, owner_state = call(
            "GET", "/v1/sync/bootstrap", token=token
        )
        expect(status, 200, "owner reads merged guest changes")
        originals = [
            entry for entry in owner_state["notebooks"]
            if entry["uid"] == uid and not entry["deleted"]
        ]
        assert len(owner_state["notebooks"]) == 1
        assert len(originals) == 1 and originals[0]["revision"] == 3
        assert originals[0]["notebook"]["folder"] == notebook["folder"]
        status, owner_changes = call(
            "GET", "/v1/sync/changes?after=0", token=token
        )
        expect(status, 200, "owner change feed receives guest edits")
        assert any(
            record["uid"] == uid and record["revision"] == 3
            for record in owner_changes["changes"]
        ), "Guest edits missing from owner change feed"

        expect(call_raw("GET", share_path + "/frame")[0],
               401, "frame without ticket")
        expect(call_raw(
            "GET", share_path + "/frame", bearer=ticket
        )[0], 404, "frame before host upload")
        expect(call(
            "POST", share_path + "/extend", {}, token=token
        )[0], 409, "extension too early")

        # A complete, minimal 1x1 PNG instead of a synthetic header.
        frame = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
        )
        expect(call_raw(
            "PUT", share_path + "/frame", frame,
            bearer=token, mime="image/png",
        )[0], 204, "host uploads PNG")
        status, received = call_raw(
            "GET", share_path + "/frame", bearer=ticket,
        )
        expect(status, 200, "guest reads PNG")
        assert received == frame, "PNG changed during upload/download"

        expect(call_raw(
            "DELETE", share_path, b"", bearer=token,
        )[0], 204, "owner ends study share")
        expect(call_raw(
            "GET", share_path + "/frame", bearer=ticket,
        )[0], 410, "revoked guest can no longer read")
        expect(call(
            "GET", share_path + "/document", token=second_guest["ticket"]
        )[0], 410, "revoked guest cannot read original")
        expect(call(
            "POST", share_path + "/join",
            {"password": "x"},
        )[0], 410, "ended session cannot be rejoined")
        status, after_end = call("GET", "/v1/sync/bootstrap", token=token)
        expect(status, 200, "owner notebook remains after sharing")
        assert any(
            entry.get("uid") == uid for entry in after_end["notebooks"]
        ), "Ending sharing removed the owner's original notebook"

        device_id = str(uuid.uuid4())
        backup_payload = {
            "notebooks": [], "libraryState": {}, "studyPlannerState": {}
        }
        payload_bytes = json.dumps(
            backup_payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        envelope = {
            "format": "nanotateczki-server-backup",
            "version": 2,
            "deviceId": device_id,
            "checksumAlgorithm": "sha256",
            "checksum": hashlib.sha256(payload_bytes).hexdigest(),
            "payload": backup_payload,
        }
        backup_bytes = json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":")
        ).encode()
        backup_sha256 = hashlib.sha256(backup_bytes).hexdigest()
        status, pending = call(
            "POST", "/v1/backups",
            {
                "deviceId": device_id,
                "sha256": backup_sha256,
                "sizeBytes": len(backup_bytes),
                "formatVersion": 2,
            },
            token=token,
        )
        if status != 201:
            raise AssertionError(f"backup create: HTTP {status}, {pending}")
        backup_id = str(uuid.UUID(pending["backupId"]))
        upload = Request(
            base_url + f"/v1/backups/{backup_id}/content",
            data=backup_bytes,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
                "X-Content-SHA256": backup_sha256,
            },
            method="PUT",
        )
        with urlopen(upload, timeout=10) as response:
            expect(response.status, 204, "backup upload")
        status, verified = call(
            "POST", f"/v1/backups/{backup_id}/finalize", {}, token=token
        )
        expect(status, 200, "backup finalize")
        assert verified["status"] == "verified"

        status, _ = call(
            "POST",
            "/v1/auth/change-password",
            {"currentPassword": password, "newPassword": new_password},
            token=token,
        )
        expect(status, 204, "change password")
        expect(call("GET", "/v1/auth/me", token=token)[0], 401,
               "old access token")
        expect(call("POST", "/v1/auth/refresh",
                    {"refreshToken": refresh_token})[0], 401,
               "old refresh token")
        expect(call("POST", "/v1/auth/login",
                    {"username": username, "password": new_password})[0], 200,
               "login with new password")
        print(
            "HTTP smoke OK: account, sync, study share, backup, password change, "
            "token revocation"
        )
    finally:
        if user_id is not None:
            subprocess.run(
                [
                    "docker", "compose", "exec", "-T", "db", "psql", "-X",
                    "-v", "ON_ERROR_STOP=1", "-U", "nano_data", "-d",
                    "nanotateczki", "-c",
                    f"DELETE FROM users WHERE id = '{user_id}'",
                ],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            )
            if backup_id is not None:
                owner_dir = project_root / "data" / "backups" / user_id
                (owner_dir / f"{backup_id}.json").unlink(missing_ok=True)
                (owner_dir / f"{backup_id}.upload").unlink(missing_ok=True)
                if owner_dir.exists():
                    owner_dir.rmdir()


if __name__ == "__main__":
    main()
