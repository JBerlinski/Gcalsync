from collections import Counter
from datetime import date, time

import pytest
from conftest import DEFAULT_GROUP, NEW_GROUP, make_csv, row

from gcalsync.sources.outlook_csv import (
    SUSPICIOUS,
    CsvFileSource,
    DecodeError,
    decode_bytes,
    parse_outlook_csv,
)


def errors(parsed):
    return [i for i in parsed.issues if i.level == "error"]


def warnings(parsed):
    return [i for i in parsed.issues if i.level == "warning"]


# --- próbki -------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "count"), [(DEFAULT_GROUP, 75), (NEW_GROUP, 30)])
def test_samples_parse_cleanly(path, count):
    parsed = CsvFileSource.from_path(path).read()
    assert parsed.issues == []
    assert len(parsed.events) == count
    assert parsed.encoding == "cp1250"
    assert parsed.delimiter == ","


def test_samples_are_not_utf8(default_group_bytes):
    with pytest.raises(UnicodeDecodeError):
        default_group_bytes.decode("utf-8")
    assert b"\r\n" in default_group_bytes


def test_sample_first_row(default_group_bytes):
    first = parse_outlook_csv(default_group_bytes).events[0]
    assert first.row == 2
    assert first.subject == "Seminarium dyplomowe (S) [1]"
    assert first.location == "18 58"
    assert (first.start_date, first.start_time) == (date(2026, 10, 1), time(9, 50))
    assert (first.end_date, first.end_time) == (date(2026, 10, 1), time(11, 25))


def test_sample_polish_characters_decoded(new_group_bytes):
    text, encoding, warns = decode_bytes(new_group_bytes)
    assert encoding == "cp1250"
    assert warns == []
    assert "Data rozpoczęcia" in text
    assert "Czas zakończenia" in text
    assert "Fałsz" in text


def test_sample_location_kept_raw(new_group_bytes):
    locations = Counter(e.location for e in parse_outlook_csv(new_group_bytes).events)
    assert "A  59" in locations  # podwójna spacja jest normalizowana dopiero w core.normalize


# --- kodowanie ----------------------------------------------------------------------------


def test_utf8_with_bom():
    data = b"\xef\xbb\xbf" + make_csv(row("Język (w) [1]"), encoding="utf-8")
    parsed = parse_outlook_csv(data)
    assert parsed.encoding == "utf-8-sig"
    assert parsed.events[0].subject == "Język (w) [1]"
    assert errors(parsed) == []


def test_utf8_without_bom():
    parsed = parse_outlook_csv(make_csv(row("Źródła światła (L) [1]"), encoding="utf-8"))
    assert parsed.encoding == "utf-8"
    assert parsed.events[0].subject == "Źródła światła (L) [1]"


@pytest.mark.parametrize("encoding", ["cp1250", "iso-8859-2"])
def test_legacy_encoding_detected_by_distinguishing_letters(encoding):
    subject = "Śląskie źródła i ąś (w) [1]"
    parsed = parse_outlook_csv(make_csv(row(subject), encoding=encoding))
    assert parsed.encoding == encoding
    assert parsed.events[0].subject == subject
    assert warnings(parsed) == []


def test_ambiguous_legacy_encoding_prefers_cp1250():
    # Tylko ę/ł/ń — identyczne bajty w obu kodowaniach (jak w próbkach).
    parsed = parse_outlook_csv(make_csv(row("Łęk (w) [1]"), encoding="iso-8859-2"))
    assert parsed.encoding == "cp1250"
    assert parsed.events[0].subject == "Łęk (w) [1]"


def test_encoding_override():
    data = make_csv(row("Śląsk (w) [1]"), encoding="iso-8859-2")
    parsed = parse_outlook_csv(data, encoding="iso-8859-2")
    assert parsed.encoding == "iso-8859-2"
    assert parsed.events[0].subject == "Śląsk (w) [1]"


def test_wrong_encoding_override_is_respected_but_warned():
    # Wymuszone kodowanie nie jest „poprawiane”, ale podejrzane znaki dają ostrzeżenie.
    data = make_csv(row("Śląsk (w) [1]"), encoding="iso-8859-2")
    parsed = parse_outlook_csv(data, encoding="cp1250")
    assert parsed.encoding == "cp1250"
    assert parsed.events[0].subject != "Śląsk (w) [1]"
    assert "błędnego kodowania" in warnings(parsed)[0].message


def test_forced_utf8_strips_bom_from_header():
    data = b"\xef\xbb\xbf" + make_csv(row("Geo (w) [1]"), encoding="utf-8")
    parsed = parse_outlook_csv(data, encoding="utf-8")
    assert parsed.issues == []
    assert parsed.events[0].subject == "Geo (w) [1]"


def test_invalid_encoding_override_is_error():
    parsed = parse_outlook_csv(make_csv(row("X (w) [1]")), encoding="no-such-codec")
    assert len(errors(parsed)) == 1


def test_suspicious_sets_are_disjoint_from_polish():
    assert "±" in SUSPICIOUS["cp1250"]
    assert "¶" in SUSPICIOUS["cp1250"]
    assert "š" in SUSPICIOUS["iso-8859-2"]


