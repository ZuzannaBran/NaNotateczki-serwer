# Wdrożenie wyłącznie v2 — 13 listopada 2026

API zaktualizowano w istniejącym projekcie Compose. Migracja
`0017_remove_snapshot_protocol.sql` została zastosowana o 23:09 UTC.
Baza była wtedy pusta; dalsza praca nie wymaga resetu i zachowuje nowe konta.

- Lokalny `.env`: `OPERATIONS_V2_WRITE_MODE=all`. Sekrety, domeny,
  wolumen PostgreSQL i publiczny tunel zostały zachowane.
- Usunięto tabele `synced_notebooks`, `change_events`,
  `idempotency_records` i `legacy_account_cutovers` oraz modele/importery v1.
- Auth, raporty i zegar mają adresy `/v2`. `/v1` zwraca 404.
- Publiczny `https://api.nanotateczki.pl/ready` zwraca 200.
  Anonimowy `/v2/auth/protocol` zwraca 401; jego odpowiednik v1 zwraca 404.
- Odczytowy audyt potwierdził brak tabel v1 i niespójnych checkpointów.
- Backend: 378 testów zaliczonych; 46 testów skryptów zaliczonych.
  Na odizolowanym PostgreSQL zaliczono 17 testów HTTP/WebSocket oraz
  osobno test 1000 dodanych i 1000 usuniętych obiektów.
  Sprawdzono też odmowę migracji 0017 przy danych v1 i rollback bez utraty
  konta; usunięcie historycznego fixture było osobnym działaniem testowym.

Log wykonania znajduje się w lokalnym `backups/deploy-v2-final.log`.
Aktualny audyt można powtórzyć bez zmiany danych:

```bash
bash scripts/stage13-status.sh
bash scripts/stage14-retirement-audit.sh
```

Publiczna aplikacja WWW w Cloudflare nadal wskazywała build
`32eeb13db7b3d1dc` z 2 listopada. Wymaga publikacji nowej paczki klienta.
Sama aktualizacja serwera nie publikuje plików w tamtym hostingu.
Testy na fizycznych urządzeniach pozostają oddzielną kontrolą.

Migracje 0001–0016 pozostają w historii schematu: są wymagane dla nowej
instalacji i kontroli checksum. Nie są aktywnym kodem API v1.
