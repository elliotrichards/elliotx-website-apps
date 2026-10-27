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
        self.saves = 0

    def load(self):
        return dict(self.entries)

    def save(self, entries):
        self.entries = dict(entries)
        self.saves += 1


class FakeClient:
    """Stands in for TmdbClient in enrich tests: answers by title, so worker order doesn't matter."""

    def __init__(self, answers=None, default=None):
        self.answers = answers or {}
        self.default = default
        self.fetched = []

    def fetch(self, media_type, tmdb_id):
        self.fetched.append((media_type, tmdb_id))
        answer = self.answers.get((media_type, tmdb_id), self.default)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise tmdb.TmdbNotFound(f"TMDB has no {media_type} with id {tmdb_id}")
        return answer


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
    client = FakeClient()
    metadata, warnings, hits = tmdb.enrich([("movie", 603)], client, cache, now=NOW)
    assert metadata[("movie", 603)] == {"title": "cached"}
    assert (warnings, hits, client.fetched, cache.saves) == ([], 1, [], 0)


def test_enrich_falls_back_to_stale_cache_when_tmdb_is_down():
    stale = NOW - tmdb.CACHE_MAX_AGE - timedelta(days=1)
    cache = MemoryCache({("movie", 603): ({"title": "stale"}, stale)})
    client = FakeClient(default=tmdb.TmdbError("HTTP 500"))
    metadata, warnings, hits = tmdb.enrich([("movie", 603)], client, cache, now=NOW)
    assert client.fetched == [("movie", 603)]  # it did try to refresh
    assert metadata[("movie", 603)] == {"title": "stale"}
    assert (warnings, hits, cache.saves) == ([], 1, 0)


def test_enrich_refreshes_stale_entries():
    stale = NOW - tmdb.CACHE_MAX_AGE - timedelta(days=1)
    cache = MemoryCache({("movie", 603): ({"title": "stale"}, stale)})
    metadata, _, hits = tmdb.enrich([("movie", 603)], FakeClient(default=MOVIE), cache, now=NOW)
    assert metadata[("movie", 603)]["title"] == "The Matrix"
    assert cache.entries[("movie", 603)][1] == NOW
    assert hits == 0


def test_enrich_warns_when_no_cache_and_tmdb_fails():
    metadata, warnings, _ = tmdb.enrich([("movie", 1)], FakeClient(), MemoryCache(), now=NOW)
    assert metadata == {}
    assert warnings == [{"tmdb_id": 1, "media_type": "movie", "reason": "TMDB has no movie with id 1"}]


def test_enrich_writes_trimmed_results_to_the_cache_once():
    cache = MemoryCache()
    keys = [("movie", i) for i in range(1, 6)]
    tmdb.enrich(keys, FakeClient(default=MOVIE), cache, now=NOW)
    assert cache.saves == 1
    assert cache.entries[("movie", 3)] == (tmdb.trim("movie", MOVIE), NOW)


def test_enrich_caps_lookups_per_run_and_defers_the_rest():
    keys = [("movie", i) for i in range(1, 11)]
    cache = MemoryCache()
    client = FakeClient(default=MOVIE)
    metadata, warnings, _ = tmdb.enrich(keys, client, cache, now=NOW, max_fetches=4)

    assert len(client.fetched) == 4 and len(metadata) == 4
    assert warnings == [
        {"reason": "6 titles are waiting for a TMDB lookup (at most 4 per run); later runs will fill them in"}
    ]

    # The next run picks up where this one stopped.
    client = FakeClient(default=MOVIE)
    metadata, warnings, hits = tmdb.enrich(keys, client, cache, now=NOW, max_fetches=4)
    assert (len(metadata), hits, len(client.fetched)) == (8, 4, 4)


def test_enrich_spends_the_budget_on_missing_titles_before_stale_ones():
    old, older = NOW - timedelta(days=40), NOW - timedelta(days=60)
    cache = MemoryCache({("movie", 1): ({"title": "a"}, old), ("movie", 2): ({"title": "b"}, older)})
    keys = [("movie", 1), ("movie", 2), ("movie", 3)]
    client = FakeClient(default=MOVIE)
    metadata, warnings, _ = tmdb.enrich(keys, client, cache, now=NOW, max_fetches=2)

    # Missing title first, then the oldest stale entry.
    assert sorted(client.fetched) == [("movie", 2), ("movie", 3)]
    assert metadata[("movie", 1)] == {"title": "a"}  # stale, still used
    assert warnings == []  # nothing was left without metadata


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def download_as_text(self):
        if self.name not in self.store:
            raise tmdb.gcs_exceptions.NotFound("no such object")
        return self.store[self.name]

    def upload_from_string(self, body, content_type):
        self.store[self.name] = body


class FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, name):
        return FakeBlob(self.store, name)


def test_gcs_cache_round_trips_one_index_object():
    bucket = FakeBucket()
    cache = tmdb.GcsCache(bucket)
    assert cache.load() == {}  # no index yet

    cache.save({("movie", 603): ({"title": "x"}, NOW), ("tv", 1396): ({"title": "y"}, NOW)})
    assert list(bucket.store) == [tmdb.GcsCache.INDEX_PATH]
    assert cache.load() == {("movie", 603): ({"title": "x"}, NOW), ("tv", 1396): ({"title": "y"}, NOW)}


def test_gcs_cache_treats_a_corrupt_index_as_empty():
    bucket = FakeBucket()
    bucket.store[tmdb.GcsCache.INDEX_PATH] = "{not json"
    assert tmdb.GcsCache(bucket).load() == {}
