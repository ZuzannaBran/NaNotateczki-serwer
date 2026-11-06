# Testy komunikacji i wspólnej edycji sesji

## Regresja operacji v2 — 2026-11-06

`api/tests/test_stage2_assets.py` obejmuje sześć kontraktów odtwarzania
Quill: obraz/PDF × usunięcie embedu, obiektu lub strony. Inverse z nowymi
ID atomów/właścicieli wskazuje cofany gest przez `revertsOperationId`
i zależność. Przywrócenie zachowuje Delta oraz receipt tego samego pliku,
ale nadal wymaga `verified_assets`; wcześniejszy upload nie omija
kontroli kolejnego attach. Redo usuwa nową referencję/właściciela,
a stary tombstone pozostaje nieodwracalny. To test projekcji serwera,
nie pełne E2E Flutter–HTTP–PostgreSQL.
Lokalny pełny zestaw po tym przyroście: **278 passed, 8 skipped**.
Pominięte scenariusze wymagają PostgreSQL; nie uznano ich za zaliczone.

`api/tests/test_stage2_editor_operations.py` sprawdza, że opóźniony,
przegrywający `object.move` nie sprawdza już usuniętej strony docelowej.
Scenariusz zachowuje tombstone zwrócony przez `page.delete` i przekazuje
go do kolejnej projekcji, zgodnie z rzeczywistą transakcją serwera.

## Bez działającego serwera

W katalogu `server/` (lub w katalogu głównym repo `NaNotateczki-serwer`):

```bash
python -m pytest -q api/tests/test_study_shares.py api/tests/test_study_shares_flow.py
```

`test_study_shares_flow.py` używa rzeczywistego FastAPI i transportu
HTTP/WebSocket, ale izoluje zapytania SQLAlchemy w pamięci. Sprawdza:

- utworzenie sesji wyłącznie dla własnego, zsynchronizowanego notebooka/boarda;
- odrzucenie cudzych, usuniętych, lokalnych i niewłaściwych typów dokumentów;
- hasło, hash Argon2id, bilet gościa, izolację biletów i rate limiting;
- surowe PNG, format/rozmiar, brak publicznego dostępu, brak cache;
- autoryzację właściciela, odnowienie i wygaśnięcie biletu;
- unieważnienie sesji bez skasowania oryginalnej notatki;
- Origin WebSocketu, autoryzację, sygnał zmiany obrazu i natychmiastowe `ended`;
- politykę CORS dla pochodzenia innego niż aplikacja.

To są testy kontraktowe. Zastąpienie SQLAlchemy atrapą **nie dowodzi**
poprawności transakcji PostgreSQL ani działania Cloudflare.

## Na działającym lokalnym API i PostgreSQL

Wymagane są kontenery z repozytorium serwera, migracje
`0001`–`0006_change_events_document_revision.sql`, aktywna lokalna rejestracja i port
`127.0.0.1:18080`. Przed testem uruchom:

```bash
docker compose up -d db
scripts/apply-migrations.sh
docker compose -f compose.yaml -f compose.local.yaml up -d api
python3 scripts/smoke-local-api.py
```

Smoke test używa prawdziwego HTTP i PostgreSQL: rejestruje tymczasowe
konto, zapisuje notebook, sprawdza niedozwolone udostępnienie,
zakłada link, dołącza gościa z jednoznakowym hasłem, publikuje i pobiera PNG,
sprawdza ograniczenia przedłużenia, unieważnia sesję, potwierdza
zachowanie źródłowej notatki i usuwa konto testowe.
Nie uruchamiaj go na produkcyjnej bazie. Jeżeli smoke test przerwie się
przed `finally`, sprawdź usunięcie konta i plików testowych.

## Synchronizacja ustawień konta

Po migracji `0007_account_settings.sql` uruchom:

```bash
python -m pytest -q api/tests/test_account_settings.py api/tests/test_backup_validation.py
```

Ręcznie sprawdź: urządzenie A zapisuje Recent i 900 s bezczynności,
urządzenie B pobiera je po logowaniu, następnie zmienia ustawienia,
a urządzenie A otrzymuje je przy uzgodnieniu stanu. W przypadku
równoczesnych zmian tej samej kategorii potwierdź HTTP 409 i zachowanie
nieprzesłanych ustawień. Przypadki te nie są potwierdzone testem
na prawdziwym PostgreSQL.

## Realtime na Linuxie, Androidzie i iOS

```bash
python -m pytest -q api/tests/test_native_realtime.py
```

Testy kontraktowe potwierdzają: brak nagłówka `Origin` dopuszcza
przejście do obowiązkowej autoryzacji JWT, niezgodny `Origin` otrzymuje
4403, a dozwolony origin przeglądarki działa bez zmian.
Osobno sprawdź prawdziwe WebSocket/WSS na urządzeniu natywnym.

