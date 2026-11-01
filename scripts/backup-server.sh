#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /absolute/path/on-separate-backup-medium" >&2
  exit 64
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
requested_destination="$1"
if [[ "$requested_destination" != /* || "$requested_destination" == "/" ]]; then
  echo "Backup destination must be a specific absolute directory." >&2
  exit 64
fi
mkdir -p -- "$requested_destination"
destination="$(realpath -- "$requested_destination")"
case "$destination/" in
  "$project_root/"*)
    echo "Backup destination must be outside the server project." >&2
    exit 64
    ;;
esac

if [[ ! -d "$project_root/data/backups" ]]; then
  echo "Missing $project_root/data/backups; refusing an incomplete backup." >&2
  exit 66
fi
command -v docker >/dev/null
command -v sha256sum >/dev/null
command -v tar >/dev/null

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging="$(mktemp -d "$destination/.nanotateczki-$timestamp-XXXXXX")"
cleanup() {
  if [[ -n "${staging:-}" && -d "$staging" && \
      "$staging" == "$destination"/.nanotateczki-* ]]; then
    rm -rf -- "$staging"
  fi
}
trap cleanup EXIT INT TERM

docker compose --project-directory "$project_root" exec -T db \
  pg_dump -U nano_data -d nanotateczki --format=custom \
  > "$staging/postgres.dump"

tar --exclude='*.upload' -C "$project_root" -czf \
  "$staging/device-backups.tar.gz" data
tar -C "$project_root" -czf "$staging/deployment-metadata.tar.gz" \
  compose.yaml compose.public.yaml Caddyfile migrations

(
  cd "$staging"
  sha256sum postgres.dump device-backups.tar.gz deployment-metadata.tar.gz \
    > SHA256SUMS
  sha256sum --check SHA256SUMS
)

if [[ -n "${BACKUP_AGE_RECIPIENT:-}" ]]; then
  command -v age >/dev/null
  archive="$staging/nanotateczki-server-$timestamp.tar"
  tar -C "$staging" -cf "$archive" \
    postgres.dump device-backups.tar.gz deployment-metadata.tar.gz SHA256SUMS
  encrypted="$destination/nanotateczki-server-$timestamp.tar.age"
  if [[ -e "$encrypted" ]]; then
    echo "Backup already exists: $encrypted" >&2
    exit 73
  fi
  age --recipient "$BACKUP_AGE_RECIPIENT" \
    --output "$encrypted.partial" "$archive"
  mv -- "$encrypted.partial" "$encrypted"
  echo "Encrypted server backup created: $encrypted"
else
  final="$destination/nanotateczki-server-$timestamp"
  if [[ -e "$final" ]]; then
    echo "Backup already exists: $final" >&2
    exit 73
  fi
  mv -- "$staging" "$final"
  staging=""
  echo "Server backup created without archive encryption: $final"
  echo "Use an encrypted destination or set BACKUP_AGE_RECIPIENT." >&2
fi
