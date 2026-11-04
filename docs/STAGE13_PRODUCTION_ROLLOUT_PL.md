# Etap 13 — kontrolowane wdrożenie (stan: przygotowane, NIE przełączone)

## Status i warunki blokujące

Etap 13 **nie jest jeszcze zaliczony produkcyjnie**. Etap 12 w aplikacji
(`docs/ETAP_12_TESTS.md`) ma status NIEZALICZONY ŚRODOWISKOWO. Etap 11
(`docs/ETAP_11_MIGRATION.md`) tworzy kopie `staged-not-activated`:
brakuje bezpiecznej aktywacji namespace, serwerowej migracji UID v1 → v2,
migracji trwałego outboxu i odcięcia starego writera. Nie wolno twierdzić,
że aplikacja korzysta już produkcyjnie z operacyjnego SyncEngine.

**Twarde kryteria przed canary:**
- Zaliczone izolowane testy PostgreSQL, WebSocket i Flutter z etapu 12.
- Prawdziwy test host+guest (gesty, brak migania, reconnect, revoke), na
  dwóch urządzeniach; prawdziwy test przejść Local → A → B → A → Local.
- Bezpieczny, testowany cutover każdego konta, wraz z checkpointami,
  tombstones, assetami i zachowanym outboxem; brak podwójnego writera.
- Zabezpieczenie starych klientów przed zapisem po migracji oraz procedura
  aktualizacji kompatybilna z istniejącymi danymi.
- Kopia pełna i poprawnie **odtworzona** na niezależnym komputerze.
- Udokumentowane wyniki restartu i awarii, 0 utraconych operacji/UID.

Dopóki nie są spełnione, **nie ustawiać** `OPERATIONS_V2_WRITE_MODE=all`,
nie włączać v2 dla realnych kont, nie usuwać starych tabel ani danych.

## Zabezpieczenie API v2

`OPERATIONS_V2_WRITE_MODE` w `compose.yaml` jest domyślnie `disabled`.
Dotyczy wyłącznie `POST /v2/operations/batch`. Odczyty v2 pozostają
aktywne, aby nie odciąć już zatwierdzonych operacji. Pozostałe API v1
i rewizyjne `/v2/planner` zachowują dotychczasową semantykę.

- `disabled` — brak nowych zapisów operacyjnych; HTTP 403 dla v2 batch.
- `canary` — zapis tylko dla właścicieli z `OPERATIONS_V2_CANARY_OWNER_IDS`
  (lista UUID oddzielonych przecinkami; goście dziedziczą owner ID).
- `all` — dopiero po pełnej migracji wszystkich kont i weryfikacji E2E.

Zmiana flag **nie migruje** dokumentów. V2 nie może edytować UID obecnego
w `synced_notebooks`. Nie można zmieniać aktywnego konta z v2 z powrotem
na klienta zapisującego v1. Nie wyłączać flagi dla konta posiadającego
niewysłany outbox v2, jeśli uniemożliwi mu to synchronizację.

Przykład bezpiecznej konfiguracji przed wdrożeniem (w produkcyjnym `.env`):
```dotenv
OPERATIONS_V2_WRITE_MODE=disabled
OPERATIONS_V2_CANARY_OWNER_IDS=
```

## 1. Pełna kopia przed migracją

Wstrzymać zapisy **na czas spójnej kopii**. Uruchomić na serwerze, dopiero
gdy zewnętrzny nośnik jest faktycznie podłączony (poniższa ścieżka jest
przykładem, nie należy jej kopiować w ciemno):

```bash
cd /srv/nanotateczki
docker compose ps
docker compose stop api
scripts/backup-server.sh /mnt/ZEWNETRZNY_DYSK/nanotateczki
# Natychmiast przywróć usługę po kopii; test odtworzenia wykonuje się osobno.
docker compose up -d api caddy
```

Skrypt tworzy `postgres.dump`, `device-backups.tar.gz`,
`deployment-metadata.tar.gz` i `SHA256SUMS`. Sprawdzić, że nośnik
nie jest tym samym dyskiem co serwer. `BACKUP_AGE_RECIPIENT` pozwala
szyfrować eksport. `.env` i klucze odzyskiwania trzeba zabezpieczyć
**osobno**, na szyfrowanym nośniku; nie umieszczać ich w repo.

Zachować działający stary obraz API oraz identyfikator commita. Zatrzymany
API należy przywrócić w przypadku nieukończonej kopii, zamiast przechodzić
do migracji.

## 2. Realne odtworzenie kopii poza produkcją

Skopiować gotowy katalog backupu (lub zdecyfrować plik `.age` do
osobnego katalogu) na **komputer testowy**. Nigdy nie odtwarzać do
istniejącej bazy `nanotateczki`. Na komputerze testowym:

