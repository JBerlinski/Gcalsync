# gcalsync

Lokalna aplikacja do synchronizacji planu zajęć WAT (eksport CSV „w formacie Outlooka”
z ewig) z Google Calendar. Plan projektu i decyzje: [docs/PLAN.md](docs/PLAN.md).

Stan: gotowe CLI i interfejs graficzny w przeglądarce (GUI) — źródła, reguły, podgląd,
logowanie do Google, kalendarz docelowy, dry-run i zapis synchronizacji.

## Wymagania

- [uv](https://docs.astral.sh/uv/) — sam pobierze odpowiednią wersję Pythona (≥ 3.11)
  i zależności. Instalacja na Windows (PowerShell):
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Wszystkie polecenia uruchamiasz w katalogu repozytorium: `uv run gcalsync …`.

## Interfejs graficzny (GUI)

```
uv run gcalsync ui
```

albo dwuklik na `start.bat`. Otworzy się przeglądarka z adresem `http://127.0.0.1:8765`
(GUI jest dostępne tylko z tego komputera). Zamknięcie: Ctrl+C albo zamknięcie okna konsoli.
Opcje: `--port N`, `--no-browser`.

Zakładki odpowiadają kolejnym krokom:

1. **Źródła** — wgrywanie plików CSV (przeciągnij albo kliknij +), nazwa nowego źródła albo
   podmiana pliku istniejącego, kolejność (priorytet) strzałkami, kodowanie, zmiana nazwy,
   usuwanie. Przy każdym źródle: liczba zdarzeń, zakres dat, przedmioty, ostrzeżenia.
2. **Reguły** — szybkie wykluczenie przedmiotu z listy przedmiotów z plików, reguły
   zaawansowane (pole, warunek, źródła, daty), włączanie/wyłączanie z licznikiem trafień.
3. **Podgląd** — co trafi do kalendarza, wykluczone, konflikty, duplikaty, kolizje, błędy.
4. **Kalendarz** — logowanie do Google, kalendarz docelowy, szablon tytułu, polityka konfliktów.
5. **Synchronizacja** — „Sprawdź zmiany” (dry-run) i „Zapisz w kalendarzu” z potwierdzeniem
   (przy dużej liczbie usunięć trzeba wpisać ich liczbę), postęp zapisu i weryfikacja.
   Tuż przed zapisem plan jest liczony ponownie — jeśli coś się zmieniło od podglądu,
   nic nie zostanie zapisane.

GUI i CLI korzystają z tych samych danych — zmiany zrobione w jednym są widoczne w drugim.

## Dane lokalne (poza repozytorium)

```
uv run gcalsync paths
```

pokazuje (i tworzy) katalog danych aplikacji. Na Windows to `%LOCALAPPDATA%\gcalsync`, czyli
`C:\Users\<nazwa>\AppData\Local\gcalsync`. Są w nim: `client_secret.json` (kładziesz sam),
`token.json` (po zalogowaniu), `config.json` (źródła, reguły, ustawienia, kalendarz),
`files\` (kopie wgranych CSV) i `runs\` (dziennik synchronizacji). Zmienna środowiskowa
`GCALSYNC_HOME` pozwala wskazać inny katalog.

## Konfiguracja źródeł i reguł

Kolejność źródeł to priorytet: **pierwsze jest najważniejsze** i wygrywa w konfliktach.

```
uv run gcalsync sources add "C:\ścieżka\kierunkowa.txt" --name "Grupa kierunkowa"
uv run gcalsync sources add "C:\ścieżka\domyslna.txt" --name "Grupa domyślna"
uv run gcalsync rules add --course "Modelowanie danych do BIM"
uv run gcalsync preview
```

| Polecenie | Znaczenie |
|---|---|
| `sources` / `sources add PLIK --name N` | lista / nowe źródło (kopia pliku trafia do katalogu danych) |
| `sources replace NAZWA PLIK` | podmiana pliku (np. nowy eksport z ewig), priorytet bez zmian |
| `sources move NAZWA POZYCJA` | zmiana priorytetu (1 = najważniejsze) |
| `sources remove NAZWA` | usunięcie źródła i kopii pliku |
| `rules add --course P` / `--contains T` / `--regex W` | wyklucz: przedmiot równy / temat zawiera / temat pasuje do wzorca |
| `rules add --field F --op O --value V` | pełna postać (pola: subject, course, kind, location; operatory: contains, equals, regex) |
| `rules add … --source NAZWA --from RRRR-MM-DD --to RRRR-MM-DD --case-sensitive` | zawężenia reguły |
| `rules`, `rules remove NR`, `rules enable NR`, `rules disable NR` | lista i zmiany reguł |
| `settings --title-template "{course} ({kind})"` | szablon tytułu; pola: `{course} {kind} {seq} {subject} {location}` |
| `preview` | podgląd zapisanej konfiguracji (`--json PLIK` zapisuje pełny wynik) |

Podgląd jednorazowy bez zapisywania konfiguracji: `gcalsync preview PLIK1 PLIK2 --names N1 N2
--exclude-course "…"`.

## Google Calendar

1. Skopiuj plik klienta OAuth (typ „Desktop app”) jako `client_secret.json` do katalogu danych
   (`gcalsync paths` pokazuje pełną ścieżkę).
2. `uv run gcalsync login` — otwiera przeglądarkę z ekranem zgody Google. Aplikacja prosi tylko
   o zakres `calendar.app.created`: może tworzyć własne kalendarze i zarządzać zdarzeniami
   wyłącznie w nich — nie widzi Twojego głównego kalendarza.
3. `uv run gcalsync calendar create` — tworzy **pusty** kalendarz „Plan WAT” (strefa
   Europe/Warsaw) i zapisuje jego ID w `config.json`.
4. `uv run gcalsync sync` — **dry-run**: pokazuje, co zostałoby dodane, zmienione i usunięte.
   Niczego nie zapisuje.
5. `uv run gcalsync sync --apply` — pokazuje ten sam plan i zapisuje go dopiero po wpisaniu
   „tak”. Gdy plan usuwa dużo zdarzeń, trzeba dodatkowo wpisać ich liczbę. Kolejność:
   dodania, zmiany, usunięcia; ok. 5 zapytań/s. Na końcu aplikacja ponownie czyta kalendarz
   i sprawdza, czy jest zgodny z planem.

Każdy zapis ma dziennik w `runs\<czas>.jsonl`. Jeśli synchronizacja zostanie przerwana
(Ctrl+C, brak sieci, wygasła sesja), następne `sync` ostrzeże o tym na początku — wystarczy
ponownie `sync --apply`: plan liczy się od nowa z faktycznego stanu kalendarza, więc dokończy
tylko brakujące operacje. Pojedyncze nieudane operacje nie zatrzymują pozostałych; trzy nieudane
pod rząd albo wygasła sesja przerywają zapis.

Pozostałe: `calendar` (sprawdzenie), `calendar forget`, `calendar use ID`, `logout`.

Zasady bezpieczeństwa synchronizacji: zmieniane są tylko zdarzenia z własnym znacznikiem
gcalsync; zakończone zajęcia są pomijane; usuwane mogą być tylko zdarzenia mieszczące się
w oknie pokrycia wgranych plików. Szczegóły: [docs/PLAN.md](docs/PLAN.md), sekcja 5.

**Tryb publikacji w Google Cloud:** w trybie *Testing* token wygasa po 7 dniach (potwierdzone
w dokumentacji Google) — wtedy `gcalsync login` ponownie. W trybie *In production* bez
weryfikacji (dozwolone do użytku osobistego, z ekranem „unverified app”) dokumentacja nie
wymienia limitu 7 dni, ale też nie mówi wprost, że go nie ma.

Kody wyjścia: `0` — OK, `1` — błędy w plikach CSV, `2` — błędne wywołanie lub konfiguracja,
`3` — błąd logowania lub Google API.

## Rozwój

```
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Test „złoty” (`tests/golden/samples_bim_excluded.json`) utrwala wynik potoku na plikach
z `samples/`. Po świadomej zmianie wyniku: `UPDATE_GOLDEN=1 uv run pytest tests/test_pipeline.py`.
Integracja z Google jest testowana na atrapie (`tests/fake_calendar.py`) i na
`HttpMockSequence` z google-api-python-client — testy nie łączą się z siecią.
