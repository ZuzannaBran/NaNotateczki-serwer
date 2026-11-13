# NaNotateczki backend

Źródłem prawdy dla backendu jest repozytorium
`ZuzannaBran/NaNotateczki-serwer`, gałąź `main`.
Katalog `NaNotateczki/server/` w repo aplikacji jest zgodnościowym lustrem
backendu dla istniejących testów; nie należy wdrażać z niego niezależnych zmian.
Backend działa z `api/` w katalogu głównym serwera. API udostępnia konta z nazwą użytkownika jako loginem, hasła Argon2id, krótkie tokeny dostępu, rotowane tokeny
odświeżania, synchronizację notatników z rewizjami i dziennikiem zmian, sygnały
WebSocket oraz zweryfikowane snapshoty urządzeń.

## Uruchomienie

### Lokalny test HTTP

Bez domeny można uruchomić API tylko na `127.0.0.1:18080`:

```bash
docker compose up -d db
scripts/apply-migrations.sh
docker compose -f compose.yaml -f compose.local.yaml up -d api
curl http://127.0.0.1:18080/ready
python3 scripts/smoke-local-api.py
```

Na innym komputerze port można udostępnić sobie przez tunel SSH:
`ssh -L 18080:127.0.0.1:18080 UZYTKOWNIK@SERWER`. Flutter Web uruchomiony
lokalnie uruchom poleceniem `bash tool/run_local_backend_web.sh` z katalogu
`code/NaNotateczki`; skrypt ustawia `BACKEND_URL=http://localhost:18080`.
Domyślny dozwolony origin przeglądarki to `http://localhost:3000`; w razie
innego portu ustaw `LOCAL_APP_ORIGIN` przy uruchamianiu Compose. Ten lokalny
wariant włącza rejestrację tylko dla API dostępnego przez port loopback.

### Wdrożenie z domeną

1. Ustaw w `.env` własną domenę w `BASE_DOMAIN`; bieżące `example.invalid` jest
   wyłącznie znacznikiem zastępczym. Zachowaj prawa pliku `600`. Sekrety bazy
   są już w `.env`; nie nadpisuj ich przy aktualizacji.
2. Zapewnij użytkownikowi uruchamiającemu Compose dostęp do działającego
   demona Docker. API działa jako UID 1000 i zapisuje `data/backups/`; oba
   katalogi powinny mieć właściciela UID 1000 i prawa `700`.
3. Uruchom bazę: `docker compose up -d db`. Przy nowej bazie wykonaj migrację:
   `scripts/apply-migrations.sh`. Skrypt rejestruje SHA-256 zastosowanych
   migracji i odmawia pracy po zmianie ich treści.
4. Uruchom `docker compose up -d api caddy`, a następnie sprawdź
   `docker compose exec api python -c 'import app.main'` oraz `/ready` przez
   Caddy. PostgreSQL i API nie mają portów hosta; Caddy wiąże tylko
   `127.0.0.1:8080`. Publiczne HTTPS wymaga tunelu lub równoważnego proxy.
5. Utwórz konto poleceniem
   `docker compose exec api python -m app.create_user example_user`.
   Polecenie poprosi o hasło. Publiczna rejestracja pozostaje wyłączona.

Przed uruchomieniem nowej wersji API wykonaj wszystkie migracje, w tym
`0002_auth_version.sql`, `0003_username.sql` i
`0007_account_settings.sql`, `0008_planner_entries.sql`
i `0009_account_settings_library_state.sql`. Migracje nadają istniejącym
kontom login z unikalnej części e-maila lub identyfikatora konta. Rejestracja i nowe hasło przy zmianie wymagają 8–128
znaków; logowanie nadal akceptuje istniejące hasła 1–256 znaków. Endpoint
`POST /v1/auth/change-password` wymaga tokenu Bearer oraz JSON z
`currentPassword` i `newPassword`. Sukces
zwraca `204` bez treści i unieważnia wszystkie dotychczasowe tokeny konta oraz
połączenia WebSocket. Stary token odświeżania nie unieważni sesji utworzonej
po zmianie hasła. Klient musi wyczyścić sesję i poprosić o ponowne zalogowanie.
Rejestracja w aplikacji wymaga `ALLOW_REGISTRATION=true`; domyślnie jest
wyłączona. Konto można też utworzyć poleceniem administracyjnym z kroku 5.

Przed publicznym wdrożeniem ustaw `CLOUDFLARED_IMAGE` na sprawdzony digest,
`TUNNEL_TOKEN` i uruchom
`docker compose -f compose.yaml -f compose.public.yaml up -d`.
Tunel ma kierować `api.BASE_DOMAIN` i `app.BASE_DOMAIN` na Caddy port 8080,
zachowując nagłówek Host. Aplikację Web buduj z
`--dart-define=BACKEND_URL=https://api.BASE_DOMAIN` i umieść wynik w
`web-dist/`. Origin API jest dokładnie `https://app.BASE_DOMAIN`.

