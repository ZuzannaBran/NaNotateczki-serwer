"""Stdlib regression tests; no Docker or production data required."""
import hashlib
import io
import pathlib
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from stage13_backup_check import BackupError, check_backup


class BackupCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.backup = self.root / "backup"
        self.backup.mkdir()
        (self.backup / "postgres.dump").write_bytes(b"PGDMP" + b"placeholder")
        self.write_archive("device-backups.tar.gz", [
            ("data/", None), ("data/backups/", None),
            ("data/backups/a.json", b"hello"),
        ])
        self.write_archive("deployment-metadata.tar.gz", [
            ("compose.yaml", b"services:\n"),
            ("compose.public.yaml", b"services:\n"),
            ("Caddyfile", b"test"),
            ("migrations/", None),
            ("migrations/0001.sql", b"SELECT 1;"),
        ])
        self.manifest()

    def write_archive(self, name, entries):
        with tarfile.open(self.backup / name, "w:gz") as tar:
            for path, data in entries:
                info = tarfile.TarInfo(path)
                if data is None:
                    info.type = tarfile.DIRTYPE
                else:
                    info.size = len(data)
                tar.addfile(info, io.BytesIO(data) if data is not None else None)

    def manifest(self):
        names = ["postgres.dump", "device-backups.tar.gz", "deployment-metadata.tar.gz"]
        (self.backup / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256((self.backup / n).read_bytes()).hexdigest()}  {n}\n"
            for n in names
        ))

    def test_valid_backup_and_asset_restore(self):
        target = self.root / "restored"
        summary = check_backup(self.backup, target)
        self.assertEqual(summary["asset_files"], 1)
        self.assertEqual((target / "data/backups/a.json").read_bytes(), b"hello")

    def test_archive_checksum_is_required(self):
        with (self.backup / "postgres.dump").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(BackupError, "SHA-256"):
            check_backup(self.backup)

    def test_traversal_is_rejected(self):
        self.write_archive("device-backups.tar.gz", [
            ("data/backups/", None), ("data/../../stolen", b"secret"),
        ])
        self.manifest()
        with self.assertRaises(BackupError):
            check_backup(self.backup, self.root / "restored")
        self.assertFalse((self.root / "stolen").exists())

    def test_symlink_is_rejected(self):
        with tarfile.open(self.backup / "device-backups.tar.gz", "w:gz") as tar:
            info = tarfile.TarInfo("data/backups")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc"
            tar.addfile(info)
        self.manifest()
        with self.assertRaisesRegex(BackupError, "Links"):
            check_backup(self.backup)

    def test_duplicate_members_are_rejected(self):
        self.write_archive("device-backups.tar.gz", [
            ("data/backups", None), ("data/backups", None),
        ])
        self.manifest()
        with self.assertRaisesRegex(BackupError, "Duplicate"):
            check_backup(self.backup)

    def test_missing_backup_directory_is_rejected(self):
        self.write_archive("device-backups.tar.gz", [("data/", None)])
        self.manifest()
        with self.assertRaisesRegex(BackupError, "data/backups"):
            check_backup(self.backup)

    def test_unexpected_metadata_is_rejected(self):
        self.write_archive("deployment-metadata.tar.gz", [
            ("compose.yaml", b"compose"),
            ("compose.public.yaml", b"compose"),
            ("Caddyfile", b"caddy"),
            (".env", b"secret"),
        ])
        self.manifest()
        with self.assertRaisesRegex(BackupError, "Unexpected"):
            check_backup(self.backup)

    def test_missing_migrations_are_rejected(self):
        self.write_archive("deployment-metadata.tar.gz", [
            ("compose.yaml", b"compose"),
            ("compose.public.yaml", b"compose"),
            ("Caddyfile", b"caddy"),
        ])
        self.manifest()
        with self.assertRaisesRegex(BackupError, "migration"):
            check_backup(self.backup)

    def test_reject_duplicate_manifest(self):
        checksum = (self.backup / "SHA256SUMS").read_text().splitlines()
        (self.backup / "SHA256SUMS").write_text("\n".join([checksum[0]] * 3) + "\n")
        with self.assertRaisesRegex(BackupError, "duplicate"):
            check_backup(self.backup)


if __name__ == "__main__":
    unittest.main()
