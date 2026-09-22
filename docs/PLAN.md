# gcalsync — plan projektu

Lokalna aplikacja z UI do synchronizacji planu zajęć WAT (eksport CSV „w formacie
Outlooka” z ewig) z Google Calendar.

Status: etapy 1–5 zrealizowane (podgląd CLI zweryfikowany na Windows na prawdziwych
plikach). Przerwa: dry-run na prawdziwym kalendarzu przed etapem 6 (zapis).

Oznaczenia: **[zweryfikowane]** — potwierdzone w oficjalnej dokumentacji lub w plikach;
**[niepotwierdzone]** — wniosek/pamięć, do sprawdzenia przy implementacji.

---

## 1. Analiza przykładowych plików (`samples/`)

- **A** = `_8e0dae000b9054e1711620d0b16d421c_.txt` — grupa domyślna, 75 zdarzeń.
- **B** = `_8e0dae000b9054e1711620d0b16d421c_ (1).txt` — grupa kierunkowa, 30 zdarzeń.

| Cecha | Obserwacja [zweryfikowane w plikach] | Konsekwencja |
|---|---|---|
| Nazwa pliku | Obie mają tę samą nazwę-hash (`.txt`), różni je tylko dopisek przeglądarki „ (1)”. | Źródła identyfikuje użytkownik (nadaje nazwę), nie nazwa pliku. Ten sam plik wgrany dwa razy → ostrzeżenie (hash treści). |
| Kodowanie | Nie UTF-8, brak BOM. Znaki spoza ASCII: `0xEA` (ę), `0xB3` (ł), `0xF1` (ń). | Te bajty znaczą to samo w cp1250 i ISO-8859-2 — próbki nie rozstrzygają. Różnica tylko dla ą/Ą/ś/Ś/ź/Ź. Heurystyka: UTF-8 (z BOM lub bez) → w przeciwnym razie wybór cp1250 vs ISO-8859-2 według punktacji „polskie litery minus znaki typowe dla złego odczytu” (`± ˇ ¶ ¦ Ľ ¬` dla ISO czytanego jako cp1250, `š Ľ` i znaki sterujące C1 dla odwrotnej sytuacji); remis → cp1250. Ręczne wymuszenie kodowania w UI/CLI. |
| Końce linii | CRLF, plik zakończony znakiem nowej linii. | Parser `csv` ze standardowej biblioteki, nigdy `split`. |
| Separator | Przecinek; brak cudzysłowów, średników, tabulatorów. | Separator wykrywany z nagłówka (`,` `;` `\t`); pola w cudzysłowach obsługiwane. |
| Kolumny (9) | `Temat, Lokalizacja, Data rozpoczęcia, Czas rozpoczęcia, Data zakończenia, Czas zakończenia, Przypomnienie wł./wył., Data przypomnienia, Czas przypomnienia` | Okrojony format Outlooka (brak „Opis”, „Całodzienne”). Mapowanie **po nazwie nagłówka** (z aliasami angielskimi), nie po pozycji. Brak kolumny wymaganej → czytelny błąd. Nieznane kolumny ignorowane. |
| Daty/godziny | `2026-10-01` (ISO), `09:50` (24h), bez strefy. Data początku = data końca. | Ścisłe parsowanie; dodatkowo `dd.mm.yyyy` i `HH:MM:SS`. Inny format → błąd z numerem wiersza. |
| Przypomnienia | Zawsze `Fałsz`, data/czas przypomnienia = początek. | Ignorowane. |
| Temat | `<Przedmiot> (<typ>) [<n>]`, typy w próbkach: `w`, `L`, `P`, `S`. | Rozbiór regexem na przedmiot/typ/numer; typ może być dowolnym skrótem (np. `ć`). Temat bez wzorca → cały temat jako przedmiot. `[n]` **nie** wchodzi do klucza tożsamości. |
| Lokalizacja | `17 58`, `116 57`, `A  59` (podwójna spacja). | Tekst surowy, tylko normalizacja białych znaków (decyzja). |
| Czas trwania | Zawsze 95 min; starty 08:00, 09:50, 11:40, 13:30, 16:00, 17:50. | Ostrzeżenie dla zdarzeń wielodniowych lub dłuższych niż 4 h (nie wiążemy się z siatką godzin WAT). |
| Zakres | A: 01.10–18.12.2026 (śr/czw/pt); B: 30.10.2026–29.01.2027. | Każde wystąpienie to osobny wiersz → pojedyncze zdarzenia, bez RRULE. |
| Zmiana czasu | 25.10.2026 koniec czasu letniego: 23.10 to +02:00, 30.10 to +01:00. Następna zmiana: 28.03.2027. | Czas z pliku = czas lokalny `Europe/Warsaw`. Do Google: `dateTime` bez offsetu + `timeZone: "Europe/Warsaw"` [zweryfikowane: „A time zone offset is required unless a time zone is explicitly specified in timeZone”]. Porównania w UTC. Godziny nieistniejące/niejednoznaczne → ostrzeżenie. Na Windows `zoneinfo` potrzebuje pakietu `tzdata` (dodany do zależności). |
| Duplikaty/kolizje w plikach | Brak w obu plikach. | — |
| Kolizje między plikami | 7, wszystkie: *Analizy teledetekcyjne* (B) vs *Modelowanie danych do BIM* (A). | Po wykluczeniu BIM: 0 kolizji. |

