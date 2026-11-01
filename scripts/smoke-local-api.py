#!/usr/bin/env python3
"""Check the local HTTP API against PostgreSQL without leaving test records."""

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
            "pages": [],
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
            "HTTP smoke OK: account, sync, backup, password change, "
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
