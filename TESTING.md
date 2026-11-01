# Testy komunikacji i wspólnej edycji sesji

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
`0001`–`0005_study_share_edit_clock.sql`, aktywna lokalna rejestracja i port
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
`0005_study_share_edit_clock.sql`. Na obecnym etapie nie wykonano
testów Flutter, rzeczywistego PostgreSQL ani przeglądarek.