## Backup

Po zalogowaniu klient może uruchomić `RemoteBackupScheduler`.
Domyślny okres odnawialnej sesji urządzenia to 3650 dni (rotacja tokenów).
Zmiana hasła, ręczne wylogowanie i unieważnienie sesji nadal odbierają dostęp.
Lista zweryfikowanych backupów jest porządkowana po `verified_at`, aby
opóźniona finalizacja starszego uploadu była prawidłowo uwzględniana. Wysyła
zweryfikowany snapshot po zmianach, ponawia błędy i normalnie zapisuje po ustawionym czasie bezczynności. Pliki są prywatne w `data/backups/<user-id>/`; serwer potwierdza rozmiar,
SHA-256 i strukturę przed oznaczeniem ich jako poprawnych.

Pełna kopia serwera wymaga osobnego fizycznego nośnika. Uruchamiaj regularnie
`scripts/backup-server.sh /absolutna/sciezka/na/osobnym/dysku`, najlepiej z
`BACKUP_AGE_RECIPIENT` ustawionym na odbiorcę szyfrowania `age`. Skrypt zapisuje
`pg_dump -Fc`, snapshoty i manifest SHA-256; nie usuwa starszych kopii.
Zaplanuj go w systemowym harmonogramie po udostępnieniu nośnika. Przed
migracją wykonaj pełną kopię i test odtworzenia na osobnej instancji.

## Kontrola zegara klienta

Publiczny `GET /v1/time` zwraca `serverTimeMs`, czyli uniksowy czas
UTC w milisekundach z zegara serwera. API ustawia `Cache-Control: no-store`.
Klient porównuje go przy starcie zalogowanej sesji z własnym czasem,
uwzględniając RTT; rozbieżność większa niż minuta zgłaszana jest do Errors.
Endpoint nie wymaga tokenu i nie zmienia stanu bazy.
**Serwer musi mieć poprawnie zsynchronizowany zegar (NTP)**, w przeciwnym
razie ostrzeżenie klienta nie jest wiarygodne. Po aktualizacji kodu
wykonaj `docker compose up -d --build api`; migracje SQL nie są potrzebne.

## Synchronizacja ustawień konta

`GET /v1/account-settings` zwraca osobne rewizje ustawień aplikacji,
edytora, kolorów Recent i czasu bezczynności backupu.
`PUT /v1/account-settings/{category}` wymaga tokenu Bearer,
`expectedRevision` i `value`. Serwer zapisuje zmiany atomowo,
a konflikt wersji zwraca 409 z bieżącym stanem bez nadpisywania danych.
Po zapisie wysyłany jest sygnał WebSocket; klient okresowo uzgadnia
trwały stan przez REST. Konto jest jedynym źródłem uprawnień do ustawień.

Stare backupy v2 pozostają zgodne. Nowe snapshoty mogą dodatkowo zawierać
`appPreferences`, `editorPreferences`, `recentColors` i
`backupPreferences`; ich format jest weryfikowany przy finalize.
Nie zmieniaj zastosowanych wcześniej migracji. Przed aktualizacją API
wykonaj backup PostgreSQL i zastosuj `scripts/apply-migrations.sh`.

### WebSocket na urządzeniach natywnych

Przeglądarka musi wysyłać dokładny dozwolony `Origin`.
Natywne klienty Flutter na Linuxie, Androidzie i iOS mogą połączyć się
bez nagłówka `Origin`, ale nadal muszą podać ważny JWT w pierwszej
wiadomości w ciągu 5 sekund. Obcy nagłówek `Origin` jest odrzucany
kodem 4403. Po wygaśnięciu sesji serwer odcina WebSocket.

## Weryfikacja lokalna

`python -m pytest -q api/tests` sprawdza uwierzytelnianie, walidację backupu,
granice HTTP, konfigurację wdrożenia oraz kontrakty wspólnych sesji HTTP
i WebSocket. `python3 scripts/smoke-local-api.py` na uruchomionym API z PostgreSQL
sprawdza rzeczywisty cykl udostępniania, PNG, dostęp gościa i unieważnienie.
Testy równoległości, odzyskiwania backupu oraz publicznego HTTPS/WSS przez
Cloudflare nadal wymagają odrębnej weryfikacji. Matryca i instrukcje:
`TESTING.md`.

## Wymagany marker protokołu konta (migracja 0013)

