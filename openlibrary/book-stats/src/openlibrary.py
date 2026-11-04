"""Open Library metadata lookup, fronted by a GCS cache of trimmed results."""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import requests
from google.api_core import exceptions as gcs_exceptions

API_BASE = "https://openlibrary.org"
# Open Library asks API clients to identify themselves; no key is needed.
USER_AGENT = "elliotx-book-stats/1.0 (+https://elliotx.com)"
MAX_ATTEMPTS = 4
# Open Library has no published rate limit but asks for restraint; with a
# User-Agent set it tolerates a few requests a second. Three workers with a
# small gap each stays around there.
POLITE_DELAY_SECONDS = 0.35
FETCH_WORKERS = 3

# Cap on lookups per run (each one is up to three requests), so a backfill
# can't push a run past the service's 300s timeout. Open Library is slow, a
# second or so per request, so ~150 lookups is ~150s across the workers.
# Anything over the cap waits for a later run (see enrich).
MAX_LOOKUPS_PER_RUN = 150

# Bumped whenever trim() keeps new fields. Entries cached under an older
# version are treated as the stalest of all: still used, but refreshed first.
TRIM_VERSION = 2

# Matches are refreshed after this long, for covers and other details added since.
# A miss is retried sooner: it may be a typo since fixed in the sheet (which
# changes the key anyway), or a book Open Library has added since.
CACHE_MAX_AGE = timedelta(days=30)
MISS_MAX_AGE = timedelta(days=7)

# A blurb is shown whole up to this length; longer ones are cut at a sentence.
DESCRIPTION_LIMIT = 1500
PUBLISHER_MAX_LENGTH = 60

SEARCH_FIELDS = "key,cover_i,first_publish_year,number_of_pages_median"


class OpenLibraryError(Exception):
    pass


class OpenLibraryNotFound(OpenLibraryError):
    pass


class OpenLibraryClient:
    def __init__(self, session=None, sleep=time.sleep):
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._sleep = sleep
        self._lock = threading.Lock()
        self.calls = 0

    def edition(self, isbn):
        """GET /isbn/{isbn}.json, which redirects to the edition record."""
        return self._get(f"{API_BASE}/isbn/{isbn}.json", {}, what=f"isbn {isbn}")

    def work(self, work_key):
        """GET /works/OL…W.json."""
        return self._get(f"{API_BASE}/works/{work_key}.json", {}, what=f"work {work_key}")

    def search(self, **params):
        """GET /search.json; returns the first doc, or None."""
        docs = self._get(
            f"{API_BASE}/search.json",
            {**params, "limit": 1, "fields": SEARCH_FIELDS},
            what=f"search {params}",
        ).get("docs", [])
        return docs[0] if docs else None

    def _get(self, url, params, *, what):
        """Retries 429/5xx and network errors with backoff; 404 is final."""
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._sleep(POLITE_DELAY_SECONDS)
            with self._lock:  # lookups run on several threads at once
                self.calls += 1
            try:
                r = self._session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                if attempt < MAX_ATTEMPTS:
                    self._sleep(2**attempt)
                continue

            if r.status_code == 404:
                raise OpenLibraryNotFound(f"Open Library has no {what}")
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                if attempt < MAX_ATTEMPTS:
                    self._sleep(min(_retry_after(r) or 2**attempt, 30))
                continue
            if not r.ok:
                raise OpenLibraryError(f"HTTP {r.status_code} for {what}")
            return r.json()

        raise OpenLibraryError(f"gave up on {what} after {MAX_ATTEMPTS} attempts ({last_error})")


def lookup_key(read):
    """
    The cache key for a read: "isbn/9780099598244", or for a row with no
    ISBN "title/in a dark, dark wood|ruth ware" (lower case, spaces
    collapsed, so a re-typed row with different capitals still hits).
    """
    if read.isbn:
        return f"isbn/{read.isbn}"
    return f"title/{_norm(read.title)}|{_norm(read.author or '')}"


