#!/usr/bin/env bash
# Isolated Stage-12 backend E2E: FastAPI + real PostgreSQL + WebSocket.
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

for command in docker python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command unavailable: $command" >&2
    exit 2
  fi
done

project="nnstage12-$$"
work="$(mktemp -d)"
compose=(docker compose -p "$project" -f compose.stage12.yaml)
uvicorn_pid=""
cleanup() {
  if [[ -n "$uvicorn_pid" ]]; then
    kill "$uvicorn_pid" >/dev/null 2>&1 || true
    wait "$uvicorn_pid" >/dev/null 2>&1 || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# No Docker mount or named volume points to /srv/nanotateczki.
"${compose[@]}" up -d --wait db
"${compose[@]}" exec -T db psql \
  -U stage12_admin -d nanotateczki_stage12_test \
  -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE nano_app LOGIN PASSWORD 'stage12-local-only';
GRANT CONNECT ON DATABASE nanotateczki_stage12_test TO nano_app;
GRANT USAGE ON SCHEMA public TO nano_app;
SQL

for migration in migrations/*.sql; do
  echo "Applying isolated test migration: $(basename "$migration")"
  "${compose[@]}" exec -T db psql -q \
    -U stage12_admin -d nanotateczki_stage12_test \
    -v ON_ERROR_STOP=1 < "$migration"
done

mapped="$("${compose[@]}" port db 5432)"
port="${mapped##*:}"
if ! [[ "$port" =~ ^[0-9]+$ ]]; then
  echo "Cannot determine isolated PostgreSQL port: $mapped" >&2
  exit 2
fi

python3 -m venv "$work/venv"
"$work/venv/bin/python" -m pip install -q -r api/requirements.txt
export DATABASE_URL="postgresql+asyncpg://nano_app:stage12-local-only@127.0.0.1:$port/nanotateczki_stage12_test"
export APP_ORIGIN="https://app.example.test"
export JWT_SECRET="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
export ALLOW_REGISTRATION="false"
export DATA_PATH="$work/data"
export NN_STAGE12_REAL_DB="1"
# Stage-12 clients use an isolated disposable database, never production.
export OPERATIONS_V2_WRITE_MODE="all"

case "${1:-}" in
  "")
    selection=()
    ;;
  --quick)
    selection=(-k 'not test_1000_inserts_1000_deletes_and_idempotent_retries')
    ;;
  *)
    echo "Usage: bash scripts/run-stage12-real.sh [--quick]" >&2
    exit 2
    ;;
esac

(cd api && "$work/venv/bin/python" -m pytest -q \
  tests/test_stage12_real_stack.py "${selection[@]}")

# Optional full Flutter -> FastAPI -> PostgreSQL -> WebSocket test.
if [[ -n "${NN_STAGE12_FLUTTER_ROOT:-}" ]]; then
  if ! command -v flutter >/dev/null 2>&1; then
    echo "Flutter SDK required for optional client E2E" >&2
    exit 2
  fi
  flutter_root="$(cd "$NN_STAGE12_FLUTTER_ROOT" && pwd)"
  if [[ ! -f "$flutter_root/pubspec.yaml" ||
        ! -f "$flutter_root/integration_test/stage12_live_api_test.dart" ]]; then
    echo "Invalid Flutter repository: $flutter_root" >&2
    exit 2
  fi

  http_port="$("$work/venv/bin/python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
  export ALLOW_REGISTRATION=true
  (cd api && exec "$work/venv/bin/python" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$http_port") >"$work/uvicorn.log" 2>&1 &
  uvicorn_pid="$!"
  ready=0
  for _ in $(seq 1 50); do
    if "$work/venv/bin/python" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$http_port/ready', timeout=1)" >/dev/null 2>&1; then
      ready=1
      break
    fi
    if ! kill -0 "$uvicorn_pid" 2>/dev/null; then
      cat "$work/uvicorn.log" >&2
      exit 1
    fi
    sleep 0.2
  done
  if [[ "$ready" != 1 ]]; then
    cat "$work/uvicorn.log" >&2
    echo "Stage 12 local API did not become ready" >&2
    exit 1
  fi
  # The Dart E2E test uses a freshly generated owner in this disposable DB.
  # This avoids introducing any backdoor/auto-activation in production auth.
  test_owner="$("$work/venv/bin/python" -c 'import uuid; print(uuid.uuid4())')"
  "${compose[@]}" exec -T db psql -q -v ON_ERROR_STOP=1 \
    -v owner_id="$test_owner" -U stage12_admin \
    -d nanotateczki_stage12_test <<'SQL'
BEGIN;
INSERT INTO users (id, username, password_hash, auth_version)
VALUES (:'owner_id'::uuid,
        'stage12_flutter_' || replace(:'owner_id', '-', ''),
        'not-a-login-password', 1);
INSERT INTO account_sync_protocol (owner_id, protocol)
VALUES (:'owner_id'::uuid, 'v2');
COMMIT;
SQL
  test_access_token="$(
    cd api && STAGE12_TEST_OWNER="$test_owner" "$work/venv/bin/python" - <<'PY'
import os
import uuid
from app.security import create_access_token

token, _ = create_access_token(uuid.UUID(os.environ["STAGE12_TEST_OWNER"]), 1)
print(token)
PY
  )"
  flutter_device="${NN_STAGE12_DEVICE_ID:-linux}"
  (cd "$flutter_root" && flutter pub get && flutter test -d "$flutter_device" \
    --dart-define="NN_STAGE12_API_URL=http://127.0.0.1:$http_port" \
    --dart-define="NN_STAGE12_ACCESS_TOKEN=$test_access_token" \
    integration_test/stage12_live_api_test.dart)
fi