Zawartość: A — Seminarium dyplomowe 15, Geowizualizacja 30, Modelowanie danych do BIM 30;
B — Analizy teledetekcyjne 30.

---

## 2. Stack i architektura

- **Python ≥ 3.11**, zarządzanie środowiskiem przez **uv** (`uv run gcalsync …`).
- **UI: NiceGUI** w przeglądarce na `127.0.0.1` (bez natywnego okna). Model zdarzeniowy
  (bez przeładowania całego skryptu jak w Streamlicie), gotowe upload/tabele/dialogi/postęp.
- **Google:** `google-api-python-client`, `google-auth-oauthlib` (`InstalledAppFlow.run_local_server`).
- **Inne:** `tzdata` (Windows), `platformdirs` (katalog konfiguracji, od etapu 4), `pytest`, `ruff`.
- System docelowy: **Windows** (skrypt `start.bat` w etapie 8).

Rdzeń (parsowanie, reguły, scalanie, diff) jest czystą logiką bez I/O; UI i CLI to cienkie
warstwy nad nim.

```
src/gcalsync/
  model.py              # Event, raporty
  sources/
    base.py             # protokół Source (tu dojdzie EwigSource)
    outlook_csv.py      # kodowanie, dialekt, nagłówki, parsowanie wierszy
  core/
    normalize.py        # rozbiór tematu, białe znaki, strefa czasowa, klucz
    rules.py            # reguły wykluczeń
    merge.py            # deduplikacja i konflikty
    pipeline.py         # pliki -> PreviewResult
    diff.py             # (etap 5) stan docelowy vs Google -> plan operacji
  gcal/                 # (etapy 5–6) auth, client, mapping, executor
  storage.py            # (etap 4) config.json, kopie plików, dziennik
  cli.py                # gcalsync preview / sync
  ui/                   # (etap 7) NiceGUI
tests/
docs/PLAN.md
```

**Dane lokalne poza repozytorium** — `platformdirs.user_config_dir("gcalsync",
appauthor=False, roaming=False)`; na Windows `%LOCALAPPDATA%\gcalsync`, czyli
`C:\Users\<nazwa>\AppData\Local\gcalsync` [zweryfikowane w kodzie platformdirs];
`gcalsync paths` wypisuje faktyczną ścieżkę. Nadpisywalne zmienną `GCALSYNC_HOME`:
`client_secret.json`, `token.json`, `config.json` (źródła, priorytety, reguły, ID kalendarza,
szablon tytułu), `files/` (kopie wgranych CSV — decyzja: tak, nigdy w repo), `runs/*.jsonl`
(dziennik). `.gitignore` dodatkowo wyklucza sekrety i tokeny.

---

## 3. Model zdarzenia i tożsamość

```
Event: source_id, row, subject_raw, course, kind, seq, location_raw, location, start, end
```

- **Klucz tożsamości** = SHA-256 z `course (casefold) | kind (casefold) | start UTC | end UTC`.
  Bez `[n]` (numeracja może się przesunąć), bez lokalizacji (zmiana sali = aktualizacja),
  bez źródła (zmiana pliku/priorytetu nie zmienia zdarzenia w kalendarzu).
- Przeniesienie zajęć na inny termin = usunięcie + dodanie.
- **Wykrywanie zmian** (etap 5): porównanie pól faktycznego zdarzenia w Google (tytuł, sala,
  opis, początek i koniec w UTC) z docelowymi — zamiast zapisanego hasha, więc wykrywa też
  ręczne edycje; w v1 synchronizacja je nadpisuje. Przypomnienia nie są porównywane
  (`reminders.useDefault = true` ustawiane tylko przy dodaniu).