def lookup(client, key, title, author):
    """
    Trimmed metadata for one key, or None if Open Library has no match.

    By ISBN: the edition, then its work, then (only if the edition and work
    between them lack a cover, page count or first-publish year) a search by
    work key, which has all three. An ISBN Open Library doesn't know falls
    back to a title and author search. Without an ISBN it's that search alone.
    """
    if key.startswith("isbn/"):
        try:
            return _by_isbn(client, key.removeprefix("isbn/"))
        except OpenLibraryNotFound:
            pass
    params = {"title": title}
    if author:
        params["author"] = author
    doc = client.search(**params)
    return trim_search(doc) if doc else None


def _by_isbn(client, isbn):
    edition = client.edition(isbn)
    works = edition.get("works") or []
    work_key = _olid(works[0].get("key")) if works else None
    work = client.work(work_key) if work_key else {}

    data = {
        "work_key": work_key,
        "cover_id": _first_cover(edition.get("covers")) or _first_cover(work.get("covers")),
        "first_publish_year": _year(work.get("first_publish_date")),
        "pages": _positive(edition.get("number_of_pages")),
        "description": trim_description(work.get("description")),
        "publisher": trim_publisher(edition.get("publishers")),
        "edition_year": _year(edition.get("publish_date")),
    }
    if work_key and None in (data["cover_id"], data["first_publish_year"], data["pages"]):
        doc = client.search(q=f"key:/works/{work_key}")
        if doc:
            found = trim_search(doc)
            for field in ("cover_id", "first_publish_year", "pages"):
                data[field] = data[field] or found[field]
    return data


def trim_search(doc):
    """A search.json doc in the same shape as a by-ISBN lookup."""
    return {
        "work_key": _olid(doc.get("key")),
        "cover_id": _first_cover([doc.get("cover_i")]),
        "first_publish_year": _positive(doc.get("first_publish_year")),
        "pages": _positive(doc.get("number_of_pages_median")),
    }


def trim_description(value):
    """
    A work's blurb as plain paragraphs separated by blank lines, or None.

    Open Library stores a string or {"type": ..., "value": ...}, often with
    markdown: links, "([source][1])" footnotes and a "----------" rule
    followed by catalogue extras ("Contains: ..."). Keeps the text before any
    rule, turns links into their text, drops footnotes, and cuts at a
    sentence once past DESCRIPTION_LIMIT.
    """
    if isinstance(value, dict):
        value = value.get("value")
    if not isinstance(value, str):
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = re.split(r"\n\s*-{3,}", text, maxsplit=1)[0]
    text = re.sub(r"^\s*\[\d+\]:.*$", "", text, flags=re.M)  # footnote definitions
    text = re.sub(r"\(\s*\[[^\]]*\]\[\d+\]\s*\)", "", text)  # ([source][1])
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [text](url)
    text = re.sub(r"\[([^\]]+)\]\[\d+\]", r"\1", text)  # [text][1]
    text = re.sub(r"[*_`]{1,3}", "", text)
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    text = "\n\n".join(p for p in paragraphs if p)
    if len(text) > DESCRIPTION_LIMIT:
        cut = text[:DESCRIPTION_LIMIT]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind(".\n"))
        text = cut[: end + 1] if end > DESCRIPTION_LIMIT // 2 else cut.rsplit(" ", 1)[0] + "…"
    return text or None


def trim_publisher(publishers):
    """The edition's first publisher, or None. Open Library lists them as plain strings."""
    for raw in publishers or []:
        if isinstance(raw, str):
            s = " ".join(raw.split())
            if s and len(s) <= PUBLISHER_MAX_LENGTH:
                return s
    return None


class GcsCache:
    """
    One index object, openlibrary/index.json, holding every trimmed result
    (or null for no match): read once and written once per run.
    """

    INDEX_PATH = "openlibrary/index.json"

    def __init__(self, bucket):
        self._bucket = bucket

    def load(self):
        """{key: (data, cached_at)}; empty if there's no index yet or it's unreadable."""
        try:
            raw = json.loads(self._bucket.blob(self.INDEX_PATH).download_as_text())
            return {k: (v["data"], _cached_at(v)) for k, v in raw.items()}
        except gcs_exceptions.NotFound:
            return {}
        except (ValueError, KeyError, TypeError, AttributeError):
            # A corrupt index is just a cold cache; this run rewrites it.
            return {}

    def save(self, entries):
        body = {
            key: {"cached_at": at.isoformat(), "version": TRIM_VERSION, "data": data}
            for key, (data, at) in sorted(entries.items())
        }
        self._bucket.blob(self.INDEX_PATH).upload_from_string(
            json.dumps(body, separators=(",", ":"), ensure_ascii=False),
            content_type="application/json",
        )