Nowy backend po migracji 0013 używa trwałego `account_sync_protocol`.
Konto bez wiersza działa wyłącznie na starym zapisie v1; sam
`OPERATIONS_V2_WRITE_MODE=canary` **nie wystarcza** do włączenia v2.
Pusty, nowy właściciel testowy może zostać aktywowany jedynie podczas
wyłączenia API przez `scripts/stage13-activate-empty-canary.sh`.
Dla istniejącego konta dostępny jest administracyjny dry-run
`python -m app.legacy_cutover UUID` z katalogu `api/` po zastosowaniu
migracji 0015. Apply wymaga uprawnień do markera protokołu, zgodnego
`--expect-v1-cursor`, jawnej allowlisty v2 i
`--all-v1-devices-flushed`. Najpierw użyj odizolowanej kopii bazy i
assetów. Zmiany v1 i stary backup nie są usuwane. Nie zastępuj tej
procedury resetem produkcyjnego konta.

## Etap 13 — wdrożenie produkcyjne (jeszcze zablokowane)

Procedura, bezpieczna flaga canary, test odtworzenia PostgreSQL+assetów
oraz monitoring: [docs/STAGE13_PRODUCTION_ROLLOUT_PL.md](docs/STAGE13_PRODUCTION_ROLLOUT_PL.md).
Domyślnie `OPERATIONS_V2_WRITE_MODE=disabled`; nie przełączać istniejących
kont przed akceptacją E2E na kopii z rzeczywistą historią oraz
testem odtworzenia kopii bazy i assetów.
Lokalna walidacja archiwum bez Dockera:

```bash
python3 -m unittest discover -s scripts/tests -p 'test_stage13_backup_check.py'
```

## Etap 12 — testy prawdziwego PostgreSQL i WebSocket (bez produkcji)

Na komputerze testowym z Docker Compose v2 i Pythonem 3:

```bash
bash scripts/run-stage12-real.sh
# Krótka wersja pomija test 1000 dodanych + 1000 skasowanych obiektów:
bash scripts/run-stage12-real.sh --quick
# Opcjonalnie z checkoutem aplikacji i Flutter SDK:
NN_STAGE12_FLUTTER_ROOT=../NaNotateczki bash scripts/run-stage12-real.sh
```

Runner uruchamia osobny, nietrwały PostgreSQL 17 na dynamicznym porcie
localhost, stosuje migracje 0001–0012, następnie wykonuje prawdziwe
zapytania FastAPI i połączenia WebSocket. Nie podmienia bazy ani hubu
mockami. Po zakończeniu usuwa testowy kontener i jego dane. Dodatkowa
blokada w pytest odrzuca niejawny start i nieznane adresy bazy.
Nie uruchamiać przeciwko /srv/nanotateczki ani produkcyjnemu API.
Nie wymaga żadnej migracji produkcyjnej ani komendy na serwerze.

Ten zestaw bada backend, ale nie jest testem dwóch prawdziwych
instancji Flutter. Weryfikacja cutover z etapu 11, realnych
gestów na dwóch platformach, awarii przy zapisie i assetów
pozostaje obowiązkowa przed etapem 13. Wyniki należy odnotować
w dokumentacji aplikacji `docs/ETAP_12_TESTS.md`.

## Utwardzenie zabezpieczeń

Proxy Caddy pobiera adres klienta z nagłówka `CF-Connecting-IP` od
zaufanego kontenera tunelu, stosuje `trusted_proxies_strict` i
ustawia zweryfikowany `X-Forwarded-For` dla backendu.
Dodatkowe reguły CSP ograniczają źródła JavaScript i połączeń Web.
Walidacja API kontroluje strukturę stron, identyfikatory i kodowanie
obrazów. Limiter logowań ma limit pamięci.
Po zmianach wykonaj test Web, import PDF oraz test HTTPS przez Cloudflare.
HTTPS nie szyfruje bazy i backupów na dysku HP — nadal potrzebna jest
oddzielna decyzja dotycząca szyfrowania danych w spoczynku i E2EE.

Szczegółowa lista kroków wdrożenia poprawek bezpieczeństwa znajduje się
w `server/SECURITY_DEPLOY.md`.


## Wspólna edycja na Web

Przed wdrożeniem API zastosuj **obie** migracje:
`0004_study_shares.sql`, `0005_study_share_edit_clock.sql` i
`0006_change_events_document_revision.sql`.
Nie modyfikuj migracji już zastosowanych. Użyj:
`scripts/apply-migrations.sh` przed `docker compose up -d --build api caddy`.

Właściciel musi mieć konto i włączony Web sync. Gość dołącza wyłącznie
linkiem `/share/<UUID>` i hasłem 1–128 znaków. Otrzymuje prawdziwy edytor,
ale uprawnienie zapisu ograniczone do jednego oryginalnego UID. Po
zakończeniu albo wygaśnięciu (4 godziny) otrzyma HTTP 410.
Przedłużenie w ostatnich 15 minutach dodaje 4 godziny bez nowego linku.

