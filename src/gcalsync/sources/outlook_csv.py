"""Parser eksportu CSV „w formacie Outlooka” z ewig.

Format ustalony na podstawie próbek (samples/): cp1250 lub zgodne kodowanie, CRLF, przecinek,
polskie nagłówki. Kolumny są mapowane po nazwie nagłówka, więc kolejność i dodatkowe kolumny
nie mają znaczenia.
"""

from __future__ import annotations

import csv
import hashlib
import io
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from gcalsync.model import Issue, RawEvent
from gcalsync.sources.base import ParsedSource

UTF8_BOM = b"\xef\xbb\xbf"
BOM_CHAR = "\ufeff"

# Kodowania jednobajtowe, między którymi wybiera heurystyka (kolejność = pierwszeństwo).
LEGACY_ENCODINGS = ("cp1250", "iso-8859-2")

POLISH_LETTERS = frozenset("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def _misread_chars(true_encoding: str, read_as: str) -> frozenset[str]:
    """Znaki powstające, gdy polskie litery z `true_encoding` odczyta się jako `read_as`."""
    out = set()
    for ch in POLISH_LETTERS:
        decoded = ch.encode(true_encoding).decode(read_as, errors="ignore")
        if decoded and decoded != ch and decoded not in POLISH_LETTERS:
            out.add(decoded)
    return frozenset(out)


# Np. ISO-8859-2 czytane jako cp1250 daje „±”, „¶”, „Ľ”… — ich obecność sugeruje złe kodowanie.
SUSPICIOUS = {
    "cp1250": _misread_chars("iso-8859-2", "cp1250"),
    "iso-8859-2": _misread_chars("cp1250", "iso-8859-2"),
}

# Kanoniczne pole -> dopuszczalne nazwy nagłówków (po normalizacji: casefold, pojedyncze spacje).
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "subject": ("temat", "subject"),
    "location": ("lokalizacja", "location", "miejsce"),
    "start_date": ("data rozpoczęcia", "start date"),
    "start_time": ("czas rozpoczęcia", "godzina rozpoczęcia", "start time"),
    "end_date": ("data zakończenia", "end date"),
    "end_time": ("czas zakończenia", "godzina zakończenia", "end time"),
}
REQUIRED_FIELDS = ("subject", "start_date", "start_time", "end_date", "end_time")
FIELD_LABELS = {
    "subject": "Temat",
    "location": "Lokalizacja",
    "start_date": "Data rozpoczęcia",
    "start_time": "Czas rozpoczęcia",
    "end_date": "Data zakończenia",
    "end_time": "Czas zakończenia",
}

DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y")
TIME_FORMATS = ("%H:%M", "%H:%M:%S")
DELIMITERS = (",", ";", "\t")


class DecodeError(ValueError):
    pass


def _is_suspicious(ch: str, encoding: str) -> bool:
    return ch in SUSPICIOUS.get(encoding, ()) or "\x80" <= ch <= "\x9f"


def _score(text: str, encoding: str) -> int:
    polish = sum(ch in POLISH_LETTERS for ch in text)
    bad = sum(_is_suspicious(ch, encoding) for ch in text)
    return polish - 3 * bad


def _misdecoding_warning(text: str, encoding: str) -> list[str]:
    if not any(_is_suspicious(ch, encoding) for ch in text):
        return []
    return [
        f"Tekst odczytany jako {encoding} zawiera znaki typowe dla błędnego kodowania "
        "(np. „±”, „¶”, znaki sterujące). Sprawdź polskie znaki i w razie potrzeby wskaż "
        "kodowanie ręcznie."
    ]


def decode_bytes(data: bytes, encoding: str | None = None) -> tuple[str, str, list[str]]:
    """Dekoduje bajty pliku. Zwraca (tekst, użyte kodowanie, ostrzeżenia).

    Bez `encoding`: UTF-8 (z BOM lub bez), a gdy to niemożliwe — lepiej punktowane z cp1250
    i ISO-8859-2 (przy remisie cp1250, typowe dla eksportów w formacie Outlooka).
    Wymuszone `encoding` jest stosowane bez zmian, ale podejrzane znaki dają ostrzeżenie.
    """
    if encoding:
        try:
            text = data.decode(encoding).removeprefix(BOM_CHAR)
        except (LookupError, UnicodeDecodeError) as exc:
            raise DecodeError(f"Nie udało się odczytać pliku jako {encoding}: {exc}") from exc
        return text, encoding, _misdecoding_warning(text, encoding.lower())

    if data.startswith(UTF8_BOM):
        try:
            return data[len(UTF8_BOM) :].decode("utf-8"), "utf-8-sig", []
        except UnicodeDecodeError as exc:
            raise DecodeError(
                f"Plik ma znacznik BOM UTF-8, ale nie jest poprawnym UTF-8: {exc}"
            ) from exc
    try:
        return data.decode("utf-8"), "utf-8", []
    except UnicodeDecodeError:
        pass

    candidates: list[tuple[int, int, str, str]] = []
    for order, enc in enumerate(LEGACY_ENCODINGS):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        candidates.append((_score(text, enc), -order, enc, text))
    if not candidates:
        raise DecodeError(
            "Nie rozpoznano kodowania pliku (ani UTF-8, ani cp1250/ISO-8859-2). "
            "Wskaż kodowanie ręcznie."
        )
    _, _, enc, text = max(candidates)
    return text, enc, _misdecoding_warning(text, enc)


