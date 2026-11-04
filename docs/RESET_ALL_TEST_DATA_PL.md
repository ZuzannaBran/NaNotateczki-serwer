# Pełny reset danych testowych (PostgreSQL + pliki)

Skrypt usuwa **wszystkie** konta, loginy, hasła, dokumenty, operacje, sesje, backupy oraz raporty z serwera, bez tworzenia kopii. Zachowuje jedynie kod i konfigurację instalacji.

Na serwerze, przy zamkniętych aplikacjach klienckich:

```bash
cd /srv/nanotateczki
git pull --ff-only origin main
bash scripts/reset-all-test-data.sh --delete-all-test-data
```

Skrypt usuwa wolumen PostgreSQL przez `docker compose down --volumes`, czyści katalog bind `data/`, tworzy pustą bazę i ponownie nakłada wszystkie migracje SQL, a następnie uruchamia Compose. W przypadku błędu zatrzymuje się bez uruchamiania starego API na częściowo skasowanych danych.

Ponieważ znikają również wszystkie konta, trzeba zarejestrować nowe. W razie wyłączonej rejestracji administrator musi tymczasowo ustawić `ALLOW_REGISTRATION=true` w `.env` i odtworzyć kontenery API.

Stare SQLite/IndexedDB i lokalne outboxy na komputerach oraz telefonach NIE są dostępne z serwera i muszą być wyczyszczone oddzielnie. Reset danych nie uruchamia produkcyjnie Flutter SyncEngine v2: nowe konta bez markera nadal korzystają z v1.

Regresje uruchamiane lokalnie bez prawdziwego Dockera:

```bash
python3 -m unittest discover -s scripts/tests -p 'test_reset_all_test_data.py'
bash -n scripts/reset-all-test-data.sh
```
