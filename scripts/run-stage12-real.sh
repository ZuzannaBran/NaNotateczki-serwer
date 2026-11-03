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
cleanup() {
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
