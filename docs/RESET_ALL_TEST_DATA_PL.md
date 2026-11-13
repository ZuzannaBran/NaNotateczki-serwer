# Pełny reset danych testowych (PostgreSQL + pliki)

Skrypt usuwa **wszystkie** konta, loginy, hasła, dokumenty, operacje, sesje, backupy oraz raporty z serwera, bez tworzenia kopii. Zachowuje jedynie kod i konfigurację instalacji.

Na serwerze, przy zamkniętych aplikacjach klienckich:

```bash
cd /srv/nanotateczki
git pull --ff-only origin main
bash scripts/reset-all-test-data.sh --delete-all-test-data
```

Skrypt najpierw buduje aktualne API. Następnie usuwa wolumen PostgreSQL
przez `docker compose down --volumes` oraz zawartość `data/` i `backups/`
(także administracyjne dumpy, które mogą zawierać hasła i dokumenty).
Tworzy pustą bazę, nakłada wszystkie migracje SQL i sprawdza, że każda
tabela poza `schema_migrations` jest pusta, zanim uruchomi API.
Istniejący tunel `cloudflared` zatrzymuje i odtwarza razem z usługami
z `compose.public.yaml`. Błąd budowania albo wykrywania usług nie usuwa
danych. Błąd migracji nie uruchamia starego API na częściowej bazie.

Ponieważ znikają również wszystkie konta, trzeba zarejestrować nowe. W razie wyłączonej rejestracji administrator musi tymczasowo ustawić `ALLOW_REGISTRATION=true` w `.env` i odtworzyć kontenery API.

Stare SQLite/IndexedDB i lokalne outboxy na komputerach oraz telefonach
NIE są dostępne z serwera i muszą być wyczyszczone oddzielnie.
Aktualna migracja 0016 nadaje nowym kontom marker v2; dawnych writerów
dokumentów API już nie routuje. Reset zachowuje skonfigurowany tryb
`OPERATIONS_V2_WRITE_MODE` — nie wymusza `all` i nie zastępuje akceptacji
aplikacji. Klient musi pochodzić z odpowiadającej wersji `NaNotateczki/dev`.

Regresje uruchamiane lokalnie bez prawdziwego Dockera:

```bash
python3 -m unittest discover -s scripts/tests -p 'test_reset_all_test_data.py'
bash -n scripts/reset-all-test-data.sh
```
