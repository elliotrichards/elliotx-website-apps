from datetime import UTC, date, datetime, timedelta

import requests

import openlibrary
from sheet import Read

NOW = datetime(2026, 10, 31, 5, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession:
    """Answers by URL path, so lookups are tested by what they ask for, not the order."""

    def __init__(self, routes):
        self.headers = {}
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.requests = []

    def get(self, url, params=None, **kwargs):
        path = url.removeprefix(openlibrary.API_BASE)
        self.requests.append((path, params))
        queue = self.routes.get(path) or [FakeResponse(404)]
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        return r


def client(routes):
    session = FakeSession(routes)
    return openlibrary.OpenLibraryClient(session=session, sleep=lambda s: None), session


EDITION = {"works": [{"key": "/works/OL1W"}], "covers": [-1, 111], "number_of_pages": 352}
WORK = {"covers": [222], "first_publish_date": "July 2015", "subjects": ["Fiction", "nyt:x=1", "Thrillers"]}


def read(title="In a Dark, Dark Wood", author="Ruth Ware", isbn=None):
    return Read(row=2, title=title, author=author, finished_date=date(2026, 10, 12), score=5, isbn=isbn)


def test_lookup_key_prefers_isbn_and_normalises_title_and_author():
    assert openlibrary.lookup_key(read(isbn="9780099598244")) == "isbn/9780099598244"
    assert openlibrary.lookup_key(read(title="In a  Dark, DARK Wood")) == "title/in a dark, dark wood|ruth ware"
    assert openlibrary.lookup_key(read(author=None)) == "title/in a dark, dark wood|"


def test_client_identifies_itself():
    c, session = client({})
    assert session.headers["User-Agent"] == openlibrary.USER_AGENT


def test_by_isbn_uses_edition_then_work():
    c, session = client(
        {"/isbn/9780099598244.json": FakeResponse(200, EDITION), "/works/OL1W.json": FakeResponse(200, WORK)}
    )
    data = openlibrary.lookup(c, "isbn/9780099598244", "T", "A")
    assert data == {
        "work_key": "OL1W",
        "cover_id": 111,  # the edition's own cover, skipping the removed -1
        "first_publish_year": 2015,
        "pages": 352,
        "subjects": ["Fiction", "Thrillers"],
    }
    assert [p for p, _ in session.requests] == ["/isbn/9780099598244.json", "/works/OL1W.json"]


def test_by_isbn_fills_gaps_from_a_search_by_work_key():
    edition = {"works": [{"key": "/works/OL1W"}]}
    doc = {"key": "/works/OL1W", "cover_i": 333, "first_publish_year": 2015, "number_of_pages_median": 340}
    c, session = client(
        {
            "/isbn/9780099598244.json": FakeResponse(200, edition),
            "/works/OL1W.json": FakeResponse(200, {"subjects": ["Mystery"]}),
            "/search.json": FakeResponse(200, {"docs": [doc]}),
        }
    )
    data = openlibrary.lookup(c, "isbn/9780099598244", "T", "A")
    assert data == {
        "work_key": "OL1W",
        "cover_id": 333,
        "first_publish_year": 2015,
        "pages": 340,
        "subjects": ["Mystery"],
    }
    assert session.requests[-1][1]["q"] == "key:/works/OL1W"


def test_unknown_isbn_falls_back_to_title_search():
    doc = {"key": "/works/OL9W", "cover_i": 9, "first_publish_year": 2019, "subject": ["Robots"]}
    c, session = client({"/search.json": FakeResponse(200, {"docs": [doc]})})
    data = openlibrary.lookup(c, "isbn/0000000000", "Klara and the Sun", "Kazuo Ishiguro")
    assert data["work_key"] == "OL9W" and data["pages"] is None
    assert session.requests[-1][1]["title"] == "Klara and the Sun"
    assert session.requests[-1][1]["author"] == "Kazuo Ishiguro"


def test_search_with_no_docs_is_no_match():
    c, session = client({"/search.json": FakeResponse(200, {"docs": []})})
    assert openlibrary.lookup(c, "title/x|", "x", None) is None
    assert "author" not in session.requests[0][1]


def test_retries_server_errors_then_gives_up():
    c, session = client({"/search.json": [FakeResponse(503), requests.ConnectionError("down"), FakeResponse(500)]})
    try:
        openlibrary.lookup(c, "title/x|", "x", None)
    except openlibrary.OpenLibraryError as exc:
        assert "gave up" in str(exc)
    else:
        raise AssertionError("expected OpenLibraryError")
    assert c.calls == openlibrary.MAX_ATTEMPTS


def test_trim_subjects_drops_noise_and_repeats():
    subjects = ["fiction", "Fiction", "In library", "nyt:list=1", "x" * 41, "Thrillers", "a", "b", "c", "d"]
    assert openlibrary.trim_subjects(subjects) == ["Fiction", "Thrillers", "A", "B", "C"]


class MemoryCache:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.saves = 0

    def load(self):
        return dict(self.entries)

    def save(self, entries):
        self.entries = dict(entries)
        self.saves += 1


def patch_lookup(monkeypatch, answers):
    asked = []

    def fake(client, key, title, author):
        asked.append(key)
        answer = answers[key]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(openlibrary, "lookup", fake)
    return asked


HIT = {"work_key": "OL1W", "cover_id": 1, "first_publish_year": 2015, "pages": 300, "subjects": []}


def test_enrich_skips_fresh_entries_and_caches_misses(monkeypatch):
    asked = patch_lookup(monkeypatch, {"isbn/1": HIT, "title/b|": None})
    cache = MemoryCache({"isbn/1": (HIT, NOW - timedelta(days=1))})
    found, warnings, hits = openlibrary.enrich({"isbn/1": ("A", None), "title/b|": ("B", None)}, None, cache, now=NOW)
    assert asked == ["title/b|"]
    assert found == {"isbn/1": HIT, "title/b|": None}
    assert cache.entries["title/b|"] == (None, NOW)
    assert warnings == [] and hits == 1


def test_enrich_retries_misses_sooner_than_matches(monkeypatch):
    asked = patch_lookup(monkeypatch, {"isbn/1": HIT, "title/b|": None})
    eight_days = NOW - timedelta(days=8)
    cache = MemoryCache({"isbn/1": (HIT, eight_days), "title/b|": (None, eight_days)})
    openlibrary.enrich({"isbn/1": ("A", None), "title/b|": ("B", None)}, None, cache, now=NOW)
    assert asked == ["title/b|"]


def test_enrich_keeps_stale_copy_when_a_refresh_fails(monkeypatch):
    patch_lookup(
        monkeypatch, {"isbn/1": openlibrary.OpenLibraryError("down"), "isbn/2": openlibrary.OpenLibraryError("down")}
    )
    cache = MemoryCache({"isbn/1": (HIT, NOW - timedelta(days=40))})
    found, warnings, _ = openlibrary.enrich({"isbn/1": ("A", None), "isbn/2": ("B", "X")}, None, cache, now=NOW)
    assert found == {"isbn/1": HIT}
    assert warnings == [{"book": "isbn/2", "title": "B", "author": "X", "reason": "down"}]
    assert cache.saves == 0


def test_enrich_defers_past_the_budget(monkeypatch):
    patch_lookup(monkeypatch, {f"isbn/{i}": HIT for i in range(5)})
    cache = MemoryCache()
    found, warnings, _ = openlibrary.enrich(
        {f"isbn/{i}": ("T", None) for i in range(5)}, None, cache, now=NOW, max_lookups=2
    )
    assert len(found) == 2
    assert "3 books are waiting" in warnings[0]["reason"]


def test_gcs_cache_round_trips_and_treats_old_versions_as_stalest():
    class Blob:
        def __init__(self, store):
            self.store = store

        def download_as_text(self):
            return self.store["body"]

        def upload_from_string(self, body, content_type):
            self.store["body"] = body

    class Bucket:
        def __init__(self):
            self.store = {}

        def blob(self, name):
            assert name == openlibrary.GcsCache.INDEX_PATH
            return Blob(self.store)

    bucket = Bucket()
    cache = openlibrary.GcsCache(bucket)
    cache.save({"isbn/1": (HIT, NOW), "title/b|": (None, NOW)})
    assert cache.load() == {"isbn/1": (HIT, NOW), "title/b|": (None, NOW)}

    bucket.store["body"] = bucket.store["body"].replace('"version":1', '"version":0')
    assert cache.load()["isbn/1"][1] == datetime.min.replace(tzinfo=UTC)

    bucket.store["body"] = "not json"
    assert cache.load() == {}
