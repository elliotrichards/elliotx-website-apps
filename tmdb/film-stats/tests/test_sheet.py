from datetime import date

import pytest

from sheet import PUBLIC_NOTE_MAX, SheetError, parse_rows

HEADER = ["title", "tmdb_id", "media_type", "watched_date", "rating", "notes"]


def test_valid_rows_are_parsed_and_normalised(sheet_values):
    viewings, _ = parse_rows(sheet_values)
    by_row = {v.row: v for v in viewings}

    # Serial 46021 = 2025-12-30; a real date cell, not a string.
    assert by_row[2].watched_date == date(2025, 12, 30)
    assert by_row[2].score == 9  # rating 4.5, doubled
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
    assert pulp.score is None
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
    viewings, warnings = parse_rows([HEADER, *rows])
    assert [v.score for v in viewings] == [1, 7, None, None, None]
    assert len(warnings) == 3


def test_invalid_iso_calendar_date_is_rejected():
    viewings, warnings = parse_rows([HEADER, ["x", 1, "movie", "2026-02-30"]])
    assert viewings == []
    assert warnings[0]["reason"].startswith("unparseable watched_date")


# --- header-based columns ---


def test_columns_are_found_by_header_in_any_order():
    # The live sheet's actual layout: date first, rating before tmdb_id.
    values = [
        ["watched_date", "title", "rating", "tmdb_id", "media_type", "notes"],
        ["2026-10-04", "Tony", "2.5", "1329016", "movie", "private"],
        ["2026-10-21", "Gregory's Girl", "4", "21764", "movie"],
    ]
    viewings, warnings = parse_rows(values)
    assert warnings == []
    assert [(v.row, v.title, v.tmdb_id, v.watched_date, v.score) for v in viewings] == [
        (2, "Tony", 1329016, date(2026, 10, 4), 5),
        (3, "Gregory's Girl", 21764, date(2026, 10, 21), 8),
    ]
    assert "private" not in repr(viewings)


def test_headers_are_case_and_space_insensitive_and_extras_ignored():
    values = [
        ["Watched Date", "Where", " TMDB_ID ", "Title"],
        ["2026-10-04", "cinema", 652, "Troy"],
    ]
    viewings, _ = parse_rows(values)
    assert viewings[0].tmdb_id == 652 and viewings[0].title == "Troy"
    assert viewings[0].media_type == "movie"  # no media_type column at all
    assert viewings[0].score is None


def test_missing_required_header_fails_the_run():
    with pytest.raises(SheetError, match="tmdb_id"):
        parse_rows([["title", "watched_date"], ["Troy", "2026-10-15"]])


def test_duplicate_header_fails_the_run():
    with pytest.raises(SheetError, match="duplicate 'tmdb_id'"):
        parse_rows([["tmdb_id", "watched_date", "tmdb_id"]])


def test_empty_sheet_fails_the_run():
    with pytest.raises(SheetError, match="empty"):
        parse_rows([])


def test_header_only_sheet_is_an_empty_log():
    assert parse_rows([HEADER]) == ([], [])


# --- published optional columns ---


def test_public_note_and_format_are_kept_but_notes_are_not():
    values = [
        ["watched_date", "tmdb_id", "notes", "Public Note", "Format"],
        ["2026-10-04", 652, "private", "  Better than  I remembered ", "Cinema"],
        ["2026-10-05", 653, "", "", ""],
    ]
    viewings, warnings = parse_rows(values)
    assert warnings == []
    assert (viewings[0].public_note, viewings[0].format) == ("Better than I remembered", "Cinema")
    assert (viewings[1].public_note, viewings[1].format) == (None, None)
    assert "private" not in repr(viewings)


def test_overlong_public_note_is_truncated_with_a_warning():
    values = [["watched_date", "tmdb_id", "public_note"], ["2026-10-04", 652, "x" * 400]]
    viewings, warnings = parse_rows(values)
    assert len(viewings[0].public_note) == PUBLIC_NOTE_MAX
    assert viewings[0].public_note.endswith("…")
    assert "truncated" in warnings[0]["reason"]


