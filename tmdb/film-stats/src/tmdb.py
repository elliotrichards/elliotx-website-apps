"""TMDB metadata lookup, fronted by a GCS cache of trimmed responses."""

import json
import time
from datetime import UTC, datetime, timedelta

import requests
from google.api_core import exceptions as gcs_exceptions

API_BASE = "https://api.themoviedb.org/3"
MAX_ATTEMPTS = 4
# TMDB's documented ceiling is ~50 req/s; a small fixed gap keeps a cold
# cache (first run, every title uncached) well under it.
POLITE_DELAY_SECONDS = 0.1

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
        self.calls = 0

    def fetch(self, media_type, tmdb_id):
        """GET /{movie|tv}/{id} with credits. Retries 429/5xx and network errors with backoff; 404 is final."""
        url = f"{API_BASE}/{media_type}/{tmdb_id}"
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._sleep(POLITE_DELAY_SECONDS)
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
    """tmdb/{movie|tv}/{id}.json in the private cache bucket."""

    def __init__(self, bucket):
        self._bucket = bucket

    def get(self, media_type, tmdb_id):
        blob = self._bucket.blob(_cache_path(media_type, tmdb_id))
        try:
            entry = json.loads(blob.download_as_text())
            return entry["data"], datetime.fromisoformat(entry["cached_at"])
        except gcs_exceptions.NotFound:
            return None, None
        except (ValueError, KeyError, TypeError):
            # A corrupt entry is just a miss; the refetch overwrites it.
            return None, None

    def put(self, media_type, tmdb_id, data, now):
        blob = self._bucket.blob(_cache_path(media_type, tmdb_id))
        blob.upload_from_string(
            json.dumps({"cached_at": now.isoformat(), "data": data}),
            content_type="application/json",
        )


def enrich(keys, client, cache, now=None):
    """
    Returns ({(media_type, id): trimmed metadata}, warnings, cache_hits).
    Fresh cache entries skip TMDB entirely; stale ones are refreshed but kept
    if TMDB fails; a title with neither just gets a warning.
    """
    now = now or datetime.now(UTC)
    metadata, warnings, cache_hits = {}, [], 0

    for media_type, tmdb_id in keys:
        cached, cached_at = cache.get(media_type, tmdb_id)
        if cached is not None and now - cached_at < CACHE_MAX_AGE:
            metadata[(media_type, tmdb_id)] = cached
            cache_hits += 1
            continue

        try:
            data = trim(media_type, client.fetch(media_type, tmdb_id))
        except TmdbError as exc:
            if cached is not None:
                metadata[(media_type, tmdb_id)] = cached
                cache_hits += 1
            else:
                warnings.append({"tmdb_id": tmdb_id, "media_type": media_type, "reason": str(exc)})
            continue

        cache.put(media_type, tmdb_id, data, now)
        metadata[(media_type, tmdb_id)] = data

    return metadata, warnings, cache_hits


def _cache_path(media_type, tmdb_id):
    return f"tmdb/{media_type}/{tmdb_id}.json"


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
