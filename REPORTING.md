# Reports and diagnostics

The backend stores private, human-readable JSON files in the mounted server
directory (by default `/srv/nanotateczki/data/`):

- `feedback/reports.json`: manual problem reports. The same system and
  identical problem text share one entry, with an occurrence counter and dates.
- `diagnostics/<account-uuid>/<device-uuid>.json`: current Errors, Data
  integrity and Optimization snapshots for a signed-in account/device.
  New content replaces the previous file atomically. Identical content is
  ignored rather than duplicated.

These paths live outside the public Caddy `web-dist/` directory. The API
creates directories with mode 0700 and files with mode 0600. Do not publish
the JSON files or include them in public web builds. They may contain stack
traces and local paths. The server backup script now archives the entire `data/` directory, including
feedback and diagnostics, alongside device snapshots.

`POST /v1/reports/feedback` accepts `{"system":"Linux","issue":"..."}`
without sign-in, subject to a rate limit. `PUT /v1/reports/diagnostics`
requires a Bearer token and accepts `deviceId`, `system`, `errors`,
`dataIntegrity` and `optimization`. Both reject unrecognized fields.
Diagnostics uploads are limited to 32,000 characters per category.

No database migration is required. Apply this change before publishing the
corresponding Flutter client, and restart the API container after backing
up the server. Do not overwrite `.env`, `data/` or PostgreSQL volumes.
