import json
from datetime import UTC, date, datetime

import pytest

from sheet import Viewing, parse_rows
from stats import (
    RECENT_LIMIT,
    TMDB_ATTRIBUTION,
    busiest,
    by_month,
    by_year,
    compute_stats,
    ratings,
    recent,
    streaks,
    viewings_log,
)

TODAY = date(2026, 2, 4)
GENERATED_AT = datetime(2026, 2, 4, 5, 0, 3, tzinfo=UTC)


def v(row, tmdb_id, watched, rating=None, media_type="movie", title="t"):
    return Viewing(row=row, title=title, tmdb_id=tmdb_id, media_type=media_type, watched_date=watched, rating=rating)


@pytest.fixture
def result(sheet_values, metadata):
    viewings, warnings = parse_rows(sheet_values)
    missing = [{"tmdb_id": 999999, "media_type": "movie", "reason": "TMDB has no movie with id 999999"}]
    return compute_stats(viewings, metadata, today=TODAY, generated_at=GENERATED_AT, warnings=warnings + missing)


# --- end to end over the fixture sheet ---


def test_envelope(result):
    assert result["schema_version"] == 1
    assert result["generated_at"] == "2026-02-04T05:00:03Z"
    assert result["attribution"] == TMDB_ATTRIBUTION
    assert any(w.get("tmdb_id") == 999999 for w in result["warnings"])


def test_totals_count_rewatches_and_tv(result):
    t = result["totals"]
    # Valid rows: 2 3 4 6 11 12 15 -> 7 viewings; Spirited Away twice.
    assert t["viewings"] == 7
    assert t["unique"] == 6
    assert t["rewatches"] == 1
    assert t["by_media_type"] == {"movie": 6, "tv": 1}
    # 136 + 125*2 + 133 + 154 + 47 minutes; the TMDB-less film adds nothing.
    assert t["runtime_hours"] == round((136 + 250 + 133 + 154 + 47) / 60, 1)


def test_metadata_breakdowns_skip_unenriched_viewings(result):
    genres = {g["name"]: g["count"] for g in result["genres"]}
    # Spirited Away's genres count once per viewing.
    assert genres["Fantasy"] == 2
    assert genres["Crime"] == 2  # Pulp Fiction + Breaking Bad
    assert result["genres"][0]["count"] >= result["genres"][-1]["count"]

    assert result["directors"][0] == {"name": "Hayao Miyazaki", "count": 2}
    assert result["creators"] == [{"name": "Vince Gilligan", "count": 1}]
    assert {lang["name"] for lang in result["languages"]} == {"en", "ja", "ko"}
    assert result["decades"] == [
        {"decade": 1990, "count": 2},
        {"decade": 2000, "count": 3},
        {"decade": 2010, "count": 1},
    ]


def test_output_is_json_serialisable_and_idempotent(sheet_values, metadata):
    viewings, warnings = parse_rows(sheet_values)
    a = compute_stats(viewings, metadata, today=TODAY, generated_at=GENERATED_AT, warnings=warnings)
    b = compute_stats(viewings, metadata, today=TODAY, generated_at=GENERATED_AT, warnings=warnings)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --- dates: year boundaries and empty months ---


def test_by_month_fills_gaps_across_year_boundary():
    viewings = [v(2, 1, date(2025, 11, 20)), v(3, 2, date(2026, 2, 1))]
    months = by_month(viewings, today=date(2026, 3, 15))
    assert months == [
        {"month": "2025-11", "count": 1},
        {"month": "2025-12", "count": 0},
        {"month": "2026-01", "count": 0},
        {"month": "2026-02", "count": 1},
        {"month": "2026-03", "count": 0},
    ]


def test_by_year_fills_gaps_up_to_this_year():
    viewings = [v(2, 1, date(2022, 6, 1)), v(3, 2, date(2024, 6, 1)), v(4, 3, date(2024, 7, 1))]
    assert by_year(viewings, today=date(2025, 1, 1)) == [
        {"year": 2022, "count": 1},
        {"year": 2023, "count": 0},
        {"year": 2024, "count": 2},
        {"year": 2025, "count": 0},
    ]


def test_empty_log():
    out = compute_stats([], {}, today=TODAY, generated_at=GENERATED_AT)
    assert out["totals"]["viewings"] == 0
    assert out["by_month"] == [] and out["by_year"] == []
    assert out["streaks"]["current"]["days"] == 0
    assert out["busiest"]["month"] is None
    assert out["ratings"] is None
    assert out["recent"] == []


# --- streaks ---


