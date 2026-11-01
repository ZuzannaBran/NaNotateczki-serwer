# Testy komunikacji i wspólnych sesji

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
`0001`–`0004_study_shares.sql`, aktywna lokalna rejestracja i port
`127.0.0.1:18080`. Przed testem uruchom:

```bash
docker compose up -d db
scripts/apply-migrations.sh
docker compose -f compose.yaml -f compose.local.yaml up -d api
python3 scripts/smoke-local-api.py
```

Smoke test używa prawdziwego HTTP i PostgreSQL: rejestruje tymczasowe
konto, zapisuje notebook, sprawdza niedozwolone udostępnienie,
zakłada link, dołącza gościa z hasłem, publikuje i pobiera PNG,
sprawdza ograniczenia przedłużenia, unieważnia sesję, potwierdza
zachowanie źródłowej notatki i usuwa konto testowe.
Nie uruchamiaj go na produkcyjnej bazie. Jeżeli smoke test przerwie się
przed `finally`, sprawdź usunięcie konta i plików testowych.

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

To oddzielny **scenariusz ręczny, nie wynik zaliczonego testu**.

1. Otwórz aplikację na `https://app.nanotateczki.pl`, zaloguj się,
   włącz Web sync i utwórz notatnik z tekstem, rysunkiem i obrazem.
2. Utwórz link sesji, otwórz go w prywatnej przeglądarce, nie loguj
   gościa i podaj hasło. Złe hasło nie powinno wpuszczać.
3. Rysuj i zmieniaj tekst u właściciela. Weryfikuj aktualizację PNG
   i brak dostępu gościa do biblioteki oraz edycji.
4. Odłącz na chwilę sieć gościa, podłącz ponownie; sprawdź odzyskanie
   aktualnego obrazu. Powtórz z kilkoma gośćmi.
5. Zakończ sesję i sprawdź komunikat `ended`, błąd `410` przy
   ponownym pobieraniu obrazu oraz zachowanie oryginalnej notatki.
6. Sprawdź wygaśnięcie po 4 h i przedłużenie w końcowym oknie 15 min.
   Test przyspieszony można przeprowadzić na osobnej bazie testowej.
7. Zweryfikuj WSS i brak cache na `/v1/study-shares/*` przez
   publiczny Cloudflare Tunnel, a także ładowanie linku po odświeżeniu.

**Aktualne ograniczenia:** testy automatyczne nie obejmują rzeczywistego
Cloudflare, dwóch osobnych instancji Flutter Web, pełnego odtwarzania
sesji właściciela po odświeżeniu strony ani pomiaru wydajności przy
dużych kanwach. Nie traktuj tych scenariuszy jako zaliczonych.
