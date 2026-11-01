# NaNotateczki backend

Backend działa z `api/` w katalogu głównym serwera. Jego kopia źródłowa jest
w `code/NaNotateczki/server/`. API udostępnia konta z adresem
e-mail jako loginem, hasła Argon2id, krótkie tokeny dostępu, rotowane tokeny
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
   `docker compose exec api python -m app.create_user user@example.com`.
   Polecenie poprosi o hasło. Publiczna rejestracja pozostaje wyłączona.

Przed uruchomieniem nowej wersji API wykonaj wszystkie migracje, w tym
`0002_auth_version.sql`. Rejestracja i nowe hasło przy zmianie wymagają 8–128
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
zweryfikowany snapshot po zmianach, ponawia błędy i normalnie zapisuje co 30
minut. Pliki są prywatne w `data/backups/<user-id>/`; serwer potwierdza rozmiar,
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