```bash
cd NaNotateczki-serwer
bash scripts/stage13-verify-restore.sh /sciezka/do/katalogu-backupu
```

Skrypt:
1. Weryfikuje SHA-256 trzech plików i sygnaturę `PGDMP`.
2. Odrzuca path traversal, symlinki i niebezpieczne człony archiwum.
3. Odtwarza archiwum `data/` do osobnego katalogu tymczasowego.
4. Uruchamia jednorazowego PostgreSQL 17 bez sieci, z danymi w tmpfs.
5. Wykonuje `pg_restore`, odpytuje `users` i `schema_migrations`.
6. Usuwa testowy kontener i pliki po zakończeniu.

Wymaga Dockera, Pythona, miejsca na rozpakowane assety i odpowiedniej pamięci
na testową bazę (tmpfs 2 GiB). Jest to **test odtworzenia**, nie backup.
Nie był uruchomiony na prawdziwym zrzucie produkcyjnym w tej sesji.
Test czystej logiki archiwizacji:

```bash
python3 -m unittest discover -s scripts/tests -p 'test_stage13_backup_check.py'
```

## 3. Tylko po pozytywnym odtworzeniu: schemat i API

Dopiero po domknięciu etapów 11–12 i spełnieniu wymienionych bramek:
```bash
cd /srv/nanotateczki
git status --short --branch
git pull --ff-only origin main
docker compose config --quiet
scripts/apply-migrations.sh
docker compose up -d --build api caddy
docker compose ps
docker compose exec -T api python -c 'import app.main'
curl --fail --silent --show-error -H 'Host: api.nanotateczki.pl' \
  http://127.0.0.1:8080/ready
bash scripts/stage13-status.sh
```

Migracje 0001–0012 są addytywne; nigdy nie cofać ich `DROP` ani nie
zmieniać zastosowanego SQL. `apply-migrations.sh` sprawdza sumy SHA-256.
Przy wdrożeniu pozostawić na początku `OPERATIONS_V2_WRITE_MODE=disabled`.
Nie używać `docker compose down -v`.

## 4. Canary i ograniczony cutover — jeszcze zablokowane

Gdy proces migracji serwera i aplikacji będzie ukończony oraz przetestowany,
należy zmigrować **wyłącznie testowe konto** i upewnić się, że żaden stary
klient nie może zapisywać do jego UID. Dopiero wówczas:

1. Ustalić *własny* `users.id` konta testowego (UUID z PostgreSQL).
2. Wstawić w `.env`: `OPERATIONS_V2_WRITE_MODE=canary` i
   `OPERATIONS_V2_CANARY_OWNER_IDS=<UUID_TEST_ACCOUNT>`.
3. Odtworzyć serwis API: `docker compose up -d --no-deps api`.
4. Zweryfikować synchronizację dwóch klientów, konflikty usunięcia,
   reconnect, restart, share, backup, sesję i brak przecieków A/B.
5. Uruchomić `bash scripts/stage13-status.sh`; oba liczniki
   `invalid_actor_checkpoints` i `dual_protocol_documents` muszą być 0.
6. Monitorować API: `docker compose logs --tail=200 api` oraz błędy
   po stronie aplikacji (Errors). Odnotować checkpointy i opóźnienia.

Nie włączać pozostałych kont automatycznie i nie uznawać samego HTTP 200 za
zaliczenie E2E. Flaga canary dopuszcza *tylko* zapisy operacyjne;
nie implementuje wymuszenia aktualizacji klienta ani serwerowej migracji.

## 5. Powrót do kompatybilnej wersji

W przypadku problemów zatrzymać promocję kont. Dla konta *bez*
zatwierdzonych operacji v2 można pozostać przy starej zgodnej ścieżce v1.
Dla konta **z operacjami v2** nie uruchamiać starego writera v1 ani nie
odtwarzać starszej bazy produkcyjnej nad nowszą. Przywrócić
kompatybilną wersję API z obsługą dziennika v2, zachować kolejki,
wycofać tylko niezgodne zmiany aplikacji i diagnozować z checkpointów.
Nie usuwać migracji SQL.

`scripts/stage13-status.sh` nie mierzy outboxów na urządzeniach:
do kompletnego monitoringu potrzebne są także telemetria klientów,
liczba niepotwierdzonych operacji i testy prawdziwych restartów.

**Stan bieżący:** przygotowano kontrolę backupu, odtwarzanie na
izolowanej instancji, flagę canary i odczytową diagnostykę. Nie wykonano
produkcji, migracji żywych danych ani cutover.
