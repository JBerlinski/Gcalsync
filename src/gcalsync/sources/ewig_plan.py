"""Plan grupy odczytany ze strony HTML planu ewig — zapasowe źródło, gdy eksport CSV zawodzi.

Od 5.10.2026 eksport „w formacie Outlooka” zrywał połączenie (nagłówek Content-Length,
zero bajtów treści), podczas gdy strona planu grupy działała. Strona zawiera pełną siatkę
semestru (ustalone z kopii strony z 23.09.2026):

- dla każdego dnia tygodnia blok: wiersz nagłówka z datami („30<br>IX”, „07<br>X”, …),
  potem wiersze bloków zajęć: numer, godziny („08:00<br>09:35”) i po jednej komórce na datę;
- komórka z zajęciami ma zagnieżdżoną tabelę z tytułem „Przedmiot - Prowadzący (Forma)”,
  skrótem formy „(<b>L</b>)”, salą po nim i numerem zajęć „[n]”.

Wynik jest zamieniany na plik w tym samym formacie co eksport ewig, więc dalej działa
zwykły parser CSV i wszystkie jego kontrole.
"""

from __future__ import annotations

import csv
import html
import io
import re
from dataclasses import dataclass
from datetime import date

TAG = re.compile(r"<(/?)([a-zA-Z]+)\b([^>]*)>")
CLASS_ATTR = re.compile(r"""\bclass\s*=\s*(["']?)([^"'\s>]+)\1""", re.I)
TITLE_ATTR = re.compile(r"""\btitle\s*=\s*(["'])(.*?)\1""", re.I | re.S)
KIND = re.compile(r"\(\s*<b\b[^>]*>\s*([^<]+?)\s*</b\s*>\s*\)", re.I)
ROOM = re.compile(r"\(\s*<b\b[^>]*>[^<]*</b\s*>\s*\)\s*<br\s*/?>\s*([^<]*?)\s*</nobr", re.I)
SEQ = re.compile(r"<nobr\b[^>]*>\s*\[(\d+)\]\s*</nobr\s*>", re.I)
DATE = re.compile(r"(\d{1,2})\s*<br\s*/?>\s*([IVX]+)", re.I)
TIMES = re.compile(r"(\d{1,2}:\d{2})\s*<br\s*/?>\s*(\d{1,2}:\d{2})", re.I)
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
         "X": 10, "XI": 11, "XII": 12}  # fmt: skip

CSV_HEADER = [
    "Temat",
    "Lokalizacja",
    "Data rozpoczęcia",
    "Czas rozpoczęcia",
    "Data zakończenia",
    "Czas zakończenia",
    "Przypomnienie wł./wył.",
    "Data przypomnienia",
    "Czas przypomnienia",
]


@dataclass(frozen=True)
class GridEvent:
    course: str
    kind: str
    seq: int
    room: str
    day: date
    start: str  # GG:MM
    end: str

    @property
    def subject(self) -> str:
        return f"{self.course} ({self.kind}) [{self.seq}]"


@dataclass
class _Cell:
    cls: str
    html: str


def _rows(page: str) -> list[list[_Cell]]:
    """Wiersze tabeli z siatką planu (rozpoznawanej po komórce dnia tygodnia, klasa *HTM0).

    Komórki z zagnieżdżonymi tabelami (zajęcia) są zbierane w całości jako surowy HTML.
    """
    depth = 0
    grid_depth: int | None = None
    rows: list[list[_Cell]] = []
    row: list[_Cell] | None = None
    cell_start: int | None = None
    cell_cls = ""

    def close_cell(pos: int) -> None:
        nonlocal cell_start
        if row is not None and cell_start is not None:
            row.append(_Cell(cell_cls, page[cell_start:pos]))
        cell_start = None

    for m in TAG.finditer(page):
        closing, name, attrs = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if name == "table":
            if closing:
                if depth == grid_depth:
                    close_cell(m.start())
                    if row is not None:
                        rows.append(row)
                    return rows
                depth -= 1
            else:
                depth += 1
            continue
        if name not in ("tr", "td", "th"):
            continue
        cls_match = CLASS_ATTR.search(attrs)
        cls = cls_match.group(2) if cls_match else ""
        if grid_depth is None:
            if not closing and name == "td" and "SheTeaGrpHTM0" in cls:
                grid_depth = depth
                # Wiersz zaczął się wcześniej — od tego miejsca zbieramy jego komórki.
                row = []
            else:
                continue
        if depth != grid_depth:
            continue
        if name == "tr":
            close_cell(m.start())
            if row is not None and (closing or row):
                rows.append(row)
            row = None if closing else []
        elif closing:
            close_cell(m.start())
        else:
            close_cell(m.start())
            if row is None:
                row = []
            cell_start, cell_cls = m.end(), cls
    close_cell(len(page))
    if row:
        rows.append(row)
    return rows


def _year(month: int, semester_iid: int) -> int:
    start_year, semester = divmod(semester_iid, 10)
    if semester == 1:  # zimowy: wrzesień–grudzień w roku startu, styczeń–marzec w następnym
        return start_year if month >= 8 else start_year + 1
    return start_year + 1  # letni: luty–wrzesień roku następnego


def _cell_event(cell_html: str) -> tuple[str, str, int, str] | None:
    title = TITLE_ATTR.search(cell_html)
    kind, seq, room = KIND.search(cell_html), SEQ.search(cell_html), ROOM.search(cell_html)
    if not (title and kind and seq):
        return None
    head, sep, _ = html.unescape(title.group(2)).rpartition(" (")
    course, dash, _teacher = head.rpartition(" - ")
    course = " ".join((course if dash else head).split())
    if not sep or not course:
        return None
    room_text = " ".join(html.unescape(room.group(1)).split()) if room else ""
    return course, kind.group(1).strip(), int(seq.group(1)), room_text


def parse_plan_grid(page: str, semester_iid: int) -> list[GridEvent]:
    """Zajęcia z siatki planu grupy, posortowane chronologicznie (jak w eksporcie)."""
    events: list[GridEvent] = []
    dates: list[date | None] = []
    for row in _rows(page):
        header = [c for c in row if "DDSheTeaGrpHTM3" in c.cls]
        if header:
            dates = []
            for c in header:
                d = DATE.search(c.html)
                month = ROMAN.get(d.group(2).upper()) if d else None
                if d and month:
                    dates.append(date(_year(month, semester_iid), month, int(d.group(1))))
                else:
                    dates.append(None)
            continue
        times = next((TIMES.search(c.html) for c in row if TIMES.search(c.html)), None)
        if times is None:
            continue
        slots = [c for c in row if c.cls.endswith("SheTeaGrpHTM3") and "DD" not in c.cls]
        for day, cell in zip(dates, slots, strict=False):
            if day is None or "<table" not in cell.html.lower():
                continue
            for inner in re.findall(r"<table\b.*?</table\s*>", cell.html, re.S | re.I):
                parsed = _cell_event(inner)
                if parsed:
                    course, kind, seq, room = parsed
                    events.append(
                        GridEvent(course, kind, seq, room, day, times.group(1), times.group(2))
                    )
    events.sort(key=lambda e: (e.day, e.start, e.subject))
    return events


def grid_to_csv(events: list[GridEvent]) -> bytes:
    """Plik w formacie eksportu ewig („Outlook”, cp1250, CRLF)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for e in events:
        day = e.day.isoformat()
        writer.writerow([e.subject, e.room, day, e.start, day, e.end, "Fałsz", day, e.start])
    return out.getvalue().encode("cp1250")