**Tytuł** z szablonu (ustawienia), domyślnie `{course} ({kind})`, np. `Geowizualizacja (L)`;
dla tematu bez typu — sam przedmiot. Numer `[n]`, pełny temat i źródło trafiają do opisu.

---

## 4. Filtrowanie, deduplikacja, konflikty

Kolejność: parsowanie → wykluczenia → deduplikacja → konflikty → stan docelowy.

- **Reguły wykluczeń** (zapisywane w `config.json` od etapu 4): pole (temat / przedmiot /
  typ / lokalizacja), operator (zawiera / równa się / regex), wielkość liter (domyślnie
  ignorowana), zakres źródeł (domyślnie wszystkie), opcjonalny zakres dat, włączona/wyłączona.
  Każde wykluczone zdarzenie pamięta regułę. Porównania po normalizacji białych znaków (NFC).
  Reguła (decyzja): **przedmiot równa się „Modelowanie danych do BIM”, wszystkie źródła** —
  nie jest wbudowana, dodaje się ją raz: `gcalsync rules add --course "Modelowanie danych do BIM"`.
- **Deduplikacja:** grupowanie po kluczu; zostaje egzemplarz ze źródła o najwyższym priorytecie
  (w obrębie jednego pliku — pierwszy wiersz); raport „występuje też w…” i rozbieżności pól.
- **Konflikty** (nakładanie `a.start < b.end && b.start < a.end`; stykanie się to nie kolizja):
  źródła po kolei od najwyższego priorytetu; zdarzenie odpada, jeśli nakłada się na przyjęte
  zdarzenie z ważniejszego źródła. Odrzucone niczego nie blokuje (deterministyczne łańcuchy).
  Kolizje **w obrębie jednego pliku**: oba zostają, z ostrzeżeniem (decyzja).
  Alternatywna polityka „zostaw wszystko, tylko ostrzegaj” dostępna jako opcja.

Przykład na próbkach (priorytet B > A):

| Etap | Wynik |
|---|---|
| Wejście | 105 (A 75 + B 30) |
| Reguła BIM | −30 → 75 |
| Duplikaty / konflikty | 0 / 0 |
| Do kalendarza | 75: Seminarium 15, Geowizualizacja 30, Analizy teledetekcyjne 30 |

Bez reguły BIM: 7 konfliktów (30.10 08:00, 09:50; 06.11 13:30; 27.11 08:00, 09:50;
09.12 11:40, 13:30). Przy B > A odpada 7 zajęć BIM (23 pozostałe trafiłyby do kalendarza —
dlatego reguła jest potrzebna); przy A > B odpadłoby 7 zajęć z Analiz.

---

## 5. Synchronizacja z Google Calendar

**Decyzja: wariant A** — osobny kalendarz „Plan WAT” tworzony przez aplikację, zakres
`https://www.googleapis.com/auth/calendar.app.created` [zweryfikowane: „Make secondary Google
calendars, and see, create, change, and delete events on them”; autoryzuje `calendars.insert`,
`calendars.get`, `events.list/insert/…`]. Zakres **nie** autoryzuje `calendarList.list`
[zweryfikowane] — ID kalendarza przechowywane w `config.json` i sprawdzane przez
`calendars.get`; `calendar.calendarlist.readonly` nie jest dodawany (decyzja).

**Znacznik:** `extendedProperties.private = {gcalsync_managed: "1", gcalsync_key, gcalsync_v: "1"}`.
Odczyt: `events.list` (singleEvents, bez usuniętych, strony po 2500) całego kalendarza
i podział lokalny na zarządzane / niezarządzane — dzięki temu podgląd pokazuje też liczbę
zdarzeń niezarządzanych. (Filtr `privateExtendedProperty` jest dostępny [zweryfikowane],
ale nie jest potrzebny.)
Limity: klucz ≤ 44 znaki, wartość ≤ 1024, ≤ 300 właściwości / 32 kB [zweryfikowane].
Zdarzenia bez znacznika — nigdy nie modyfikowane, tylko liczone w podglądzie.

**Synchronizacja przyrostowa:** D = stan docelowy, C = zarządzane zdarzenia w Google.
Dodaj D−C, zaktualizuj różne treścią, usuń C−D, usuń nadmiarowe duplikaty klucza w C.