# --- TV season and episode ---

EPISODE_HEADER = ["watched_date", "tmdb_id", "media_type", "Season", "Episode"]


def test_tv_rows_keep_season_and_episode():
    rows = [
        ["2026-01-01", 1396, "tv", 2, 5],
        ["2026-01-01", 1396, "tv", "3", 1.0],
        ["2026-01-01", 1396, "tv", 0, 1],  # season 0: TMDB's specials
        ["2026-01-01", 1396, "tv", "", ""],
    ]
    viewings, warnings = parse_rows([EPISODE_HEADER, *rows])
    assert warnings == []
    assert [(v.season, v.episode) for v in viewings] == [(2, 5), (3, 1), (0, 1), (None, None)]


def test_bad_or_half_filled_episodes_are_dropped_but_the_viewing_kept():
    rows = [
        ["2026-01-01", 1396, "tv", 2, ""],
        ["2026-01-01", 1396, "tv", "", 5],
        ["2026-01-01", 1396, "tv", 2, 0],
        ["2026-01-01", 1396, "tv", -1, 3],
        ["2026-01-01", 1396, "tv", 1.5, 3],
        ["2026-01-01", 1396, "tv", "two", 3],
        ["2026-01-01", 1396, "tv", True, 3],
        ["2026-01-01", 603, "movie", 1, 1],
    ]
    viewings, warnings = parse_rows([EPISODE_HEADER, *rows])
    assert len(viewings) == len(rows)
    assert all((v.season, v.episode) == (None, None) for v in viewings)
    reasons = {w["row"]: w["reason"] for w in warnings}
    assert set(reasons) == set(range(2, 10))
    assert "go together" in reasons[2] and "go together" in reasons[3]
    assert all("invalid season/episode" in reasons[r] for r in range(4, 9))
    assert "movie row" in reasons[9]


# --- score out of 10, and the old rating column ---

SCORE_HEADER = ["watched_date", "tmdb_id", "score", "rating"]


def test_score_is_a_whole_number_out_of_10():
    rows = [
        ["2026-01-01", 1, 7],
        ["2026-01-01", 2, "10"],
        ["2026-01-01", 3, 1.0],
        ["2026-01-01", 4, 7.5],
        ["2026-01-01", 5, 0],
        ["2026-01-01", 6, 11],
        ["2026-01-01", 7, True],
        ["2026-01-01", 8, "eight"],
    ]
    viewings, warnings = parse_rows([SCORE_HEADER, *rows])
    assert [v.score for v in viewings] == [7, 10, 1, None, None, None, None, None]
    assert all(isinstance(v.score, int) for v in viewings if v.score is not None)
    assert [w["row"] for w in warnings] == [5, 6, 7, 8, 9]
    assert all("invalid score" in w["reason"] for w in warnings)


def test_score_wins_over_rating_and_rating_fills_rows_not_moved_yet():
    rows = [
        ["2026-01-01", 1, 6, 4.5],  # moved across and re-scored: the score counts
        ["2026-01-01", 2, "", 4.5],  # not moved yet: rating doubled
        ["2026-01-01", 3, "", ""],  # unrated
    ]
    viewings, warnings = parse_rows([SCORE_HEADER, *rows])
    assert [v.score for v in viewings] == [6, 9, None]
    assert warnings == []


def test_bad_score_falls_back_to_a_good_rating_with_a_warning():
    viewings, warnings = parse_rows([SCORE_HEADER, ["2026-01-01", 1, 7.5, 3.5]])
    assert viewings[0].score == 7
    assert warnings == [{"row": 2, "reason": "invalid score 7.5, ignored"}]


def test_score_column_alone_needs_no_rating_column():
    viewings, warnings = parse_rows([["watched_date", "tmdb_id", "Score"], ["2026-01-01", 1, 8]])
    assert (viewings[0].score, warnings) == (8, [])
