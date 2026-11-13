# Audyt v2 — aplikacja, serwer i świeży start, 13 listopada 2026

Audyt dotyczy aplikacji `NaNotateczki/dev` po `0988b7e5` i serwera
`NaNotateczki-serwer/main` po `892cc05`. Stan z 9 listopada jest historyczny.
Nie można oznaczyć całego planu jako zakończonego ani uznać instalacji za
gotową do globalnego uruchomienia v2.

## Stan planu 0–14

| Etap | Wynik kontroli |
| --- | --- |
| 0–2 | Model i protokół istnieją; frontier/kursor zastępuje limit 256 zależności. Wspólny korpus sprawdza teraz także wyniki reducerów. |
| 3 | Lokalny SQLite pozostaje zamkniętym etapem; nie tworzono drugiego magazynu. |
| 4 | Dziennik PostgreSQL, projekcja i transakcyjne API istnieją. Zmiany poniżej rozszerzają tombstones dzieci usuwanej strony. |
| 5 | Częściowy. Naprawiono pojedynczy ruch kontra usunięcie strony źródłowej, także po wcześniejszym ACK. Pozostają złożone konflikty kontenerów i niezgodność ostatniej strony. |
| 6 | Host i gość mają v2, trwały outbox i patchowanie. Ujednolicono archiwizację usunięć; pełna akceptacja Share na dwóch urządzeniach pozostaje otwarta. |
| 7 | Namespace i jawny, idempotentny import Local są zintegrowane. Reset serwera nie usuwa danych na innych urządzeniach. |
| 8 | Planner CAS, operacje folderów/zadań i ustawienia v2 są podłączone. Nie ma potrzeby wdrażania ich od początku. |
| 9 | Backup format 3, kontrola assetów i replay istnieją. Odtworzenie produkcyjne na niezależnym środowisku wymaga osobnego potwierdzenia. |
| 10 | Wspólna sekwencja flush/sync/backup/drain i zachowanie outboxu offline istnieją. |
| 11 | Migracje 0015–0016 oraz cutover historyczny istnieją. Przy świadomym starcie od zera konwersja dawnych użytkowników nie jest potrzebna. |
| 12 | Poprzedni pełny CI aplikacji zaliczył 17/17 zadań. Nowe zmiany wymagają własnego przebiegu; fizyczne urządzenia pozostają osobną akceptacją. |
| 13 | Nie zamknięty: brak globalnego rollout i pełnej akceptacji użycia. Bramka zapisu pozostaje wyłączona. |
| 14 | Produkcyjny writer dokumentów v1 został wcześniej wycofany w kodzie. Czytniki i importer historyczny pozostają potrzebne; samo ich występowanie nie dowodzi aktywnego v1. |

## Naprawy w tej sesji

1. Dodano jeden wspólny korpus **37 przypadków dostarczenia** dla Pythona
   i produkcyjnego `EditorSqliteProjector`. Obejmuje LWW, ruchy, sortIndex,
   Unicode/Quill, tombstones, Undo/Redo z nowymi UID oraz embedy PNG/PDF.
   Flutter sprawdza też niezmienione koperty i restart plikowej SQLite.
   Python niezależnie odtwarza oczekiwane wyniki w CI. Porównywane są dane
   semantyczne, nie identyczność fizycznego schematu SQLite i JSONB.
2. Usunięcie strony rezerwuje UID wszystkich jej dzieci, również tych,
   których offline ruch jeszcze nie dotarł do serwera. Późna edycja nie
   odtwarza UID na innej stronie. Backend i lustro mają tę samą zmianę.
3. Ruch przyjęty po usunięciu strony źródłowej nie zostawia lokalnego
   obiektu na stronie docelowej. Treść i pierwotne koperty trafiają do
   istniejącego archiwum odzyskiwania, bez zmiany operationId/actorSeq
   i bez usuwania outboxu. ACK sprzed usunięcia zachowuje prawidłowy ruch.
4. Konto i gość Share stosują wspólną politykę bezstratnych usunięć.
   Nawet operacja, która technicznie komutuje z ruchem, zachowuje kopię
   oczekującej lokalnej treści przed usunięciem obiektu.
5. Otwarty edytor odświeża również stronę docelową unieważnionego ruchu,
   bez odmontowania kontrolera i bez resetu viewportu.
6. Reset najpierw buduje API, usuwa także administracyjne backupy,
   zachowuje konfigurację tunelu publicznego i sprawdza pustkę wszystkich
   tabel przed startem API. Jego skrypty i regresje są objęte kontrolą lustra.