def test_byte_undefined_in_cp1250_falls_back_to_iso_with_warning():
    # 0x81 nie jest poprawnym UTF-8 ani znakiem cp1250; ISO-8859-2 definiuje wszystkie bajty,
    # ale daje znak sterujący C1 — stąd ostrzeżenie.
    _, encoding, warns = decode_bytes(b"Temat\x81")
    assert encoding == "iso-8859-2"
    assert len(warns) == 1


def test_forced_encoding_that_does_not_fit_is_decode_error():
    with pytest.raises(DecodeError):
        decode_bytes(b"\xff\xfe", encoding="utf-8")


# --- dialekt i nagłówki -------------------------------------------------------------------


def test_semicolon_delimiter():
    header = "Temat;Lokalizacja;Data rozpoczęcia;Czas rozpoczęcia;Data zakończenia;Czas zakończenia"
    data = make_csv("Geo (w) [1];14 57;2026-10-01;11:40;2026-10-01;13:15", header=header)
    parsed = parse_outlook_csv(data)
    assert parsed.delimiter == ";"
    assert parsed.events[0].location == "14 57"


def test_quoted_comma_in_subject():
    data = make_csv(row('"Analiza, cz. 1 (w) [1]"'))
    parsed = parse_outlook_csv(data)
    assert errors(parsed) == []
    assert parsed.events[0].subject == "Analiza, cz. 1 (w) [1]"


def test_unquoted_comma_in_subject_is_error():
    parsed = parse_outlook_csv(make_csv(row("Analiza, cz. 1 (w) [1]")))
    assert parsed.events == []
    assert "Liczba pól" in errors(parsed)[0].message
    assert errors(parsed)[0].row == 2


def test_columns_mapped_by_name_not_position():
    header = "Czas zakończenia,Data zakończenia,Temat,Czas rozpoczęcia,Data rozpoczęcia"
    data = make_csv("09:35,2026-10-01,Geo (w) [1],08:00,2026-10-01", header=header)
    parsed = parse_outlook_csv(data)
    ev = parsed.events[0]
    assert ev.subject == "Geo (w) [1]"
    assert ev.location == ""
    assert (ev.start_time, ev.end_time) == (time(8, 0), time(9, 35))


def test_english_headers_and_lf_line_endings():
    header = "Subject,Start Date,Start Time,End Date,End Time,Location"
    data = make_csv(
        "Geo (w) [1],2026-10-01,08:00,2026-10-01,09:35,14 57", header=header, newline="\n"
    )
    parsed = parse_outlook_csv(data)
    assert errors(parsed) == []
    assert parsed.events[0].location == "14 57"


def test_missing_required_column_is_error():
    header = "Temat,Lokalizacja,Data rozpoczęcia,Czas rozpoczęcia,Data zakończenia"
    parsed = parse_outlook_csv(make_csv("Geo,1,2026-10-01,08:00,2026-10-01", header=header))
    assert parsed.events == []
    [err] = errors(parsed)
    assert "Czas zakończenia" in err.message


def test_empty_file_is_error():
    assert "pusty" in errors(parse_outlook_csv(b""))[0].message
    assert errors(parse_outlook_csv(b"\r\n  \r\n"))


def test_header_only_is_warning():
    parsed = parse_outlook_csv(make_csv())
    assert parsed.events == []
    assert errors(parsed) == []
    assert "nie zawiera" in warnings(parsed)[0].message


def test_blank_lines_are_skipped():
    data = make_csv(row("A (w) [1]"), "", ",,,,,,,,", row("B (w) [1]"))
    parsed = parse_outlook_csv(data)
    assert [e.subject for e in parsed.events] == ["A (w) [1]", "B (w) [1]"]
    assert parsed.issues == []


def test_polish_date_format():
    parsed = parse_outlook_csv(make_csv(row("A (w) [1]", "01.10.2026 08:00", "1.10.2026 09:35")))
    assert parsed.events[0].start_date == date(2026, 10, 1)
    assert parsed.events[0].end_date == date(2026, 10, 1)


def test_bad_date_reports_row_and_keeps_other_rows():
    data = make_csv(row("A (w) [1]"), row("B (w) [1]", "10/01/2026 08:00"), row("C (w) [1]"))
    parsed = parse_outlook_csv(data, source_id="plik")
    assert [e.subject for e in parsed.events] == ["A (w) [1]", "C (w) [1]"]
    [err] = errors(parsed)
    assert err.row == 3
    assert err.source_id == "plik"
    assert "10/01/2026" in err.message


def test_bad_time_is_error():
    parsed = parse_outlook_csv(make_csv(row("A (w) [1]", "2026-10-01 8:00 AM")))
    assert "8:00 AM" in errors(parsed)[0].message


def test_empty_subject_is_error():
    parsed = parse_outlook_csv(make_csv(row("")))
    assert "Pusty temat" in errors(parsed)[0].message


def test_source_sha256_differs_between_samples():
    a = CsvFileSource.from_path(DEFAULT_GROUP)
    b = CsvFileSource.from_path(NEW_GROUP)
    assert a.sha256 != b.sha256
    assert len(a.sha256) == 64
