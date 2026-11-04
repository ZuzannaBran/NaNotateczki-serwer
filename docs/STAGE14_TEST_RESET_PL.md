# Etap 14 — destrukcyjna migracja wyłącznie konta testowego

## Status i ograniczenie klienta

W przypadku danych testowych nie trzeba konwertować historycznych dokumentów
v1 do operacji v2. Można je **świadomie skasować** i aktywować pustą przestrzeń
v2 dla tego samego konta. Serwer zachowuje identyfikator, login i hash hasła.
To **nie** migruje kodu aplikacji: produkcyjne AppScope,
LiveWebSyncService, StudyShareGuestEditor i backup nadal korzystają z v1.
Nie wykonuj resetu swojego normalnego konta, dopóki klient Flutter
nie zostanie przełączony na v2. Może to unieruchomić edycję do czasu
aktualizacji klienta. Do technicznych testów korzystaj wyłącznie
z wydzielonego konta testowego i klienta API v2 z etapu 12.

## Przed resetem

1. Upewnij się, że konto jest testowe i można bezpowrotnie utracić jego
   notatki, planner, zadania, ustawienia, backupy, share i kolejki.
   Wszystkie urządzenia muszą zamknąć aplikację.
2. Na serwerze musi być migracja 0013. Wykonaj pełną kopię i test
   odtworzenia zgodnie z docs/STAGE13_PRODUCTION_ROLLOUT_PL.md.
   Nie pomijaj backupu całego serwera, jeśli istnieje choć jedno inne
   konto lub ważny plik.
3. Sprawdź dokładny UUID i username:

       cd /srv/nanotateczki
       docker compose exec -T db psql -X -U nano_data -d nanotateczki \
         -c 'SELECT id, username FROM users ORDER BY username;'

4. Zatrzymaj API (nie bazę):

       docker compose stop api

## Jednoznacznie potwierdzony reset

Zastąp przykładowe wartości **rzeczywistym UUID i username** konta
testowego. Trzeci argument musi mieć dokładnie format WIPE-UUID:

    bash scripts/stage14-reset-test-account-v2.sh \
      00000000-0000-0000-0000-000000000000 \
      test_account \
      WIPE-00000000-0000-0000-0000-000000000000

Skrypt:
- odmawia działania przy uruchomionym API lub nieznanym stanie Compose;
- porównuje UUID i login pod blokadą wiersza użytkownika;
- nie modyfikuje innych kont ani globalnych migracji;
- kasuje dane wskazanego konta wraz z idempotency receipts, outboxem
  serwera, share, backup rows, plannerem i sesjami odświeżania;
- zwiększa `auth_version`, unieważniając stare access JWT i WebSocket
  bez zmieniania hasła;
- w **tej samej transakcji** dodaje nieodwracalny marker protokołu v2;
- odmawia działania na koncie już aktywowanym do v2 oraz przy nieznanych
  nowych tabelach konta;
- przy błędzie PostgreSQL nie zatwierdza częściowego resetu.

**Ważne:** lokalne SQLite/IndexedDB/outbox na starych urządzeniach
pozostają. Należy je usunąć lub bezpiecznie odizolować per konto,
z wyłączoną aplikacją, przed ponownym użyciem loginu. Sam backend nie ma
dostępu do tych magazynów. Historyczne pliki backupów i raportów na
dysku serwera pozostają do ręcznej, jawnej retencji; rekordy backupów
są usunięte i nie będą pobierane przez API.

## Włączenie tylko testowego właściciela

W pliku .env ustaw:

    OPERATIONS_V2_WRITE_MODE=canary
    OPERATIONS_V2_CANARY_OWNER_IDS=<UUID_KONTA_TESTOWEGO>

Następnie:

    docker compose up -d --build api
    bash scripts/stage13-status.sh
    bash scripts/stage14-retirement-audit.sh

Audyt etapu 14 może nadal zwrócić kod 75, jeśli inne konta korzystają z
v1. To prawidłowe zachowanie. Nie ustawiaj OPERATIONS_V2_WRITE_MODE=all
w celu obejścia blokady.

## Testy

Na komputerze z checkoutem backendu, bez dotykania bazy produkcyjnej:

    python3 -m unittest discover -s scripts/tests \
      -p 'test_stage14_reset_test_account.py'
    python3 -m pytest -q \
      api/tests/test_stage14_reset_legacy_write_fences.py
    bash scripts/run-stage12-real.sh --quick

Dwa pierwsze zestawy sprawdzają zabezpieczenia przy atrapach danych i
Dockera. Ostatni korzysta z disposable PostgreSQL i API; nie zastępuje
testu pełnej aplikacji oraz nie testuje procedury resetu na realnym koncie.
Przed produkcją wymagana jest odrębna walidacja SQL resetu na wyłącznie
testowym użytkowniku w izolowanej bazie i pełny test Flutter E2E.

## Pozostałe prace przed usunięciem v1

- Podłączenie SyncEngine do wszystkich akcji edytora, z trwałym lokalnym
  outboxem, aktualizacją UI i migracją namespace (Local/konta/Share).
- Transfer assetów oraz atrybutów tekstu Quill: obecny reducer v2 je
  blokuje. Nie wolno po cichu gubić tych elementów.
- Integracja host/guest, ustawień, backupu i logout z v2.
- Testy dwóch prawdziwych urządzeń, awarii, backupu i restartu.
- Dopiero potem usuwanie v1 routerów, timerów i zgodnościowego lustra.

## Sonda kompatybilności starego klienta

API udostępnia uwierzytelnione `GET /v1/auth/protocol` zwracające
`{"protocol":"v1"}` albo `{"protocol":"v2"}`.
Zaktualizowany klient Flutter sprawdza protokół **przed** recovery,
synchronizacją snapshotów i uruchomieniem backupu v1. Konto v2 jest
blokowane lokalnie z czytelnym błędem; nie oznacza to obsługi edycji v2.
Stare serwery zwracające 404 pozostają kompatybilne z klientem v1.
Inne błędy sondy (w tym brak sieci) wstrzymują inicjalizację sync,
nie kasując lokalnych danych. Serwerowe blokady transakcyjne v1/v2
pozostają obowiązujące, nawet jeżeli wynik sondy zestarzeje się.
