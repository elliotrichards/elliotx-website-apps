from datetime import UTC, datetime, timedelta

import requests

import tmdb

NOW = datetime(2026, 2, 4, 5, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self._responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class MemoryCache:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})

    def get(self, media_type, tmdb_id):
        return self.entries.get((media_type, tmdb_id), (None, None))

    def put(self, media_type, tmdb_id, data, now):
        self.entries[(media_type, tmdb_id)] = (data, now)


MOVIE = {
    "title": "The Matrix",
    "release_date": "1999-03-30",
    "runtime": 136,
    "genres": [{"id": 28, "name": "Action"}],
    "poster_path": "/m.jpg",
    "original_language": "en",
    "vote_average": 8.2,
    "overview": "dropped by trim",
    "credits": {"crew": [{"job": "Director", "name": "Lana Wachowski"}, {"job": "Writer", "name": "x"}]},
}


def client(responses):
    session = FakeSession(responses)
    return tmdb.TmdbClient("token", session=session, sleep=lambda _: None), session


def test_trim_movie_keeps_only_needed_fields():
    t = tmdb.trim("movie", MOVIE)
    assert t["directors"] == ["Lana Wachowski"]
    assert t["genres"] == ["Action"]
    assert "overview" not in t


def test_trim_tv_splits_combined_genres_and_uses_creators():
    raw = {
        "name": "Show",
        "first_air_date": "2010-01-01",
        "episode_run_time": [],
        "last_episode_to_air": {"runtime": 52},
        "genres": [{"name": "Sci-Fi & Fantasy"}, {"name": "Drama"}],
        "created_by": [{"name": "Creator"}],
    }
    t = tmdb.trim("tv", raw)
    assert t["genres"] == ["Science Fiction", "Fantasy", "Drama"]
    assert t["runtime"] == 52
    assert t["creators"] == ["Creator"] and t["directors"] == []


def test_fetch_retries_rate_limit_then_succeeds():
    c, session = client([FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(503), FakeResponse(200, MOVIE)])
    assert c.fetch("movie", 603)["title"] == "The Matrix"
    assert c.calls == 3
    assert session.urls[0].endswith("/movie/603")


def test_fetch_gives_up_after_max_attempts():
    c, _ = client([requests.ConnectionError("down")] * tmdb.MAX_ATTEMPTS)
    try:
        c.fetch("movie", 603)
    except tmdb.TmdbError as exc:
        assert "gave up" in str(exc)
    else:
        raise AssertionError("expected TmdbError")


def test_enrich_uses_fresh_cache_without_calling_tmdb():
    cache = MemoryCache({("movie", 603): ({"title": "cached"}, NOW - timedelta(days=1))})
    c, _ = client([])
    metadata, warnings, hits = tmdb.enrich([("movie", 603)], c, cache, now=NOW)
    assert metadata[("movie", 603)] == {"title": "cached"}
    assert (warnings, hits, c.calls) == ([], 1, 0)


def test_enrich_falls_back_to_stale_cache_when_tmdb_is_down():
    stale = NOW - tmdb.CACHE_MAX_AGE - timedelta(days=1)
    cache = MemoryCache({("movie", 603): ({"title": "stale"}, stale)})
    c, _ = client([FakeResponse(500)] * tmdb.MAX_ATTEMPTS)
    metadata, warnings, _ = tmdb.enrich([("movie", 603)], c, cache, now=NOW)
    assert metadata[("movie", 603)] == {"title": "stale"}
    assert warnings == []


def test_enrich_warns_when_no_cache_and_tmdb_fails():
    c, _ = client([FakeResponse(404)])
    metadata, warnings, _ = tmdb.enrich([("movie", 1)], c, MemoryCache(), now=NOW)
    assert metadata == {}
    assert warnings == [{"tmdb_id": 1, "media_type": "movie", "reason": "TMDB has no movie with id 1"}]


def test_enrich_writes_trimmed_result_to_cache():
    cache = MemoryCache()
    c, _ = client([FakeResponse(200, MOVIE)])
    tmdb.enrich([("movie", 603)], c, cache, now=NOW)
    data, cached_at = cache.entries[("movie", 603)]
    assert data == tmdb.trim("movie", MOVIE) and cached_at == NOW
