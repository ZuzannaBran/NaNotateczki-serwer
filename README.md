# NaNotateczki backend

## Wdrożenie wyłącznie v2

Stan serwera i wykonane kontrole:
[wdrożenie z 13 listopada](docs/V2_DEPLOYED_2026_11_13_PL.md).

`bash scripts/deploy-v2.sh` aktualizuje istniejący projekt Compose bez
kasowania danych. Zatrzymuje API na czas migracji, uruchamia je i sprawdza
`/ready`. Uruchamiaj z aktualnego `main`, z zachowaniem istniejącego `.env`.

Migracja 0016 przypisuje protokół v2 nowym i pustym kontom. Zapis jest
domyślnie wstrzymany (`OPERATIONS_V2_WRITE_MODE=disabled`). Skrypt wdrożenia
zachowuje wybrany tryb z konfiguracji Compose i nie rozszerza canary na
wszystkie konta. Pierwszy test wymaga `OPERATIONS_V2_WRITE_MODE=canary`
oraz UUID konta w `OPERATIONS_V2_CANARY_OWNER_IDS` w `.env`.
`OPERATIONS_V2_WRITE_MODE=all` ustaw dopiero po akceptacji canary,
backup/restore i testów dwóch klientów. Migracja 0017 usuwa puste tabele snapshotów. Odmawia działania, jeśli
pozostały historyczne dane lub konta bez markera v2; nie kasuje kont.
Po wdrożeniu użyj nowej wersji Flutter, bez flagi aktywacji.

Bramka canary obejmuje również ustawienia konta, tworzenie/upload/finalizację
backupu oraz tworzenie, dołączanie, przedłużanie i wygląd Share. Sam marker
v2 nie omija listy aktywowanych kont. Odczyt danych i odwołanie Share
pozostają możliwe przy wstrzymanym zapisie.

Dokumenty, Share, backup, planer i ustawienia używają adresów `/v2`.
Logowanie `/v2/auth`, diagnostyka i zegar pozostają odrębnym API.
Adresy `/v1` oraz endpointy konwersji historycznych snapshotów zwracają 404.


Źródłem prawdy dla backendu jest repozytorium
`ZuzannaBran/NaNotateczki-serwer`, gałąź `main`.
Katalog `NaNotateczki/server/` w repo aplikacji jest zgodnościowym lustrem
backendu dla istniejących testów; nie należy wdrażać z niego niezależnych zmian.
Backend działa z `api/` w katalogu głównym serwera. API udostępnia konta z nazwą użytkownika jako loginem, hasła Argon2id, krótkie tokeny dostępu, rotowane tokeny
odświeżania, synchronizację notatników przez dziennik operacji, sygnały WebSocket
oraz zweryfikowane archiwa operacyjne urządzeń (format 3).

## Uruchomienie

### Lokalny test HTTP

Bez domeny można uruchomić API tylko na `127.0.0.1:18080`:

```bash
docker compose up -d db
scripts/apply-migrations.sh
OPERATIONS_V2_WRITE_MODE=all docker compose -f compose.yaml -f compose.local.yaml up -d api
curl http://127.0.0.1:18080/ready
python3 scripts/smoke-local-api.py
```

Jawne `all` w tym przykładzie dotyczy wyłącznie odizolowanego środowiska
testowego. Nie używaj go do aktywacji produkcji przed zaliczeniem canary.

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

Przed uruchomieniem nowej wersji API wykonaj wszystkie migracje 0001–0017. Migracje nadają istniejącym
kontom login z unikalnej części e-maila lub identyfikatora konta. Rejestracja i nowe hasło przy zmianie wymagają 8–128
znaków; logowanie nadal akceptuje istniejące hasła 1–256 znaków. Endpoint
`POST /v2/auth/change-password` wymaga tokenu Bearer oraz JSON z
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

