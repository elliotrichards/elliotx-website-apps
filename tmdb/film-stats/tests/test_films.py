from datetime import UTC, date, datetime

from films import SCHEMA_VERSION, TMDB_ATTRIBUTION, export_film, export_viewings, films_export
from sheet import Viewing, parse_rows

GENERATED_AT = datetime(2026, 2, 4, 5, 0, 3, tzinfo=UTC)


def v(row, tmdb_id, watched, score=None, media_type="movie", title="t", **extra):
    return Viewing(
        row=row, title=title, tmdb_id=tmdb_id, media_type=media_type, watched_date=watched, score=score, **extra
    )


def test_envelope_and_every_title_once(sheet_values, metadata):
    viewings, _ = parse_rows(sheet_values)
    out = films_export(viewings, metadata, generated_at=GENERATED_AT, warnings=[{"reason": "x"}])
    assert out["schemaVersion"] == SCHEMA_VERSION == 3
    assert out["generatedAt"] == "2026-02-04T05:00:03Z"
    assert out["attribution"] == TMDB_ATTRIBUTION
    assert out["warnings"] == [{"reason": "x"}]
    assert len(out["viewings"]) == len(viewings)
    assert set(out["films"]) == {"movie/603", "movie/129", "movie/496243", "movie/680", "tv/1396", "movie/999999"}


def test_title_missing_from_tmdb_falls_back_to_the_sheet(sheet_values, metadata):
    viewings, _ = parse_rows(sheet_values)
    lost = films_export(viewings, metadata, generated_at=GENERATED_AT)["films"]["movie/999999"]
    assert lost["title"] == "Lost to TMDB"
    assert lost["posterPath"] is None and lost["genres"] == [] and lost["directors"] == []


def test_viewings_are_oldest_first_whatever_the_sheet_order():
    # Backfilled rows are appended at the bottom of the sheet with old dates.
    rows = [v(2, 1, date(2026, 10, 1)), v(3, 2, date(2019, 5, 4)), v(4, 3, date(2026, 9, 1))]
    assert [r["watchedOn"] for r in export_viewings(rows)] == ["2019-05-04", "2026-09-01", "2026-10-01"]


def test_order_within_a_day_follows_the_sheet_rows():
    rows = [v(9, 2, date(2026, 8, 31)), v(4, 1, date(2026, 8, 31)), v(5, 3, date(2026, 9, 1))]
    out = export_viewings(rows)
    assert [(r["tmdbId"], r["order"]) for r in out] == [(1, 1), (2, 2), (3, 1)]


def test_rewatches_and_same_day_repeats_get_distinct_ids():
    rows = [
        v(2, 603, date(2026, 1, 1)),
        v(3, 603, date(2026, 3, 1)),
        v(4, 603, date(2026, 3, 1)),
        v(5, 603, date(2026, 3, 1), media_type="tv"),
    ]
    out = export_viewings(rows)
    assert [r["rewatch"] for r in out] == [False, True, True, False]
    assert [r["id"] for r in out] == [
        "2026-01-01-movie-603",
        "2026-03-01-movie-603",
        "2026-03-01-movie-603-2",
        "2026-03-01-tv-603",
    ]


def test_viewing_shape_is_camel_case_with_note_and_format():
    [row] = export_viewings([v(2, 652, date(2026, 10, 15), score=6, public_note="Fun", format="Home")])
    assert row == {
        "id": "2026-10-15-movie-652",
        "mediaType": "movie",
        "tmdbId": 652,
        "watchedOn": "2026-10-15",
        "order": 1,
        "score": 6,
        "rewatch": False,
        "note": "Fun",
        "format": "Home",
    }


def test_film_people_from_entries_cached_before_ids_keep_their_names(metadata):
    film = export_film(metadata[("movie", 603)])
    assert film["directors"] == [{"id": None, "name": "Lana Wachowski"}, {"id": None, "name": "Lilly Wachowski"}]
    assert film["title"] == "The Matrix" and film["tmdbVote"] == 8.2


def test_film_maps_new_fields_to_camel_case():
    trimmed = {
        "media_type": "movie",
        "title": "Troy",
        "original_title": "Troy",
        "cast": [{"id": 1, "name": "Brad Pitt", "character": "Achilles", "profile_path": "/b.jpg"}],
        "directors": [{"id": 2, "name": "Wolfgang Petersen"}],
        "collection": None,
        "vote_count": 9000,
        "backdrop_path": "/bd.jpg",
    }
    film = export_film(trimmed)
    assert film["cast"] == [{"id": 1, "name": "Brad Pitt", "character": "Achilles", "profilePath": "/b.jpg"}]
    assert film["directors"] == [{"id": 2, "name": "Wolfgang Petersen"}]
    assert (film["tmdbVoteCount"], film["backdropPath"], film["mediaType"]) == (9000, "/bd.jpg", "movie")
