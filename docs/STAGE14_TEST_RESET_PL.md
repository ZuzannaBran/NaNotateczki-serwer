# Etap 14 — destrukcyjna migracja wyłącznie konta testowego

## Kiedy użyć resetu

W przypadku danych testowych nie trzeba konwertować historycznych dokumentów
v1 do operacji v2. Można je **świadomie skasować** i aktywować pustą przestrzeń
v2 dla tego samego konta. Serwer zachowuje identyfikator, login i hash hasła.
Po pakiecie 11 aktualny klient Flutter korzysta wyłącznie z v2.
Puste konta są automatycznie aktywowane przez migrację 0016 i nie wymagają
resetu. Dla wartościowych danych użyj konwersji historycznej, nie resetu.

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

## Uruchomienie po resecie

Uruchom aktualny backend:

    bash scripts/deploy-v2.sh

Zaloguj się aktualną aplikacją do wyczyszczonego testowego magazynu konta.
Stare klienty nie odzyskają dostępu do zapisu v1.

## Testy

Na komputerze z checkoutem backendu, bez dotykania bazy produkcyjnej:

    python3 -m unittest discover -s scripts/tests \
      -p 'test_stage14_reset_test_account.py'
    python3 -m pytest -q \
      api/tests/test_http_boundary.py
    bash scripts/run-stage12-real.sh --quick

Dwa pierwsze zestawy sprawdzają zabezpieczenia przy atrapach danych i
Dockera. Ostatni korzysta z disposable PostgreSQL i API; nie zastępuje
testu pełnej aplikacji oraz nie testuje procedury resetu na realnym koncie.
Przed produkcją wymagana jest odrębna walidacja SQL resetu na wyłącznie
testowym użytkowniku w izolowanej bazie i pełny test Flutter E2E.

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