**Zakres, w którym aplikacja może cokolwiek zmieniać** (decyzje 6 i 7, zaakceptowane):
- zdarzenia zakończone (`end ≤ teraz`) są poza synchronizacją — nie są dodawane, zmieniane
  ani usuwane;
- usuwać/zmieniać wolno tylko zarządzane zdarzenia mieszczące się w **oknie pokrycia**
  wgranych plików: od najwcześniejszego początku do najpóźniejszego końca wśród
  wszystkich sparsowanych zdarzeń (także wykluczonych). Zdarzenia poza oknem zostają
  nietknięte. Okno jest pokazywane w podglądzie;
- duplikaty (ten sam klucz więcej niż raz) są usuwane tylko w tym samym zakresie; zostaje
  egzemplarz, który już ma docelową treść;
- zarządzane zdarzenie ze znacznikiem bez klucza jest raportowane i nieruszane;
- reguły ograniczone wyłącznie do usuwanego źródła są usuwane razem z nim (zamiast stać się
  globalnymi).

**Pola zdarzenia:** `summary` (szablon), `location`, `description` (przedmiot, typ, numer,
temat, źródło, „zarządzane przez gcalsync”), `start/end` z `timeZone`,
`reminders.useDefault = true` (decyzja), bez uczestników.

**Odporność:**
- stan zawsze liczony z Google → ponowne uruchomienie po przerwaniu dokańcza pracę;
- kolejność: dodania → aktualizacje → usunięcia (najwyżej chwilowy nadmiar, nigdy braki);
- dziennik `runs/<czas>.jsonl` (plan + wynik każdej operacji), baner o przerwanej synchronizacji;
- utracona odpowiedź na insert → duplikat usuwany przy następnym diffie; ID zdarzeń nadaje
  Google (własne ID: base32hex a–v0–9, 5–1024 znaków [zweryfikowane]; blokada ponownego
  użycia ID usuniętego zdarzenia [niepotwierdzone] — dlatego nie w v1);
- limity: 600 zapytań/min/użytkownik, 10 000/min/projekt; przekroczenie → 403 lub 429
  `usageLimits`; wykładniczy backoff z losowością do 1 s, maks. 32–64 s [zweryfikowane].
  Tempo ok. 5 zapytań/s, ponawianie przy 403/429 (rate limit) i 5xx. Dokładne wartości
  `reason` w odpowiedziach błędów [niepotwierdzone] — obsługa oprze się na kodzie HTTP
  i sprawdzi `reason`, gdy jest dostępny;
- bezpiecznik: co najmniej 5 usunięć nieaktualnych zdarzeń i ponad 30% zarządzanych
  zdarzeń w zakresie → ostrzeżenie w dry-run i dodatkowe potwierdzenie przy zapisie;
- odczyty: wbudowane `num_retries` google-api-python-client — ponawia 5xx, 429, 403
  z `rateLimitExceeded`/`userRateLimitExceeded` i błędy połączenia, z wykładniczym
  opóźnieniem [zweryfikowane w kodzie biblioteki]; `calendars.insert` bez ponawiania
  (ryzyko utworzenia dwóch kalendarzy);
- `invalid_grant` → komunikat „zaloguj się ponownie”;
- **dry-run domyślnie**; zapis tylko po potwierdzeniu (UI) lub z `--apply` (CLI).

---

## 6. UI (NiceGUI, przeglądarka)

1. **Źródła** — drag & drop, kodowanie (z ręczną zmianą), liczba wierszy, zakres dat,
   przedmioty z licznikami, ostrzeżenia; nazwa źródła; priorytet ↑↓; profile źródeł
   z „podmień plik”.
2. **Reguły wykluczeń** — tabela z licznikiem trafień na żywo.
3. **Kalendarz i ustawienia** — logowanie, wybór/utworzenie kalendarza, szablon tytułu.
4. **Podgląd** — zakładki: Do kalendarza / Wykluczone / Konflikty / Duplikaty i rozbieżności /
   Zmiany w Google (+ ~ − z porównaniem pól, liczba niezarządzanych, okno pokrycia).
5. **Synchronizacja** — potwierdzenie, postęp, log, podsumowanie.

---

## 7. Konfiguracja Google Cloud / OAuth (do wykonania samodzielnie)

