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
`0002_auth_version.sql` oraz `0003_username.sql`. Migracja nadaje istniejącym
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

### Diagnostyka Caddy przy Cloudflare Tunnel

Caddy działa na porcie 8080 bez publicznego przekierowania portów. Zachowuje
`no-new-privileges` i `cap_drop: [ALL]`, ale przyznaje wyłącznie
`NET_BIND_SERVICE` przez `cap_add`, ponieważ obraz Caddy ma uprawnienie
pliku wykonywalnego wymagające tej zdolności. Bez wyjątku kontener może
restartować się z komunikatem `exec /usr/bin/caddy: operation not permitted`.

Po aktualizacji repozytorium uruchom
`docker compose -f compose.yaml -f compose.public.yaml up -d caddy cloudflared`
i sprawdź `curl -i -H 'Host: api.nanotateczki.pl' http://127.0.0.1:8080/health`.
Port `18080` istnieje wyłącznie w nakładce `compose.local.yaml`, a nie
w publicznym wdrożeniu.

## Bezpieczeństwo po aktualizacji

Caddy ufa nagłówkowi \`CF-Connecting-IP\` tylko od prywatnego proxy
(zgodnie z \`trusted_proxies\`), weryfikuje listę pośredników od prawej
strony i nadpisuje \`X-Forwarded-For\` przed przekazaniem do API. Używaj
wyłącznie Cloudflare Tunnel; nie otwieraj portu Caddy na internet.
Nowy Content-Security-Policy ogranicza źródła skryptów i połączeń;
sprawdź działanie edytora Web i importu PDF po wdrożeniu.

Limiter logowania ma teraz twardy limit kluczy w pamięci. Walidacja
backupów i synchronizacji odrzuca błędne struktury notatników, duplikaty
stron i niepoprawnie zakodowane obrazy. Te zabezpieczenia **nie**
zapewniają szyfrowania end-to-end ani szyfrowania danych w spoczynku.
Do ochrony dysku serwera nadal konieczne jest szyfrowanie nośnika i
bezpieczne zarządzanie kluczami.

## Backup

Walidator backupu v2 porównuje SHA-256 pola payload z dokładnymi bajtami JSON
otrzymanymi od aplikacji, a nie z ponownie serializowanym obiektem Pythona.
Dzięki temu poprawny JSON z Darta (np. zapis liczby 1e-7) jest akceptowany
bez osłabiania kontroli sumy całego pliku i walidacji struktury. Przy
odrzuceniu w logu API znajduje się przyczyna walidacji bez treści notatek.


Po zalogowaniu klient może uruchomić `RemoteBackupScheduler`. Wysyła
zweryfikowany snapshot po zmianach, ponawia błędy i normalnie zapisuje po ustawionym czasie bezczynności. Pliki są prywatne w `data/backups/<user-id>/`; serwer potwierdza rozmiar,
SHA-256 i strukturę przed oznaczeniem ich jako poprawnych.

Pełna kopia serwera wymaga osobnego fizycznego nośnika. Uruchamiaj regularnie
`scripts/backup-server.sh /absolutna/sciezka/na/osobnym/dysku`, najlepiej z
`BACKUP_AGE_RECIPIENT` ustawionym na odbiorcę szyfrowania `age`. Skrypt zapisuje
`pg_dump -Fc`, snapshoty i manifest SHA-256; nie usuwa starszych kopii.
Zaplanuj go w systemowym harmonogramie po udostępnieniu nośnika. Przed
migracją wykonaj pełną kopię i test odtworzenia na osobnej instancji.

## Weryfikacja lokalna

`python -m pytest -q api/tests` sprawdza uwierzytelnianie, walidację backupu,
granice HTTP i konfigurację wdrożenia. Testy integracyjne z PostgreSQL,
równoległym zapisem dwóch sesji i odtworzeniem backupu są potrzebne przed
udostępnieniem usługi użytkownikom. Sam zestaw jednostkowy ich nie zastępuje.


## Shared study sessions (Web)

Run migration 0004_study_shares.sql before deploying the new API.
Signed-in hosts create four-hour password-protected view-only sessions.
Guests use the link, enter the password, and receive a session ticket held
only in memory; they never receive the host account token or notebook data.
Snapshots are private PNG files in data/study-shares (API UID 1000), not
public Caddy files. The host may revoke the session at any time.

Cloudflare Tunnel: existing app and api hostnames both route to Caddy :8080.
Enable WebSockets in Cloudflare Network settings. Never open router ports.
Disable caching for /v2/study-shares/* and authenticated API responses.
The browser must be rebuilt with BACKEND_URL set to the public HTTPS API.

A host sees a warning five minutes before expiration; POST
`/v2/study-shares/{id}/extend` is allowed only in the last 15 minutes,
extends the session by four hours and keeps existing guest tickets valid.
The same link and password remain active. Guests receive an `extended`
WebSocket event. No guest can extend a session.

## Własność i trwały zapis projektu

Sesja jest wyłącznie warstwą udostępniania nad istniejącym dokumentem
`synced_notebooks` właściciela. Przed utworzeniem sprawdzamy, że
dokument należy do konta i nie został usunięty. Zamknięcie i wygaśnięcie
sesji nie zmienia `synced_notebooks`, jego UID, folderu, historii ani
backupu. Usuwane są jedynie udostępnione klatki PNG i dostęp gości.

Klient powinien opróżnić lokalną kolejkę zapisu i wykonać `syncNow()`
przed utworzeniem oraz zakończeniem sesji; gdy są błędy lub oczekujące
zmiany, nie może pokazać powodzenia zapisu na koncie.

A shared session's host publishes its current dark/light mode and editor
background style/spacing through authenticated `PUT /v2/study-shares/{id}/appearance`.
Guest `GET /document` returns this state, and an `appearance_changed` WebSocket
event requests a refresh without changing document revisions or creating a
new notebook. The validated appearance is stored in the existing
`study_shares.edit_clock` JSON under `_appearance`; no SQL migration is needed.

## Chrome / Flutter Web na localhost

Jeżeli lokalny klient ma korzystać z tego API, ustaw w .env
`ALLOW_LOCALHOST_ORIGINS=true` i odtwórz kontener API. Domyślnie opcja
jest wyłączona. Dopuszcza wyłącznie originy HTTP localhost, 127.0.0.1
i [::1], także z portem; domena publiczna z APP_ORIGIN nadal działa.
Ta sama reguła obejmuje CORS HTTP oraz Origin WebSocketów konta i Share.
Nie zmienia uwierzytelniania ani uprawnień i nie przywraca tras v1.
