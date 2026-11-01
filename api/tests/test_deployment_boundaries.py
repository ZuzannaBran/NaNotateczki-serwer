from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_compose_does_not_publish_database_or_api_ports() -> None:
    compose = (ROOT / "compose.yaml").read_text()

    assert "5432:5432" not in compose
    assert "8000:8000" not in compose
    assert "127.0.0.1:8080:8080" in compose
    assert "read_only: true" in compose
    assert "cap_drop: [ALL]" in compose


def test_caddy_can_start_with_restricted_capabilities() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    caddy = compose.split("\n  caddy:\n", 1)[1].split("\nvolumes:\n", 1)[0]

    assert "cap_drop: [ALL]" in caddy
    assert "cap_add: [NET_BIND_SERVICE]" in caddy
    assert "security_opt: [no-new-privileges:true]" in caddy


def test_schema_enforces_cross_owner_device_integrity() -> None:
    migration = (ROOT / "migrations" / "0001_initial.sql").read_text()

    assert "FOREIGN KEY (owner_id, device_id)" in migration
    assert "REFERENCES devices(owner_id, id)" in migration
    assert "UNIQUE (owner_id, uid)" in migration
    assert "CHECK ((deleted_at IS NULL AND content IS NOT NULL)" in migration


def test_server_backup_is_verified_and_can_be_encrypted() -> None:
    script = (ROOT / "scripts" / "backup-server.sh").read_text()

    assert "pg_dump" in script
    assert "--format=custom" in script
    assert "sha256sum --check SHA256SUMS" in script
    assert "BACKUP_AGE_RECIPIENT" in script
    assert "--exclude='*.upload'" in script


def test_proxy_sets_browser_security_headers_and_bounds_bodies() -> None:
    caddy = (ROOT / "Caddyfile").read_text()

    assert "max_size 70MB" in caddy
    assert "Strict-Transport-Security" in caddy
    assert "frame-ancestors 'none'" in caddy


def test_migrations_are_recorded_and_modified_history_is_rejected() -> None:
    script = (ROOT / "scripts" / "apply-migrations.sh").read_text()

    assert "schema_migrations" in script
    assert "sha256sum" in script
    assert "Applied migration was modified" in script
    assert "BEGIN;" in script and "COMMIT;" in script
    assert "REVOKE ALL ON TABLE schema_migrations FROM nano_app" in script


def test_username_migration_preserves_legacy_accounts() -> None:
    migration = (ROOT / "migrations" / "0003_username.sql").read_text()
    assert "ALTER TABLE users ADD COLUMN username" in migration
    assert "split_part(email, '@', 1)" in migration
    assert "CREATE UNIQUE INDEX users_username_unique" in migration
    assert "DROP COLUMN email" in migration


def test_proxy_uses_strict_client_ip_handling_and_limits_script_origins() -> None:
    caddy = (ROOT / "Caddyfile").read_text()
    assert "trusted_proxies_strict" in caddy
    assert "client_ip_headers CF-Connecting-IP" in caddy
    assert "header_up X-Forwarded-For {client_ip}" in caddy
    assert "script-src 'self' https://cdn.jsdelivr.net" in caddy


def test_backend_limiter_does_not_grow_without_a_bound() -> None:
    limiter = (ROOT / "api/app/rate_limit.py").read_text()
    assert "max_keys: int = 4096" in limiter
    assert "if len(self._entries) >= self.max_keys" in limiter