1. console.cloud.google.com → nowy projekt, np. `gcalsync`.
2. APIs & Services → Library → włącz **Google Calendar API**.
3. Google Auth Platform: Branding (nazwa, e-mail), Audience (**External**), Data Access
   (zakres `…/auth/calendar.app.created`).
4. Clients → Create client → **Desktop app** → pobierz JSON jako `client_secret.json`
   do katalogu konfiguracji aplikacji (nie do repo).
5. Pierwsze uruchomienie: przeglądarka, ekran „Google hasn't verified this app” → Continue.

**Tryb publikacji — co mówi dokumentacja:**
- **Testing** [zweryfikowane]: do 100 użytkowników testowych; „Authorizations by a test user
  will expire seven days from the time of consent” — refresh token też wygasa po 7 dniach.
- **In production bez weryfikacji** (decyzja) [zweryfikowane]: aplikacja do użytku osobistego
  (< 100 użytkowników) może działać bez weryfikacji; użytkownik widzi ekran „unverified app”,
  limit 100 nowych użytkowników. Lista powodów wygaśnięcia refresh tokena (odwołanie dostępu,
  6 miesięcy nieużywania, limit 100 tokenów na klienta i konto, zmiana hasła przy zakresach
  Gmaila, polityki administratora) **nie zawiera** limitu 7 dni.
- **[niepotwierdzone]** Dokumentacja nie mówi wprost, że w trybie Production limit 7 dni
  nie obowiązuje — to wniosek z tego, że jest opisany tylko dla Testing. Nie znalazłem też
  oficjalnej klasyfikacji `calendar.app.created` jako sensitive/non-sensitive.
  README opisze oba tryby; jeśli token w Production mimo to wygasa, przejście do Testing
  nic nie pogarsza.

---

## 8. Testy

- Parser na próbkach: liczby wierszy, kodowanie, nagłówki, CRLF, rozbiór tematu, `A  59`.
- Pliki syntetyczne: UTF-8 z BOM i bez, ISO-8859-2 z ą/ś/ź, cp1250 z ą/ś/ź, średnik,
  przecinek w cudzysłowie, brak kolumny, pusty plik, sam nagłówek, `dd.mm.yyyy`,
  zła data, nagłówki angielskie, inna kolejność kolumn, zła liczba pól.
- Czas: 23.10.2026 09:50 → 07:50Z; 30.10.2026 08:00 → 07:00Z; okolice 28.03.2027;
  godziny nieistniejące/niejednoznaczne.
- Reguły, deduplikacja, konflikty: 7 kolizji na próbkach, odwrócony priorytet, łańcuch A–B–C,
  stykanie, kolizja w jednym pliku, rozbieżna sala.
- **Test „złoty”**: cały potok na próbkach z regułą BIM = dokładnie 75 zdarzeń (wzorzec JSON).
- Etap 6: **FakeCalendar** w pamięci (filtr `privateExtendedProperty`, wstrzykiwanie 429/500/
  timeoutów): +75, potem 0 operacji, zmiana sali ~1, przerwanie i wznowienie bez duplikatów,
  niezarządzane nietknięte, okno pokrycia, przeszłe zdarzenia, bezpiecznik.
- Opcjonalnie ręcznie: `@pytest.mark.live` na kalendarzu testowym (domyślnie pomijane).

---

## 9. Etapy (commity na jednej gałęzi)

1. ✅ Szkielet: `pyproject.toml` (uv), ruff, pytest, `.gitignore`, README.
2. ✅ Model + parser CSV z heurystyką kodowania + testy na próbkach.
3. ✅ Reguły, deduplikacja, konflikty, potok, CLI `gcalsync preview` (bez Google), test złoty.
   **Przerwa: weryfikacja podglądu CLI na prawdziwych plikach.**
4. ✅ Trwała konfiguracja (źródła, priorytety, reguły, szablon tytułu, kopie CSV).
5. ✅ Google: OAuth, utworzenie/wybór kalendarza, odczyt, diff (dry-run).
   **Przerwa: dry-run na prawdziwym kalendarzu przed zapisem.**
6. Wykonanie planu: dziennik, retry/backoff, bezpiecznik, testy na FakeCalendar.
7. UI NiceGUI (ekrany 1–5).
8. Dopracowanie: README z OAuth, `start.bat`.

## Na później (poza v1)

- łączenie par „przeniesione” w podglądzie,
- kolory według typu zajęć,
- widok tygodniowy,
- testy UI (testy dymne NiceGUI),
- automatyczne pobieranie planu z ewig (nowa implementacja `Source`).