Goście i właściciel wysyłają zmiany do `synced_notebooks` przez REST.
Baza przesyłana przez klienta musi odpowiadać zapisanej wersji historycznej;
serwer odrzuca spreparowane snapshoty i zapobiega równoległym sesjom
tego samego dokumentu. Usunięcie oryginału unieważnia aktywną sesję.
Zmiany trafiają do `synced_notebooks` przez REST,
a WebSocket sygnalizuje rewizje niemal natychmiast. Serwer łączy niezależne
obiekty; dla zmian tego samego obiektu stosuje kolejność przyjęcia z
dokładnością do mikrosekund, a w remisie rozmiar modyfikacji. Metadane
konfliktu pozostają w `study_shares.edit_clock`. Właściciel odzyskuje
aktywną sesję po odświeżeniu. Gość po odświeżeniu ponownie wpisuje hasło.
Stary endpoint PNG pozostaje tylko dla zgodności wstecznej.

Cloudflare Tunnel powinien kierować
`app.nanotateczki.pl` i `api.nanotateczki.pl` do
`http://caddy:8080`. Włącz WebSocket i wyłącz cache dla
`/v1/study-shares/*`. Zbuduj Flutter Web z
`--dart-define=BACKEND_URL=https://api.nanotateczki.pl`.
Nie wystawiaj portów PostgreSQL ani API na routerze. Test E2E
w dwóch prawdziwych przeglądarkach jest wymagany przed produkcją:
rysowanie jednoczesne, edycja tego samego pola, restart hosta,
przedłużenie, wygaśnięcie i odcięcie gościa.


## Synchronizacja kalendarza (migracja 0008)

Wykonaj `scripts/apply-migrations.sh` przed restartem API. Endpoints
`GET /v1/planner/entries?after=<uid>` (strony po 200) i
`PUT /v1/planner/entries/<uid>` wymagają tokenu właściciela.
Zapis jest atomowy, obejmuje osobne wydarzenia i Study Sessions oraz
trwałe tombstones po usunięciu. Potwierdzone usunięcie wygrywa z
niezsynchronizowaną edycją innego urządzenia, także gdy jego zegar jest
ustawiony w przyszłość. Usuniętego ID nie można przywrócić przez PUT,
nawet używając historycznej flagi `restoreDeleted` w synchronizacji notatek.
Dla dwóch aktywnych edycji jednego wpisu `modifiedAt` nadal pochodzi
z zegara UTC klienta; poprawna kolejność wymaga zsynchronizowanych zegarów
lub przyszłego CAS per wpis. UTC usuwa różnice stref, ale nie offset zegara.
Po commicie publikuje `planner_changed` przez WebSocket.
Backup `verifiedAt` oznacza wyłącznie weryfikację archiwum: nie wolno
używać go do rozstrzygania kolejności edycji ani do przywracania tombstones.
Po utracie połączenia klient pobiera pełny stan serwera i uzgadnia go
z trwałymi lokalnymi zmianami offline.


## Etap 14 — reset danych konta testowego do v2

Jeśli wszystkie dane konkretnego konta są przeznaczone do usunięcia,
użyj po backupie i zatrzymaniu API zabezpieczonego skryptu
`scripts/stage14-reset-test-account-v2.sh` z UUID, nazwą i jawnym
potwierdzeniem `WIPE-UUID`. Podczas jednej transakcji czyści stan v1,
sesje i kolejki tego konta oraz wstawia marker v2. Nie usuwa innych kont.
Zapisy v1 ustawień, kalendarza, Share i backupów zostały także objęte
blokadą protokołu, aby starsze urządzenia nie odtworzyły stanu po resecie.
**Uwaga:** obecny interfejs Flutter nadal używa v1; skrypt pozwala
testować v2 przez API, ale sam nie migruje edytora. Nie resetuj normalnego
konta przed migracją klienta. Szczegóły:
[docs/STAGE14_TEST_RESET_PL.md](docs/STAGE14_TEST_RESET_PL.md).

## Etap 14 — audyt wycofania legacy (bez usuwania v1)

Po wdrożeniu migracji 0013 można wykonać odczytowy audyt:

```bash
bash scripts/stage14-retirement-audit.sh
python3 -m unittest discover -s scripts/tests -p 'test_stage14_retirement_audit.py'
```

Audyt raportuje liczbę kont i dokumentów v1, aktywnych sesji Share v1,
podwójnych UID oraz niepoprawnych checkpointów. Działa w transakcji
PostgreSQL READ ONLY, niczego nie przełącza i przy blokadzie zwraca kod 75.
Nawet kod 0 nie zezwala na wyłączenie v1: nowe konta nadal używają v1,
a migracja istniejących kont, outbox urządzeń i testy E2E są otwarte.
Warunki bezpiecznej finalizacji opisano w aplikacji:
docs/ETAP_14_FINALIZACJA.md (gałąź dev).
Nie usuwać starszych routerów ani archiwalnych adapterów recovery.
