# gcalsync

Lokalna aplikacja do synchronizacji planu zajęć WAT (eksport CSV „w formacie Outlooka”
z ewig) z Google Calendar. Plan projektu i decyzje: [docs/PLAN.md](docs/PLAN.md).

## Wymagania

- [uv](https://docs.astral.sh/uv/) — sam pobierze odpowiednią wersję Pythona (≥ 3.11)
  i zależności. Instalacja na Windows (PowerShell):
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

## Uruchamianie

```
uv run gcalsync --help
```

## Rozwój

```
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
