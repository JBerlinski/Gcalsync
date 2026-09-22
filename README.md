# gcalsync

Lokalna aplikacja do synchronizacji planu zajęć WAT (eksport CSV „w formacie Outlooka”
z ewig) z Google Calendar. Plan projektu i decyzje: [docs/PLAN.md](docs/PLAN.md).

Stan: gotowy podgląd w wierszu poleceń (parsowanie, reguły wykluczeń, deduplikacja,
konflikty). Integracja z Google Calendar i UI — w kolejnych etapach.

## Wymagania

- [uv](https://docs.astral.sh/uv/) — sam pobierze odpowiednią wersję Pythona (≥ 3.11)
  i zależności. Instalacja na Windows (PowerShell):
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

## Podgląd planu (CLI)

Kolejność plików to priorytet: **pierwszy plik jest najważniejszy** i wygrywa w konfliktach.
Podgląd niczego nie zapisuje.

PowerShell / cmd, w katalogu repozytorium:

```
uv run gcalsync preview "C:\ścieżka\kierunkowa.txt" "C:\ścieżka\domyslna.txt" ^
    --names "Grupa kierunkowa" "Grupa domyślna" ^
    --exclude-course "Modelowanie danych do BIM"
```

(`^` to kontynuacja linii w cmd; w PowerShell użyj `` ` `` albo wpisz wszystko w jednej linii.)

Opcje:

| Opcja | Znaczenie |
|---|---|
| `--names N1 N2 …` | nazwy źródeł w kolejności plików (domyślnie nazwy plików) |
| `--exclude-course PRZEDMIOT` | wyklucz przedmiot o dokładnie tej nazwie (bez typu i numeru), bez rozróżniania wielkości liter; można powtarzać |
| `--exclude-contains TEKST` | wyklucz zdarzenia, których temat zawiera tekst |
| `--exclude-regex WZORZEC` | wyklucz zdarzenia, których temat pasuje do wyrażenia regularnego |
| `--encoding KODOWANIE` | wymuś kodowanie plików (domyślnie wykrywane: UTF-8, cp1250 lub ISO-8859-2) |
| `--policy keep-all` | nie rozstrzygaj konfliktów priorytetem, tylko je pokaż |
| `--json PLIK` | zapisz pełny podgląd jako JSON (UTF-8) |

Kod wyjścia: `0` — podgląd bez błędów, `1` — w plikach są błędy (synchronizacja byłaby
zablokowana), `2` — błędne wywołanie.

Raport pokazuje: źródła (kodowanie, zakres dat, przedmioty), reguły z liczbą trafień,
wykluczone zdarzenia, scalone duplikaty z rozbieżnościami, konflikty rozwiązane priorytetem,
kolizje pozostawione jako ostrzeżenia, błędy i ostrzeżenia parsera, listę zdarzeń do kalendarza
(z offsetem strefy czasowej, np. `+02:00` przed i `+01:00` po zmianie czasu) oraz okno pokrycia
plików.

## Rozwój

```
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Test „złoty” (`tests/golden/samples_bim_excluded.json`) utrwala wynik potoku na plikach
z `samples/`. Po świadomej zmianie wyniku: `UPDATE_GOLDEN=1 uv run pytest tests/test_pipeline.py`.
