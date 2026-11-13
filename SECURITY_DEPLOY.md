# Wdrożenie poprawek bezpieczeństwa

Commit w GitHub nie aktualizuje automatycznie usług na HP.
Przed wdrożeniem wykonaj kopię PostgreSQL i data/backups na odrębny nośnik.

1. Połącz się z HP przez Tailscale/SSH i przejdź do /srv/nanotateczki.
2. Wykonaj scripts/backup-server.sh /ODDZIELNY/NOSNIK.
3. Zaktualizuj pliki źródłowe repozytorium, zachowując .env, data/ i web-dist/.
4. Uruchom scripts/apply-migrations.sh i sprawdź wynik.
5. Uruchom: docker compose -f compose.yaml -f compose.public.yaml up -d --build
6. Sprawdź: docker compose -f compose.yaml -f compose.public.yaml ps
7. Sprawdź HTTPS: curl -i https://api.nanotateczki.pl/health
8. Testuj /v2/backups i /v2/operations bez tokenu: oczekuj HTTP 401.
9. W przeglądarce sprawdź logowanie, import PDF, backup, restore i WebSocket.

Cloudflare Tunnel musi kierować API oraz aplikację na http://caddy:8080.
Nie wystawiaj do internetu portów 5432, 8000, 8080 ani SSH.
Nowa konfiguracja Caddy przekazuje zweryfikowany IP użytkownika do API.

## Nadal wymagające osobnej pracy

- HTTPS nie szyfruje bazy PostgreSQL ani plików backupów na dysku.
  Wymagane jest szyfrowanie nośnika lub treści z planem odzyskiwania kluczy.
- Wspólne SQLite/IndexedDB w jednym profilu nie izoluje wielu użytkowników.
  Klient blokuje zmianę konta; do obsługi różnych osób używaj odrębnych profili.
- PDF.js jest zewnętrznym skryptem CDN, a CSP nadal dopuszcza unsafe-inline.
  Docelowo użyj lokalnej zależności PDF.js i wyeliminuj inline script.
- Pełny audyt wymaga integracyjnych testów PostgreSQL, backup/restore i Web.