7. Rzeczywisty reset odtworzył wyścig inicjalizacji PostgreSQL: próba po
   lokalnym sockecie uznawała tymczasowy serwer init za gotowy tuż przed
   jego wyłączeniem. Healthcheck produkcyjny i testowy sprawdzają teraz TCP.
   Wznowienie resetu pamięta też konieczność uruchomienia publicznego tunelu.

## Otwarte problemy aplikacji — blokady pełnej akceptacji

### P0: więcej niż jeden zatwierdzony ruch i usunięcie strony źródłowej

`OperationStore.movesAfterSourceDeletion` celowo odmawia uproszczonego
usunięcia kopii, jeżeli UID ma także inny zatwierdzony ruch. W takim
przypadku obiekt może prawidłowo przetrwać na innej stronie serwera.
Automatyczne skasowanie go byłoby błędem. Nadal potrzebny jest pełny
rebase miejsca obiektu według zatwierdzonego dziennika oraz regresja
dwóch procesów dla ruchów A→B→C z równoczesnym usunięciem A/B.
Pozostawienie oryginalnego outboxu jest ochroną danych, nie zaliczeniem
całego pakietu 2.

### P0: różna obsługa ostatniej strony po obu stronach protokołu

`EditorSqliteProjector._deletePage` odmawia usunięcia ostatniej strony;
istniejąca regresja `the last page cannot be removed through v2` potwierdza
to zachowanie. Python `apply_operation` usuwa stronę bez takiego warunku.
Również rzeczywisty test backendu usuwa stronę dokumentu jedno-stronicowego.
Dwa urządzenia usuwające offline różne ostatnie dwie strony wymagają
uzgodnionej polityki protokołu i testu powrotu do edytora po restarcie.
Sama blokada przycisku w UI nie wystarcza.

### Akceptacja środowiska i aplikacji

- Test dwóch procesów Flutter i testy symulatorów nie zastępują pełnych
  gestów hosta/gościa oraz offline/restart na dwóch fizycznych urządzeniach.
- Lokalny serwer nie ma `pdftoppm`. Pełny Flutter wykazał błąd importu PDF
  z `ProcessException: No such file or directory`. Nie zaliczono tego
  przebiegu. CI instaluje wymaganą zależność.
- Publiczne odczyty `app.nanotateczki.pl` i `api.nanotateczki.pl` zwróciły
  z tego środowiska HTTP 403. Ten wynik nie potwierdza dostępności nowej
  aplikacji dla użytkownika; wymaga sprawdzenia reguł dostępu i wersji klienta.
- Backup ma istniejące limity 64 MiB i 100000 zdarzeń. Nie potwierdzono
  produkcyjnej polityki kompaktacji/retencji dla większej historii.

## Weryfikacja i reset

- Końcowy zestaw powiązanych regresji Flutter: **170 PASS**.
- Wspólny korpus: **37 PASS** po stronie Python i SQLite; restart SQLite
  jest częścią tych testów.
- Rzeczywisty, odrębny PostgreSQL: **19 PASS**, w tym nowy test usunięcia
  źródła przed ruchem, późniejszą edycją i próbą ponownego użycia UID.
- Skrypty serwera: **46 PASS**; kontrola narzędzia lustra: **4 PASS**.
- Cały backend bez prawdziwej bazy: **378 PASS, 19 SKIP**. Pominięte
  scenariusze zaliczono osobno na rzeczywistym PostgreSQL (19 PASS).
- Format 303 plików Dart: bez zmian po formatowaniu. Pełny lokalny Flutter
  przerwano po błędzie brakującego `pdftoppm`, aby nie przeciążać serwera.
  Nie jest to zielony wynik pełnej macierzy ani zakończona analiza Darta.
- Nowe pełne CI jest uruchamiane przez jawne markery obu repozytoriów.
  Oczekujące zadania nie są wynikiem PASS.

Pełny reset nie tworzy nowej kopii danych, zgodnie z poleceniem usunięcia
wszystkich śladów użytkowników. Kod i konfiguracja instalacji pozostają.
Nie obejmuje to telefonów, innych komputerów, kopii na zewnętrznych
nośnikach ani kont przeglądarki niedostępnych na tym serwerze.

Po resecie ponowny odczyt każdej tabeli użytkowników potwierdził pustkę.
Liczby: **0 kont, 0 operacji, 0 dokumentów v1, 16 migracji**.
`data/` i `backups/` zawierają **0 plików**. API `/ready` zwraca 200;
działają DB, API, Caddy i odtworzony tunel. Runtime i `.env` mają zgodny
tryb `OPERATIONS_V2_WRITE_MODE=disabled`. Zastąpiono wcześniejszą
nieskuteczną konfigurację `all`, podczas gdy stary runtime pozostawał
`disabled`. Nie wykonano globalnej aktywacji v2 na podstawie samych flag.
