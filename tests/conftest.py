from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
# Plan grupy domyślnej (Seminarium, Geowizualizacja, Modelowanie danych do BIM) — 75 zdarzeń.
DEFAULT_GROUP = SAMPLES / "_8e0dae000b9054e1711620d0b16d421c_.txt"
# Plan nowej grupy kierunkowej (Analizy teledetekcyjne) — 30 zdarzeń.
NEW_GROUP = SAMPLES / "_8e0dae000b9054e1711620d0b16d421c_ (1).txt"

HEADER = (
    "Temat,Lokalizacja,Data rozpoczęcia,Czas rozpoczęcia,Data zakończenia,Czas zakończenia,"
    "Przypomnienie wł./wył.,Data przypomnienia,Czas przypomnienia"
)


def make_csv(*rows: str, header: str = HEADER, encoding: str = "cp1250", newline="\r\n") -> bytes:
    """Buduje plik CSV w formacie ewig z podanych wierszy."""
    return newline.join([header, *rows, ""]).encode(encoding)


def row(subject, start="2026-10-01 08:00", end="2026-10-01 09:35", location="17 58") -> str:
    sd, st = start.split(maxsplit=1)
    ed, et = end.split(maxsplit=1)
    return f"{subject},{location},{sd},{st},{ed},{et},Fałsz,{sd},{st}"


@pytest.fixture
def default_group_bytes() -> bytes:
    return DEFAULT_GROUP.read_bytes()


@pytest.fixture
def new_group_bytes() -> bytes:
    return NEW_GROUP.read_bytes()
