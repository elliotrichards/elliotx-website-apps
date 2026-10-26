from datetime import date

from sheet import parse_rows


def test_valid_rows_are_parsed_and_normalised(sheet_values):
    viewings, _ = parse_rows(sheet_values)
    by_row = {v.row: v for v in viewings}

    # Serial 46021 = 2025-12-30; a real date cell, not a string.
    assert by_row[2].watched_date == date(2025, 12, 30)
    assert by_row[2].rating == 4.5
    # Blank media_type defaults to movie; ISO string date.
    assert by_row[3].media_type == "movie"
    assert by_row[3].watched_date == date(2025, 12, 31)
    # Float id, upper-case type, fractional (date-time) serial.
    assert by_row[11].tmdb_id == 680
    assert by_row[11].media_type == "movie"
    assert by_row[11].watched_date == date(2026, 1, 31)
    # Numeric-string id, tv row.
    assert by_row[12].tmdb_id == 1396
    assert by_row[12].media_type == "tv"


def test_bad_rows_are_skipped_with_row_numbers(sheet_values):
    viewings, warnings = parse_rows(sheet_values)
    rows = {v.row for v in viewings}
    reasons = {w["row"]: w["reason"] for w in warnings}

    assert reasons[7].startswith("unparseable watched_date")  # "2026-01-31.5"
    assert reasons[8] == "missing tmdb_id"
    assert reasons[9].startswith("non-numeric tmdb_id")
    assert reasons[10].startswith("unparseable watched_date")  # 31/02, not ISO
    assert reasons[13].startswith("unknown media_type")
    assert rows.isdisjoint({7, 8, 9, 10, 13})


def test_blank_rows_are_skipped_silently(sheet_values):
    viewings, warnings = parse_rows(sheet_values)
    warned = {w["row"] for w in warnings}
    # Row 5 is [] and row 14 is all empty strings.
    assert 5 not in warned and 14 not in warned
    assert {v.row for v in viewings}.isdisjoint({5, 14})


def test_invalid_rating_keeps_the_viewing(sheet_values):
    viewings, warnings = parse_rows(sheet_values)
    pulp = next(v for v in viewings if v.row == 11)
    assert pulp.rating is None
    assert any(w["row"] == 11 and "invalid rating" in w["reason"] for w in warnings)


def test_notes_are_never_kept(sheet_values):
    viewings, _ = parse_rows(sheet_values)
    assert all("notes" not in vars(v) for v in viewings)
    assert "first watch" not in repr(viewings)


def test_half_step_ratings_only():
    rows = [
        ["a", 1, "movie", "2026-01-01", 0.5],
        ["b", 2, "movie", "2026-01-01", "3.5"],
        ["c", 3, "movie", "2026-01-01", 3.2],
        ["d", 4, "movie", "2026-01-01", 0],
        ["e", 5, "movie", "2026-01-01", True],
    ]
    viewings, warnings = parse_rows(rows)
    assert [v.rating for v in viewings] == [0.5, 3.5, None, None, None]
    assert len(warnings) == 3


def test_invalid_iso_calendar_date_is_rejected():
    viewings, warnings = parse_rows([["x", 1, "movie", "2026-02-30"]])
    assert viewings == []
    assert warnings[0]["reason"].startswith("unparseable watched_date")
