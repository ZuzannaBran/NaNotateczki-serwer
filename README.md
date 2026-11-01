# NaNotateczki backend

Backend działa z `api/` w katalogu głównym serwera. Jego kopia źródłowa jest
w `code/NaNotateczki/server/`. API udostępnia konta z nazwą użytkownika jako loginem, hasła Argon2id, krótkie tokeny dostępu, rotowane tokeny
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
`0007_account_settings.sql`. Migracja nadaje istniejącym
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

Po zalogowaniu klient może uruchomić `RemoteBackupScheduler`. Wysyła
zweryfikowany snapshot po zmianach, ponawia błędy i normalnie zapisuje po ustawionym czasie bezczynności. Pliki są prywatne w `data/backups/<user-id>/`; serwer potwierdza rozmiar,
SHA-256 i strukturę przed oznaczeniem ich jako poprawnych.

Pełna kopia serwera wymaga osobnego fizycznego nośnika. Uruchamiaj regularnie
`scripts/backup-server.sh /absolutna/sciezka/na/osobnym/dysku`, najlepiej z
`BACKUP_AGE_RECIPIENT` ustawionym na odbiorcę szyfrowania `age`. Skrypt zapisuje
`pg_dump -Fc`, snapshoty i manifest SHA-256; nie usuwa starszych kopii.
Zaplanuj go w systemowym harmonogramie po udostępnieniu nośnika. Przed
migracją wykonaj pełną kopię i test odtworzenia na osobnej instancji.

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

## Weryfikacja lokalna

`python -m pytest -q api/tests` sprawdza uwierzytelnianie, walidację backupu,
granice HTTP, konfigurację wdrożenia oraz kontrakty wspólnych sesji HTTP
i WebSocket. `python3 scripts/smoke-local-api.py` na uruchomionym API z PostgreSQL
sprawdza rzeczywisty cykl udostępniania, PNG, dostęp gościa i unieważnienie.
Testy równoległości, odzyskiwania backupu oraz publicznego HTTPS/WSS przez
Cloudflare nadal wymagają odrębnej weryfikacji. Matryca i instrukcje:
`TESTING.md`.

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
