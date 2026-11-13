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
         share_id: str | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json", "X-Request-ID": str(uuid.uuid4())}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if share_id is not None:
        headers["X-Share-Id"] = share_id
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
             mime: str | None = None,
             checksum: str | None = None) -> tuple[int, bytes]:
    """Exchange raw verified backup bytes."""
    headers = {"X-Request-ID": str(uuid.uuid4())}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    if mime is not None:
        headers["Content-Type"] = mime
    if checksum is not None:
        headers["X-Content-SHA256"] = checksum
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

        expect(call("GET", "/v2/activation/readiness", token=token)[0], 200,
               "fresh account v2 readiness")
        expect(call("GET", "/v1/sync/bootstrap", token=token)[0], 404,
               "retired snapshot route")

        uid, page, actor = (uuid.uuid4().hex for _ in range(3))
        previous = None
        def operation(kind, seq, value, **target):
            nonlocal previous
            result = {
                "schemaVersion": 1, "operationId": str(uuid.uuid4()),
                "documentId": uid, "actorId": actor, "actorSeq": seq,
                "logicalClock": seq, "dependencies": [previous] if previous else [],
                "type": kind, "payload": {"value": value}, **target,
            }
            previous = result["operationId"]
            return result
        seed = [
            operation("document.create", 1, {"title": "Smoke", "kindIndex": 0}),
            operation("page.create", 2, {"title": "Page"}, objectId=page),
        ]
        expect(call("POST", "/v2/operations/batch", {"operations": seed}, token)[0],
               200, "initial operational notebook")
        status, share = call("POST", "/v2/study-shares", {
            "documentUid": uid, "title": "Smoke", "password": "x",
        }, token)
        expect(status, 201, "operational invitation")
        share_id = str(uuid.UUID(share["id"]))
        share_path = f"/v2/study-shares/{share_id}"
        status, joined = call("POST", share_path + "/join", {"password": "x"})
        expect(status, 200, "guest joins")
        assert joined["protocol"] == "v2" and joined["documentUid"] == uid
        guest = joined["ticket"]
        appearance = {"darkMode": True, "backgroundStyle": 1, "backgroundSpacing": 32}
        expect(call("PUT", share_path + "/appearance", appearance, token)[0],
               200, "host appearance")
        assert call("GET", share_path + "/appearance", token=guest)[1]["appearance"] == appearance
        guest_edit = {
            "schemaVersion": 1, "operationId": str(uuid.uuid4()),
            "documentId": uid, "actorId": "guest-" + uuid.uuid4().hex,
            "actorSeq": 1, "logicalClock": 3, "dependencies": [previous],
            "type": "stroke.create", "pageId": page, "objectId": "guest-ink",
            "payload": {"value": {"x": 4}},
        }
        expect(call("POST", "/v2/operations/batch",
                    {"operations": [guest_edit]}, guest, share_id=share_id)[0],
               200, "guest operation")
        status, feed = call("GET", "/v2/operations?after=0", token=token)
        expect(status, 200, "owner feed")
        assert feed["operations"][-1]["operation"]["operationId"] == guest_edit["operationId"]
        expect(call("DELETE", share_path, token=token)[0], 204, "revoke invitation")
        expect(call("GET", "/v2/operations", token=guest, share_id=share_id)[0],
               410, "revoke guest journal access")

        device_id = str(uuid.uuid4())
        payload = {
            "notebooks": [], "libraryState": {"folders": []},
            "studyPlannerState": {"sessions": []},
            "operationHistory": feed["operations"], "assetBlobs": {},
        }
        compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        content = compact({
            "format": "nanotateczki-server-backup", "version": 3,
            "protocol": "operations-v2", "deviceId": device_id,
            "accountId": user_id, "schemaVersion": 8,
            "operationCheckpoint": {f"account:{user_id}": feed["cursor"]},
            "checksumAlgorithm": "sha256",
            "checksum": hashlib.sha256(compact(payload)).hexdigest(),
            "createdAt": "2026-11-13T11:00:00Z", "payload": payload,
        })
        digest = hashlib.sha256(content).hexdigest()
        status, backup = call("POST", "/v2/backups", {
            "deviceId": device_id, "formatVersion": 3,
            "sha256": digest, "sizeBytes": len(content),
        }, token)
        expect(status, 201, "create backup")
        backup_id = str(uuid.UUID(backup["backupId"]))
        backup_path = f"/v2/backups/{backup_id}"
        uploaded, _ = call_raw("PUT", backup_path + "/content", content,
                               bearer=token, mime="application/octet-stream",
                               checksum=digest)
        expect(uploaded, 204, "upload operational backup")
        status, receipt = call("POST", backup_path + "/finalize", {}, token)
        expect(status, 200, "verify backup")
        assert receipt["status"] == "verified" and receipt["sha256"] == digest
        assert call_raw("GET", backup_path + "/download", bearer=token) == (200, content)

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
                # Backup files are owned by the API container UID, not by
                # the host CI runner. Clean them as the same unprivileged UID.
                subprocess.run(
                    [
                        "docker", "compose", "exec", "-T", "api", "python", "-c",
                        (
                            "from pathlib import Path\n"
                            "import sys\n"
                            "owner_dir = Path('/data/backups') / sys.argv[1]\n"
                            "for suffix in ('.json', '.upload'):\n"
                            "    (owner_dir / (sys.argv[2] + suffix)).unlink("
                            "missing_ok=True)\n"
                            "if owner_dir.is_dir() and not any(owner_dir.iterdir()):\n"
                            "    owner_dir.rmdir()\n"
                        ),
                        user_id, backup_id,
                    ],
                    cwd=project_root,
                    check=True,
                    capture_output=True,
                    text=True,
                )


if __name__ == "__main__":
    main()