## Flutter

W repozytorium `NaNotateczki`, gałąź `dev`:

```bash
flutter test test/study_share_http_test.dart test/study_share_service_test.dart
flutter test test/live_web_sync_service_test.dart test/api_client_test.dart
dart analyze
```

Testy klienta obejmują REST, odrzucenia, tokeny gościa, WSS,
surowe PNG, metadane linku, autoryzację właściciela oraz ochronę
przed utworzeniem linku, jeżeli nie udało się zsynchronizować notatki.

## Weryfikacja w przeglądarkach po wdrożeniu

**Stan WIP; poniższe scenariusze nie zostały jeszcze zaliczone.**

1. Otwórz Flutter Web jako właściciel, zaloguj się i włącz Web sync.
   Utwórz notatnik/board w folderze i wygeneruj link z hasłem.
2. Gość otwiera link w trybie prywatnym, bez konta, i edytuje dokument
   oryginalnymi narzędziami. Sprawdź tekst, rysowanie, gumkę i obrazy.
3. Właściciel i dwóch gości równocześnie edytują różne obiekty:
   wszystkie zmiany powinny zostać w oryginale bez kopii.
4. Dwie osoby edytują ten sam obiekt: serwer rozstrzyga konflikt według
   czasu przyjęcia do mikrosekundy, przy remisie wygrywa większa zmiana.
5. Odłącz sieć gościa, edytuj, połącz ponownie; sprawdź retry, status
   potwierdzenia i brak utraty wcześniejszych zmian.
6. Odśwież stronę właściciela: odzyskaj trwającą sesję i oryginalny UID.
7. Przedłuż sesję w ostatnich 15 minutach i sprawdź 4 godziny.
8. Zakończ sesję w trakcie aktywnej edycji; stary bilet nie może już
   zapisać dokumentu (HTTP 410); notatka zostaje w folderze właściciela.
9. Sprawdź WSS, CORS, Cloudflare Tunnel oraz opóźnienie od zakończenia
   gestu do wyświetlenia aktualizacji na drugim urządzeniu.

Nie uruchamiaj smoke testu na produkcyjnej bazie.
Przed uruchomieniem lokalnego API zastosuj migrację
`0005_study_share_edit_clock.sql` oraz
`0006_change_events_document_revision.sql`. Na obecnym etapie nie wykonano
testów Flutter, rzeczywistego PostgreSQL ani przeglądarek.

## Dodatkowe regresje przed wdrożeniem

- Właściciel i gość modyfikują niezależne kreski podczas starej rewizji:
  `change_events.content` musi być identyczne z zatwierdzonym
  `synced_notebooks.content`, a nie z żądaniem pojedynczego urządzenia.
- Spreparowana `baseNotebook` musi zwrócić 409, również jeśli klient podaje
  istniejący UID; serwer sprawdza historyczną rewizję w PostgreSQL.
- Dwie aktywne sesje tego samego UID są zabronione; skasowanie dokumentu
  kończy aktywną sesję, a WebSocket gościa odbiera `ended`.
- Gdy owner kończy udostępnianie, DELETE musi poprzedzać oczekiwanie
  na lokalny backup/synchronizację.
- Po odświeżeniu gościa i odcięciu sieci sprawdź utratę ostatnich
  niepotwierdzonych operacji. Trwały outbox gościa nie jest obecnie wdrożony.

Zmiany obejmują testy regresyjne, ale wykonanie `pytest`, `flutter test`,
PostgreSQL i testów przeglądarek pozostaje **niepotwierdzone**.

## Stage 4 — operation journal (opt-in, not live yet)

- Pure projection regressions:
  `api/tests/test_operation_projection.py` checks independent ink, scalar
  convergence, permanent deletion of object/page/folder/document IDs,
  immutable deletion payloads, and explicit rejection of rich-text/assets.
- Transaction-boundary unit regressions:
  `api/tests/test_operation_stage4.py` checks idempotent HTTP retries, actor
  identity binding, causal gaps and sequence forks without database writes.
- Legacy dual-writer protection:
  `api/tests/test_operation_legacy_guard.py` checks v1 PUT and DELETE reject
  a document already materialized by v2.
- Existing `test_study_shares_flow.py` mocks account for the new v2 lookup.

Run locally when dependencies are available:

```bash
cd api
python -m pytest -q tests/test_operation_projection.py \
  tests/test_operation_stage4.py \
  tests/test_operation_legacy_guard.py \
  tests/test_study_shares_flow.py
```

**Not yet verified here:** PostgreSQL `0010` migration against existing
data, concurrent real transactions, response-loss retries across a process
crash, WebSocket reconnection and live two-device convergence. Those require
the actual DB/WebSocket integration environment and must pass before enabling
v2 for production data. No GitHub Actions run is part of stage 4.
