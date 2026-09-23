# gcalsync

Lokalna aplikacja do synchronizacji planu zajęć WAT (eksport CSV „w formacie Outlooka”
z ewig) z Google Calendar. Plan projektu i decyzje: [docs/PLAN.md](docs/PLAN.md).

Stan: pełna automatyzacja w GitHub Actions (pobieranie planu z ewig o 0:00, 6:00, 12:00 i 18:00 i synchronizacja)
oraz CLI do pracy lokalnej i diagnostyki.

## Wymagania

- [uv](https://docs.astral.sh/uv/) — sam pobierze odpowiednią wersję Pythona (≥ 3.11)
  i zależności. Instalacja na Windows (PowerShell):
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Wszystkie polecenia uruchamiasz w katalogu repozytorium: `uv run gcalsync …`.

## Automatyzacja (GitHub Actions)

Zadanie `.github/workflows/sync.yml` o 0:00, 6:00, 12:00 i 18:00 (czas polski) loguje się
do ewig, pobiera plan grup (to samo co ikona eksportu „w formacie OutLook”), przepuszcza go
przez reguły i synchronizuje kalendarz „Plan WAT”. Konfiguracja bez sekretów: [`gcalsync.config.json`](gcalsync.config.json)
(grupy i ich priorytet, semestr, reguły, szablon tytułu, ID kalendarza, `auto_apply`).

Zapis następuje tylko, gdy: pliki z ewig pobrały się i nie mają błędów, kalendarz jest dostępny
i nie zadziałał bezpiecznik masowego usuwania. Inaczej zadanie kończy się błędem (e-mail od
GitHuba), a kalendarz zostaje nietknięty. Podsumowanie zmian jest w raporcie uruchomienia,
a pobrane pliki i dziennik w artefakcie (7 dni).

**Sterowanie z telefonu:** aplikacja GitHub → repozytorium → Actions → „Synchronizacja planu”
→ „Run workflow” (opcje: zapis / tylko dry-run, zgoda na masowe usuwanie), historia uruchomień
i podsumowania. `auto_apply` w `gcalsync.config.json` włącza zapis z uruchomień cyklicznych
(do edycji także z telefonu).

### Jednorazowa konfiguracja

1. **Google Cloud → Google Auth Platform → Audience → „Publish app”** (tryb *In production*).
   W trybie *Testing* token wygasa po 7 dniach i automat by stanął.
2. Nowy token (już w trybie *In production*): `uv run gcalsync logout`, potem
   `uv run gcalsync login`.
3. **GitHub → Settings → Secrets and variables → Actions → New repository secret:**
   - `EWIG_LOGIN`, `EWIG_PASSWORD` — dane do ewig,
   - `GOOGLE_TOKEN_JSON` — cała zawartość `%LOCALAPPDATA%\gcalsync\token.json`
     (PowerShell: `Get-Content $env:LOCALAPPDATA\gcalsync\token.json | Set-Clipboard`).
4. Workflow musi być na gałęzi domyślnej (`main`) — harmonogram i przycisk „Run workflow”
   działają tylko stamtąd.
5. Pierwsze uruchomienie ręcznie, bez zapisu. Gdy podsumowanie się zgadza, ustaw
   `"auto_apply": true` w `gcalsync.config.json`.

Lokalnie to samo: `uv run gcalsync auto` (dry-run) lub `--apply`, z tymi samymi zmiennymi
środowiskowymi.

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
`3` — błąd ewig, logowania lub Google API, `4` — zatrzymane przez bezpiecznik (tylko `auto`).

## Rozwój

```
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Test „złoty” (`tests/golden/samples_bim_excluded.json`) utrwala wynik potoku na plikach
z `samples/`. Po świadomej zmianie wyniku: `UPDATE_GOLDEN=1 uv run pytest tests/test_pipeline.py`.
Integracja z Google jest testowana na atrapie (`tests/fake_calendar.py`) i na
`HttpMockSequence` z google-api-python-client, a pobieranie z ewig na atrapie serwera
(`tests/fake_ewig.py`) — testy nie łączą się z siecią.
