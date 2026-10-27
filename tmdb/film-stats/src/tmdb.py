"""TMDB metadata lookup, fronted by a GCS cache of trimmed responses."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import requests
from google.api_core import exceptions as gcs_exceptions

API_BASE = "https://api.themoviedb.org/3"
MAX_ATTEMPTS = 4
# TMDB's documented ceiling is ~50 req/s. A small gap per request across a
# few workers stays well under it (~15 req/s at typical latency).
POLITE_DELAY_SECONDS = 0.1
FETCH_WORKERS = 4

# Cap on TMDB lookups per run, so a big backfill can't push a run past the
# service's 120s timeout. ~600 lookups is ~40s across the workers above;
# anything over the cap waits for a later run (see enrich).
MAX_FETCHES_PER_RUN = 600

# Cached entries are refreshed after this long so vote averages and posters
# don't go permanently stale. A failed refresh falls back to the stale copy.
CACHE_MAX_AGE = timedelta(days=30)

# TV genres are TMDB's combined labels; split them into the movie genre names
# so a "Sci-Fi & Fantasy" show and a "Science Fiction" film count together.
TV_GENRE_SPLIT = {
    "Action & Adventure": ["Action", "Adventure"],
    "Sci-Fi & Fantasy": ["Science Fiction", "Fantasy"],
    "War & Politics": ["War", "Politics"],
}


class TmdbError(Exception):
    pass


class TmdbNotFound(TmdbError):
    pass


class TmdbClient:
    def __init__(self, token, session=None, sleep=time.sleep):
        self._session = session or requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self._sleep = sleep
        self._lock = threading.Lock()
        self.calls = 0

    def fetch(self, media_type, tmdb_id):
        """GET /{movie|tv}/{id} with credits. Retries 429/5xx and network errors with backoff; 404 is final."""
        url = f"{API_BASE}/{media_type}/{tmdb_id}"
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._sleep(POLITE_DELAY_SECONDS)
            with self._lock:  # fetch runs on several threads at once
                self.calls += 1
            try:
                r = self._session.get(url, params={"append_to_response": "credits"}, timeout=20)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                if attempt < MAX_ATTEMPTS:
                    self._sleep(2**attempt)
                continue

            if r.status_code == 404:
                raise TmdbNotFound(f"TMDB has no {media_type} with id {tmdb_id}")
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                if attempt < MAX_ATTEMPTS:
                    self._sleep(min(_retry_after(r) or 2**attempt, 30))
                continue
            if not r.ok:
                raise TmdbError(f"HTTP {r.status_code} for {media_type}/{tmdb_id}")
            return r.json()

        raise TmdbError(f"gave up on {media_type}/{tmdb_id} after {MAX_ATTEMPTS} attempts ({last_error})")


def trim(media_type, raw):
    """Keeps only the fields the stats need, in one shape for both movies and TV."""
    if media_type == "tv":
        genres = []
        for g in raw.get("genres", []):
            genres.extend(TV_GENRE_SPLIT.get(g["name"], [g["name"]]))
        # Shows rarely have a show-level Director credit; creators are the
        # closest equivalent and are reported separately from film directors.
        return {
            "media_type": "tv",
            "title": raw.get("name") or raw.get("original_name"),
            "release_date": raw.get("first_air_date") or None,
            "runtime": _tv_runtime(raw),
            "genres": list(dict.fromkeys(genres)),
            "directors": [],
            "creators": [c["name"] for c in raw.get("created_by", [])],
            "poster_path": raw.get("poster_path"),
            "original_language": raw.get("original_language"),
            "vote_average": raw.get("vote_average"),
        }

    crew = raw.get("credits", {}).get("crew", [])
    return {
        "media_type": "movie",
        "title": raw.get("title") or raw.get("original_title"),
        "release_date": raw.get("release_date") or None,
        "runtime": raw.get("runtime") or None,
        "genres": [g["name"] for g in raw.get("genres", [])],
        "directors": list(dict.fromkeys(c["name"] for c in crew if c.get("job") == "Director")),
        "creators": [],
        "poster_path": raw.get("poster_path"),
        "original_language": raw.get("original_language"),
        "vote_average": raw.get("vote_average"),
    }


class GcsCache:
    """
    One index object, tmdb/index.json, holding every trimmed entry: read once
    and written once per run. (A per-title object meant one GCS read per
    title, which for a backfill of thousands is most of the run's time limit.)
    """

    INDEX_PATH = "tmdb/index.json"

    def __init__(self, bucket):
        self._bucket = bucket

    def load(self):
        """{(media_type, id): (data, cached_at)}; empty if there's no index yet or it's unreadable."""
        try:
            raw = json.loads(self._bucket.blob(self.INDEX_PATH).download_as_text())
            return {_parse_key(k): (v["data"], datetime.fromisoformat(v["cached_at"])) for k, v in raw.items()}
        except gcs_exceptions.NotFound:
            return {}
        except (ValueError, KeyError, TypeError, AttributeError):
            # A corrupt index is just a cold cache; this run rewrites it.
            return {}

    def save(self, entries):
        body = {f"{mt}/{tid}": {"cached_at": at.isoformat(), "data": data} for (mt, tid), (data, at) in entries.items()}
        self._bucket.blob(self.INDEX_PATH).upload_from_string(
            json.dumps(body, separators=(",", ":")),
            content_type="application/json",
        )


def enrich(keys, client, cache, now=None, max_fetches=MAX_FETCHES_PER_RUN, workers=FETCH_WORKERS):
    """
    Returns ({(media_type, id): trimmed metadata}, warnings, cache_hits).

    Fresh cache entries skip TMDB entirely. Stale ones are used as they are
    and refreshed if there's room in this run's lookup budget, oldest first,
    after titles with no entry at all. A failed lookup keeps any stale copy;
    a title with neither gets a warning. Titles that didn't fit in the
    budget get one combined warning and are picked up by later runs.
    """
    now = now or datetime.now(UTC)
    entries = cache.load()
    metadata, warnings = {}, []
    missing, stale = [], []

    for key in keys:
        entry = entries.get(key)
        if entry is None:
            missing.append(key)
            continue
        data, cached_at = entry
        metadata[key] = data
        if now - cached_at >= CACHE_MAX_AGE:
            stale.append(key)

    stale.sort(key=lambda k: entries[k][1])
    todo = (missing + stale)[:max_fetches]
    deferred = len(missing) - min(len(missing), max_fetches)

    def lookup(key):
        try:
            return trim(key[0], client.fetch(*key)), None
        except TmdbError as exc:
            return None, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lookup, todo))

    fetched = 0
    for (media_type, tmdb_id), (data, exc) in zip(todo, results, strict=True):
        key = (media_type, tmdb_id)
        if data is not None:
            metadata[key] = data
            entries[key] = (data, now)
            fetched += 1
        elif key not in metadata:
            warnings.append({"tmdb_id": tmdb_id, "media_type": media_type, "reason": str(exc)})

    if deferred:
        warnings.append(
            {
                "reason": f"{deferred} titles are waiting for a TMDB lookup "
                f"(at most {max_fetches} per run); later runs will fill them in"
            }
        )
    if fetched:
        cache.save(entries)

    cache_hits = len(metadata) - fetched
    return metadata, warnings, cache_hits


def _parse_key(key):
    media_type, tmdb_id = key.split("/")
    return media_type, int(tmdb_id)


def _tv_runtime(raw):
    # episode_run_time is often empty on newer shows; the last aired
    # episode's runtime is the next-best per-episode figure.
    run_times = raw.get("episode_run_time") or []
    if run_times:
        return run_times[0]
    last = raw.get("last_episode_to_air") or {}
    return last.get("runtime") or None


def _retry_after(response):
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