def refresh_keys(spec, wanted):
    """
    The keys in `wanted` that a manual refresh asks for. `spec` is a comma-
    separated string of ISBNs (dashes and spaces allowed), full cache keys
    ("isbn/9780099598244", "title/..."), or "all". Anything that isn't a
    wanted book is ignored.
    """
    items = [i.strip() for i in (spec or "").split(",") if i.strip()]
    if "all" in (i.lower() for i in items):
        return set(wanted)
    keys = set()
    for item in items:
        digits = re.sub(r"[^0-9Xx]", "", item).upper()
        keys.add(item if item in wanted else f"isbn/{digits}")
    return keys & set(wanted)


def enrich(wanted, client, cache, now=None, max_lookups=MAX_LOOKUPS_PER_RUN, workers=FETCH_WORKERS, refresh=()):
    """
    Returns ({key: trimmed metadata or None}, warnings, cache_hits) for
    `wanted`, {key: (title, author)}. A key that's absent from the result
    has never been looked up successfully; None means "looked up, no match".

    Fresh cache entries skip Open Library entirely. Stale ones are used as
    they are and refreshed if there's room in this run's budget, oldest
    first, after keys with no entry at all. A failed lookup keeps any stale
    copy; a key with neither gets a warning. Keys that didn't fit in the
    budget get one combined warning and are picked up by later runs.

    `refresh` is a set of keys to re-fetch now even if their cache entry is
    fresh (see refresh_keys). They go first in the queue, still within the
    budget, and a failed refresh keeps the cached copy like any other.
    """
    now = now or datetime.now(UTC)
    entries = cache.load()
    found, missing, stale, forced = {}, [], [], []

    for key in sorted(wanted):
        entry = entries.get(key)
        if entry is None:
            missing.append(key)
            continue
        data, cached_at = entry
        found[key] = data
        if key in refresh:
            forced.append(key)
        elif now - cached_at >= (CACHE_MAX_AGE if data is not None else MISS_MAX_AGE):
            stale.append(key)

    stale.sort(key=lambda k: entries[k][1])
    todo = (forced + missing + stale)[: max(max_lookups, 0)]
    deferred = len([k for k in missing if k not in todo])

    def attempt(key):
        try:
            return lookup(client, key, *wanted[key]), None
        except OpenLibraryError as exc:
            return None, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(attempt, todo))

    fetched, warnings = 0, []
    for key, (data, exc) in zip(todo, results, strict=True):
        if exc is None:
            found[key] = data
            entries[key] = (data, now)
            fetched += 1
        elif key not in found:
            title, author = wanted[key]
            warnings.append({"book": key, "title": title, "author": author, "reason": str(exc)})

    if fetched:
        cache.save(entries)
    if deferred:
        warnings.append(
            {
                "reason": f"{deferred} books are waiting for an Open Library lookup "
                f"(at most {max_lookups} per run); later runs will fill them in"
            }
        )
    return found, warnings, len(found) - fetched


def _cached_at(entry):
    """An entry trimmed by an older trim() counts as cached at the epoch, so it's refreshed before any other."""
    if entry.get("version") != TRIM_VERSION:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(entry["cached_at"])


def _norm(text):
    return " ".join(text.lower().split())


def _olid(key):
    """ "/works/OL20150260W" → "OL20150260W"."""
    return key.rsplit("/", 1)[-1] if isinstance(key, str) and key else None


def _first_cover(covers):
    # Open Library uses -1 for a removed cover.
    return next((c for c in covers or [] if isinstance(c, int) and c > 0), None)


def _positive(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _year(text):
    """The year out of free-text dates like "2018", "May 02, 2019" or "1st edition, 1967"."""
    match = re.search(r"\b(\d{4})\b", text) if isinstance(text, str) else None
    return int(match.group(1)) if match else None


def _retry_after(response):
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