def normalize_header(name: str) -> str:
    name = unicodedata.normalize("NFC", name.replace(BOM_CHAR, ""))
    return " ".join(name.split()).casefold()


_ALIAS_TO_FIELD = {alias: fld for fld, aliases in HEADER_ALIASES.items() for alias in aliases}


def _map_header(header: list[str]) -> tuple[dict[str, int], list[str]]:
    """Zwraca (pole -> indeks kolumny, ostrzeżenia)."""
    mapping: dict[str, int] = {}
    warnings = []
    for idx, name in enumerate(header):
        fld = _ALIAS_TO_FIELD.get(normalize_header(name))
        if fld is None:
            continue
        if fld in mapping:
            warnings.append(f"Kolumna „{name}” powtarza się — używana jest pierwsza.")
            continue
        mapping[fld] = idx
    return mapping, warnings


def _detect_delimiter(first_line: str) -> str:
    best = (-1, -1, ",")
    for delim in DELIMITERS:
        fields = next(csv.reader([first_line], delimiter=delim), [])
        known = sum(normalize_header(f) in _ALIAS_TO_FIELD for f in fields)
        best = max(best, (known, len(fields), delim), key=lambda t: (t[0], t[1]))
    return best[2]


def _parse_date(value: str) -> date:
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"nieznany format daty „{value}” (oczekiwano RRRR-MM-DD lub DD.MM.RRRR)")


def _parse_time(value: str) -> time:
    value = value.strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"nieznany format godziny „{value}” (oczekiwano GG:MM)")


def parse_outlook_csv(
    data: bytes, encoding: str | None = None, source_id: str | None = None
) -> ParsedSource:
    """Parsuje plik CSV. Błędy nie przerywają parsowania — są zbierane w `issues`."""

    def issue(level, message, row=None):
        return Issue(level, message, source_id=source_id, row=row)

    if not data.strip():
        return ParsedSource(events=[], issues=[issue("error", "Plik jest pusty.")])

    try:
        text, used_encoding, dec_warnings = decode_bytes(data, encoding)
    except DecodeError as exc:
        return ParsedSource(events=[], issues=[issue("error", str(exc))])
    issues = [issue("warning", w) for w in dec_warnings]

    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = _detect_delimiter(first_line)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

    header: list[str] | None = None
    for row in reader:
        if any(cell.strip() for cell in row):
            header = row
            break
    if header is None:  # np. sam BOM albo same separatory
        issues.append(issue("error", "Plik jest pusty."))
        return ParsedSource(events=[], issues=issues, encoding=used_encoding, delimiter=delimiter)
    header_line = reader.line_num

    mapping, header_warnings = _map_header(header)
    issues += [issue("warning", w, header_line) for w in header_warnings]
    missing = [FIELD_LABELS[f] for f in REQUIRED_FIELDS if f not in mapping]
    if missing:
        issues.append(
            issue(
                "error",
                f"Brak wymaganych kolumn: {', '.join(missing)}. "
                f"Znalezione nagłówki: {', '.join(header)}.",
                header_line,
            )
        )
        return ParsedSource(events=[], issues=issues, encoding=used_encoding, delimiter=delimiter)

    events: list[RawEvent] = []
    for row in reader:
        line = reader.line_num
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            issues.append(
                issue(
                    "error",
                    f"Liczba pól ({len(row)}) różni się od liczby kolumn nagłówka ({len(header)}). "
                    "Możliwy niecytowany separator w temacie.",
                    line,
                )
            )
            continue
        values = {fld: row[idx] for fld, idx in mapping.items()}
        subject = values["subject"].strip()
        if not subject:
            issues.append(issue("error", "Pusty temat.", line))
            continue
        try:
            events.append(
                RawEvent(
                    row=line,
                    subject=subject,
                    location=values.get("location", ""),
                    start_date=_parse_date(values["start_date"]),
                    start_time=_parse_time(values["start_time"]),
                    end_date=_parse_date(values["end_date"]),
                    end_time=_parse_time(values["end_time"]),
                )
            )
        except ValueError as exc:
            msg = str(exc)
            issues.append(issue("error", msg[0].upper() + msg[1:] + ".", line))

    if not events and not any(i.level == "error" for i in issues):
        issues.append(issue("warning", "Plik nie zawiera żadnych zdarzeń."))
    return ParsedSource(events=events, issues=issues, encoding=used_encoding, delimiter=delimiter)


@dataclass
class CsvFileSource:
    """Plik CSV wgrany przez użytkownika. Priorytet wynika z kolejności źródeł, nie z pliku."""

    id: str
    name: str
    data: bytes
    filename: str = ""
    encoding: str | None = None  # None = wykryj automatycznie
    # Prowadzący spoza pliku (np. ze strony planu ewig): (przedmiot, typ, numer) -> nazwiska.
    teachers: dict[tuple[str, str, int], str] = field(default_factory=dict)

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        id: str | None = None,
        name: str | None = None,
        encoding: str | None = None,
    ) -> CsvFileSource:
        path = Path(path)
        return cls(
            id=id or path.name,
            name=name or path.name,
            data=path.read_bytes(),
            filename=path.name,
            encoding=encoding,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def read(self) -> ParsedSource:
        return parse_outlook_csv(self.data, encoding=self.encoding, source_id=self.id)
