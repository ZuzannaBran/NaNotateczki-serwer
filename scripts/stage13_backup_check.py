#!/usr/bin/env python3
"""Validate a Stage-13 backup and optionally restore its data archive safely."""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

EXPECTED = frozenset({
    "postgres.dump", "device-backups.tar.gz", "deployment-metadata.tar.gz",
})
CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)\Z")


class BackupError(Exception):
    """The backup is incomplete, corrupted, or contains unsafe paths."""


def validate_manifest(directory: Path) -> None:
    manifest = directory / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise BackupError("SHA256SUMS is missing or is a symlink")
    checksum_lines = manifest.read_text(encoding="ascii").splitlines()
    if len(checksum_lines) != len(EXPECTED):
        raise BackupError("SHA256SUMS must list exactly three archives")
    seen: set[str] = set()
    for line in checksum_lines:
        match = CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise BackupError("Malformed SHA256SUMS entry")
        digest, filename = match.groups()
        if filename not in EXPECTED or filename in seen:
            raise BackupError("Unexpected or duplicate SHA256SUMS filename")
        seen.add(filename)
        file = directory / filename
        if not file.is_file() or file.is_symlink():
            raise BackupError(f"Missing or unsafe backup file: {filename}")
        computed = hashlib.sha256()
        with file.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                computed.update(block)
        if computed.hexdigest() != digest:
            raise BackupError(f"SHA-256 mismatch for {filename}")
    if seen != EXPECTED:
        raise BackupError("Incomplete manifest")
    with (directory / "postgres.dump").open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise BackupError("PostgreSQL backup is not a custom-format dump")


def safe_name(name: str, expected_root: str) -> tuple[str, ...]:
    if not name or name.startswith("/") or "\\" in name or "\\x00" in name:
        raise BackupError("Unsafe archive path")
    normalized = name.rstrip("/")
    pieces = normalized.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces):
        raise BackupError("Archive path traversal or malformed path")
    if pieces[0] != expected_root:
        raise BackupError(f"Archive contains files outside {expected_root}/")
    if PurePosixPath(normalized).is_absolute():
        raise BackupError("Absolute archive entry")
    return tuple(pieces)


def verify_archive(
    archive_path: Path, root_name: str, extraction: Path | None = None,
) -> tuple[int, int]:
    if extraction is not None:
        if extraction.is_symlink() or extraction.exists():
            raise BackupError("Asset restore directory must not exist")
        extraction.mkdir(parents=True)
    seen: set[str] = set()
    count = 0
    total_bytes = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            pieces = safe_name(member.name, root_name)
            key = "/".join(pieces)
            if key in seen:
                raise BackupError(f"Duplicate archive member: {key}")
            seen.add(key)
            if not (member.isdir() or member.isfile()):
                raise BackupError("Links and special archive members are forbidden")
            target = extraction.joinpath(*pieces) if extraction else None
            if member.isdir():
                if target is not None:
                    target.mkdir(parents=True, exist_ok=True)
                continue
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise BackupError("Archive file has no content stream")
            with source:
                if target is not None:
                    with target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                else:
                    while source.read(1024 * 1024):
                        pass
            count += 1
            total_bytes += member.size
    if root_name == "data" and "data/backups" not in seen:
        raise BackupError("Device backup archive lacks data/backups")
    return count, total_bytes


def check_backup(directory: Path, asset_restore: Path | None = None) -> dict:
    if not directory.is_dir() or directory.is_symlink():
        raise BackupError("Backup must be a real directory")
    validate_manifest(directory)
    files, size = verify_archive(
        directory / "device-backups.tar.gz", "data", asset_restore
    )
    # Metadata tar contains several top-level paths; verify it separately.
    with tarfile.open(directory / "deployment-metadata.tar.gz", "r:gz") as tar:
        seen = set()
        for member in tar:
            top = safe_name(member.name, member.name.split("/")[0])[0]
            if top not in {"compose.yaml", "compose.public.yaml", "Caddyfile", "migrations"}:
                raise BackupError("Unexpected deployment metadata archive path")
            if not (member.isdir() or member.isfile()):
                raise BackupError("Unsafe metadata archive member")
            if member.name in seen:
                raise BackupError("Duplicate metadata archive member")
            seen.add(member.name)
            if member.isfile():
                stream = tar.extractfile(member)
                if stream is None:
                    raise BackupError("Missing metadata member payload")
                with stream:
                    while stream.read(1024 * 1024):
                        pass
        if not {"compose.yaml", "compose.public.yaml", "Caddyfile"}.issubset(seen):
            raise BackupError("Missing deployment configuration")
    return {"asset_files": files, "asset_bytes": size}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_dir", type=Path)
    parser.add_argument("--restore-assets", type=Path)
    args = parser.parse_args()
    try:
        summary = check_backup(args.backup_dir, args.restore_assets)
    except (BackupError, OSError, UnicodeError, tarfile.TarError) as exc:
        print(f"BACKUP INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"BACKUP VERIFIED: {summary['asset_files']} files, "
          f"{summary['asset_bytes']} uncompressed bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