def test_streak_across_month_end():
    days = [date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 1), date(2026, 2, 2)]
    s = streaks(days, today=date(2026, 2, 2))
    assert s["longest"] == {"days": 4, "start": "2026-01-30", "end": "2026-02-02"}
    assert s["current"] == s["longest"]


def test_streak_across_year_end_and_same_day_rewatches():
    days = [date(2025, 12, 31), date(2025, 12, 31), date(2026, 1, 1)]
    assert streaks(days, today=date(2026, 6, 1))["longest"]["days"] == 2


def test_current_streak_survives_until_end_of_next_day():
    days = [date(2026, 2, 2), date(2026, 2, 3)]
    assert streaks(days, today=date(2026, 2, 4))["current"]["days"] == 2
    assert streaks(days, today=date(2026, 2, 5))["current"] == {"days": 0, "start": None, "end": None}


def test_longest_streak_tie_goes_to_earliest():
    days = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 10), date(2026, 1, 11)]
    assert streaks(days, today=date(2026, 3, 1))["longest"]["start"] == "2026-01-01"


def test_fixture_streaks(result):
    # 2025-12-30 .. 2026-01-02 is four straight days; last watch 02-03, today 02-04.
    assert result["streaks"]["longest"] == {"days": 4, "start": "2025-12-30", "end": "2026-01-02"}
    assert result["streaks"]["current"] == {"days": 2, "start": "2026-02-02", "end": "2026-02-03"}


# --- busiest, ratings, recent ---


def test_busiest_month_and_weekday():
    viewings = [
        v(2, 1, date(2026, 1, 5)),  # Monday
        v(3, 2, date(2026, 1, 12)),  # Monday
        v(4, 3, date(2026, 2, 7)),  # Saturday
    ]
    b = busiest(viewings)
    assert b["month"] == {"month": "2026-01", "count": 2}
    assert b["weekday"] == {"day": "Monday", "count": 2}
    assert len(b["weekdays"]) == 7


def test_ratings_distribution_and_average():
    viewings = [v(2, 1, TODAY, 4.5), v(3, 2, TODAY, 3), v(4, 3, TODAY, None), v(5, 4, TODAY, 4.5)]
    r = ratings(viewings)
    assert r["count"] == 3
    assert r["average"] == 4.0
    dist = {d["rating"]: d["count"] for d in r["distribution"]}
    assert dist[4.5] == 2 and dist[3.0] == 1 and dist[0.5] == 0
    assert len(r["distribution"]) == 10


def test_ratings_absent_when_nothing_rated():
    assert ratings([v(2, 1, TODAY)]) is None


def test_recent_is_newest_first_with_metadata_and_fallback(result):
    rec = result["recent"]
    assert rec[0]["title"] == "Lost to TMDB"  # sheet title when TMDB had nothing
    assert rec[0]["poster_path"] is None and rec[0]["year"] is None
    assert rec[1] == {
        "title": "Breaking Bad",
        "year": 2008,
        "poster_path": "/bb.jpg",
        "watched_date": "2026-02-02",
        "rating": 4.5,
        "tmdb_id": 1396,
        "media_type": "tv",
    }
    dates = [r["watched_date"] for r in rec]
    assert dates == sorted(dates, reverse=True)


def test_recent_limit_and_same_day_order():
    viewings = [v(row, row, date(2026, 1, 1)) for row in range(2, 2 + RECENT_LIMIT + 5)]
    rec = recent(viewings, {})
    assert len(rec) == RECENT_LIMIT
    assert rec[0]["tmdb_id"] == 2 + RECENT_LIMIT + 4  # furthest down the sheet first


def test_viewings_log_lists_every_viewing_newest_first(sheet_values, metadata):
    viewings, _ = parse_rows(sheet_values)
    log = viewings_log(viewings, metadata, generated_at=GENERATED_AT)

    assert log["schema_version"] == 1
    assert log["generated_at"] == "2026-02-04T05:00:03Z"
    assert log["count"] == len(viewings) == len(log["viewings"])
    dates = [v["watched_date"] for v in log["viewings"]]
    assert dates == sorted(dates, reverse=True)
    # Same shape as `recent`, which is just the head of the log.
    assert log["viewings"][:RECENT_LIMIT] == recent(viewings, metadata)
    assert all("notes" not in v for v in log["viewings"])


def test_people_with_ids_count_by_name(metadata):
    meta = {**metadata, ("movie", 603): {**metadata[("movie", 603)], "directors": [{"id": 9339, "name": "Lana W"}]}}
    out = compute_stats([v(2, 603, date(2026, 1, 1))], meta, today=TODAY, generated_at=GENERATED_AT)
    assert out["directors"] == [{"name": "Lana W", "count": 1}]