Publiczny `GET /v2/time` zwraca `serverTimeMs`, czyli uniksowy czas
UTC w milisekundach z zegara serwera. API ustawia `Cache-Control: no-store`.
Klient porównuje go przy starcie zalogowanej sesji z własnym czasem,
uwzględniając RTT; rozbieżność większa niż minuta zgłaszana jest do Errors.
Endpoint nie wymaga tokenu i nie zmienia stanu bazy.
**Serwer musi mieć poprawnie zsynchronizowany zegar (NTP)**, w przeciwnym
razie ostrzeżenie klienta nie jest wiarygodne. Po aktualizacji kodu
wykonaj `docker compose up -d --build api`; migracje SQL nie są potrzebne.

## Synchronizacja ustawień konta

`GET /v2/account-settings` zwraca osobne rewizje ustawień aplikacji,
edytora, kolorów Recent i czasu bezczynności backupu.
`PUT /v2/account-settings/{category}` wymaga tokenu Bearer,
`expectedRevision` i `value`. Serwer zapisuje zmiany atomowo,
a konflikt wersji zwraca 409 z bieżącym stanem bez nadpisywania danych.
Po zapisie wysyłany jest sygnał WebSocket; klient okresowo uzgadnia
trwały stan przez REST. Konto jest jedynym źródłem uprawnień do ustawień.

Backup konta wymaga formatu 3 (`operations-v2`). Może zawierać
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

## Gotowość konta

Nowe konta otrzymują marker `v2` w tej samej transakcji co rejestracja.
`GET /v2/auth/protocol` wymaga zalogowania. Przed startem klient sprawdza
`GET /v2/activation/readiness`: marker konta i włączony zapis.
Brak markera oznacza nieobsługiwane konto; nie uruchamia writera v1.
`OPERATIONS_V2_WRITE_MODE` zachowuje tryby `disabled`, `canary` i `all`.
Dla tej pustej instalacji docelowy zapis to `all` w lokalnym `.env`;
skrypt wdrożenia nie zmienia konfiguracji innych instalacji.

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
localhost, stosuje wszystkie migracje 0001–0017, następnie wykonuje prawdziwe
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

Właściciel tworzy Share przez `/v2/study-shares`. Gość otwiera
`/share/<UUID>` i podaje hasło. Dokument jest edytowany przez operacje v2;
nie ma endpointu zastępującego całą notatkę. Uprawnienie gościa jest
ograniczone do oryginalnego dokumentu. Wygaśnięcie lub odwołanie sesji
odbiera dostęp (HTTP 410). WebSocket dostarcza powiadomienia i podgląd,
a trwały dziennik REST umożliwia ponowienie oraz odtworzenie po restarcie.

Cloudflare Tunnel kieruje API do `http://caddy:8080`. Wyłącz cache dla
uwierzytelnionego API i włącz WebSocket. Frontend wymaga nowego builda
z `--dart-define=BACKEND_URL=https://api.nanotateczki.pl`.

## Planer, foldery i zadania

`GET /v2/planner/entries?after=<uid>` pobiera strony po 200 wpisów.
`PUT /v2/planner/entries/<uid>` wymaga `expectedRevision` i identyfikatora
mutacji. CAS rozstrzyga konkurencyjne zapisy bez polegania na zegarze
urządzenia; usunięcia mają trwałe tombstones. Foldery i zadania biblioteki
używają operacji `folder.*` i `task.*`, bez ustawienia `libraryState`.

## Kontrola po wdrożeniu

```bash
bash scripts/stage13-status.sh
bash scripts/stage14-retirement-audit.sh
```

Odczytowy audyt sprawdza markery kont, brak tabel snapshotów i zgodność
checkpointów z dziennikiem. Przy niespójności zwraca kod 75.
Migracje historyczne 0001–0016 pozostają w repozytorium, ponieważ są
niezbędne do odtworzenia schematu i weryfikacji już zapisanych checksum.
Nie wolno zmieniać migracji zastosowanych wcześniej.
