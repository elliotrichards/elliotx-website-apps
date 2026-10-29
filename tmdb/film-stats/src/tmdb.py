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

# Bumped whenever trim() keeps new fields. Entries cached under an older
# version are treated as the stalest of all: still used, but refreshed first.
TRIM_VERSION = 2
# The same for trim_season().
SEASON_TRIM_VERSION = 1

# Top-billed cast kept per title, for the site's film pages.
CAST_LIMIT = 8

# Certification country: the site's audience is in the UK.
CERTIFICATION_COUNTRY = "GB"

# Extra responses fetched in the same request. release_dates (movies) and
# content_ratings (TV) carry certifications; TV keeps its IMDb ID in
# external_ids, where a movie has it at the top level.
APPEND = {"movie": "credits,release_dates", "tv": "credits,content_ratings,external_ids"}

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
        """GET /{movie|tv}/{id} with credits and certifications."""
        return self._get(
            f"{API_BASE}/{media_type}/{tmdb_id}",
            {"append_to_response": APPEND[media_type]},
            what=f"{media_type}/{tmdb_id}",
            not_found=f"TMDB has no {media_type} with id {tmdb_id}",
        )

    def fetch_season(self, tmdb_id, season):
        """GET /tv/{id}/season/{n}: every episode of one season of a show."""
        return self._get(
            f"{API_BASE}/tv/{tmdb_id}/season/{season}",
            {},
            what=f"tv/{tmdb_id} season {season}",
            not_found=f"TMDB has no season {season} of tv/{tmdb_id}",
        )

    def _get(self, url, params, *, what, not_found):
        """Retries 429/5xx and network errors with backoff; 404 is final."""
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._sleep(POLITE_DELAY_SECONDS)
            with self._lock:  # fetch runs on several threads at once
                self.calls += 1
            try:
                r = self._session.get(url, params=params, timeout=20)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                if attempt < MAX_ATTEMPTS:
                    self._sleep(2**attempt)
                continue

            if r.status_code == 404:
                raise TmdbNotFound(not_found)
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                if attempt < MAX_ATTEMPTS:
                    self._sleep(min(_retry_after(r) or 2**attempt, 30))
                continue
            if not r.ok:
                raise TmdbError(f"HTTP {r.status_code} for {what}")
            return r.json()

        raise TmdbError(f"gave up on {what} after {MAX_ATTEMPTS} attempts ({last_error})")


def trim(media_type, raw):
    """
    Keeps only the fields the site's movies pages need, in one
    shape for both movies and TV. People are {id, name} so the site can tell
    two people with the same name apart.
    """
    shared = {
        "media_type": media_type,
        "tagline": raw.get("tagline") or None,
        "overview": raw.get("overview") or None,
        "poster_path": raw.get("poster_path"),
        "backdrop_path": raw.get("backdrop_path"),
        "original_language": raw.get("original_language"),
        "vote_average": raw.get("vote_average"),
        "vote_count": raw.get("vote_count"),
        "cast": _cast(raw),
    }

    if media_type == "tv":
        genres = []
        for g in raw.get("genres", []):
            genres.extend(TV_GENRE_SPLIT.get(g["name"], [g["name"]]))
        # Shows rarely have a show-level Director credit; creators are the
        # closest equivalent and are reported separately from film directors.
        return {
            **shared,
            "title": raw.get("name") or raw.get("original_name"),
            "original_title": raw.get("original_name"),
            "release_date": raw.get("first_air_date") or None,
            "runtime": _tv_runtime(raw),
            "genres": list(dict.fromkeys(genres)),
            "directors": [],
            "creators": [_person(c) for c in raw.get("created_by", [])],
            "countries": list(raw.get("origin_country") or []),
            "collection": None,
            "certification": _tv_certification(raw),
            "imdb_id": (raw.get("external_ids") or {}).get("imdb_id") or None,
            "seasons": raw.get("number_of_seasons"),
            "episodes": raw.get("number_of_episodes"),
        }

    crew = raw.get("credits", {}).get("crew", [])
    directors = {c.get("id") or c.get("name"): _person(c) for c in crew if c.get("job") == "Director"}
    collection = raw.get("belongs_to_collection")
    return {
        **shared,
        "title": raw.get("title") or raw.get("original_title"),
        "original_title": raw.get("original_title"),
        "release_date": raw.get("release_date") or None,
        "runtime": raw.get("runtime") or None,
        "genres": [g["name"] for g in raw.get("genres", [])],
        "directors": list(directors.values()),
        "creators": [],
        "countries": [c["iso_3166_1"] for c in raw.get("production_countries", []) if c.get("iso_3166_1")],
        "collection": {"id": collection["id"], "name": collection.get("name")} if collection else None,
        "certification": _movie_certification(raw),
        "imdb_id": raw.get("imdb_id") or None,
    }


def trim_season(raw):
    """
    {episode number: {name, air_date, runtime}} for one season. The overview
    is left out (it would spoil the log) and so is the still, which nothing
    shows. Keys are strings, as they'll be in JSON.
    """
    return {
        str(e["episode_number"]): {
            "name": e.get("name") or None,
            "air_date": e.get("air_date") or None,
            "runtime": e.get("runtime") or None,
        }
        for e in raw.get("episodes", [])
        if e.get("episode_number") is not None
    }


class GcsCache:
    """
    One index object, tmdb/index.json, holding every trimmed entry: read once
    and written once per run. (A per-title object meant one GCS read per
    title, which for a backfill of thousands is most of the run's time limit.)
    """

    INDEX_PATH = "tmdb/index.json"
    VERSION = TRIM_VERSION

    def __init__(self, bucket):
        self._bucket = bucket

    def load(self):
        """{(media_type, id): (data, cached_at)}; empty if there's no index yet or it's unreadable."""
        try:
            raw = json.loads(self._bucket.blob(self.INDEX_PATH).download_as_text())
            return {self._parse_key(k): (v["data"], _cached_at(v, self.VERSION)) for k, v in raw.items()}
        except gcs_exceptions.NotFound:
            return {}
        except (ValueError, KeyError, TypeError, AttributeError):
            # A corrupt index is just a cold cache; this run rewrites it.
            return {}

    def save(self, entries):
        body = {
            self._key(key): {"cached_at": at.isoformat(), "version": self.VERSION, "data": data}
            for key, (data, at) in entries.items()
        }
        self._bucket.blob(self.INDEX_PATH).upload_from_string(
            json.dumps(body, separators=(",", ":")),
            content_type="application/json",
        )

    @staticmethod
    def _key(key):
        media_type, tmdb_id = key
        return f"{media_type}/{tmdb_id}"

    @staticmethod
    def _parse_key(key):
        media_type, tmdb_id = key.split("/")
        return media_type, int(tmdb_id)


class SeasonCache(GcsCache):
    """
    Seasons, keyed (show id, season), in their own index object: the title
    index's loader reads every key as "type/id", so a season key there would
    make an older build of the job treat the whole cache as corrupt.
    """

    INDEX_PATH = "tmdb/seasons.json"
    VERSION = SEASON_TRIM_VERSION

    @staticmethod
    def _key(key):
        tmdb_id, season = key
        return f"{tmdb_id}/{season}"

    @staticmethod
    def _parse_key(key):
        tmdb_id, season = key.split("/")
        return int(tmdb_id), int(season)


def enrich(keys, client, cache, now=None, max_fetches=MAX_FETCHES_PER_RUN, workers=FETCH_WORKERS):
    """
    Returns ({(media_type, id): trimmed metadata}, warnings, cache_hits).

    Fresh cache entries skip TMDB entirely. Stale ones are used as they are
    and refreshed if there's room in this run's lookup budget, oldest first,
    after titles with no entry at all. A failed lookup keeps any stale copy;
    a title with neither gets a warning. Titles that didn't fit in the
    budget get one combined warning and are picked up by later runs.
    """
    metadata, failures, deferred, fetched = _fill(
        keys,
        cache,
        lambda key: trim(key[0], client.fetch(*key)),
        now=now or datetime.now(UTC),
        max_fetches=max_fetches,
        workers=workers,
    )
    warnings = [
        {"tmdb_id": tmdb_id, "media_type": media_type, "reason": str(exc)} for (media_type, tmdb_id), exc in failures
    ]
    if deferred:
        warnings.append(
            {
                "reason": f"{deferred} titles are waiting for a TMDB lookup "
                f"(at most {max_fetches} per run); later runs will fill them in"
            }
        )
    return metadata, warnings, len(metadata) - fetched


def enrich_seasons(wanted, client, cache, now=None, max_fetches=MAX_FETCHES_PER_RUN, workers=FETCH_WORKERS):
    """
    Returns ({(show id, season): trim_season()}, warnings) for the seasons in
    `wanted`, {(show id, season): episode numbers watched}. Cached like titles,
    with one more reason to refetch straight away: a watched episode that
    isn't in the cached copy (a show still airing when it was cached).
    """
    seasons, failures, deferred, _ = _fill(
        sorted(wanted),
        cache,
        lambda key: trim_season(client.fetch_season(*key)),
        now=now or datetime.now(UTC),
        max_fetches=max_fetches,
        workers=workers,
        incomplete=lambda key, data: any(str(e) not in data for e in wanted[key]),
    )
    warnings = [
        {"tmdb_id": tmdb_id, "media_type": "tv", "season": season, "reason": str(exc)}
        for (tmdb_id, season), exc in failures
    ]
    for (tmdb_id, season), episodes in sorted(wanted.items()):
        data = seasons.get((tmdb_id, season))
        if data is None:
            continue
        for episode in sorted(episodes):
            if str(episode) not in data:
                warnings.append(
                    {
                        "tmdb_id": tmdb_id,
                        "media_type": "tv",
                        "season": season,
                        "reason": f"TMDB has no episode {episode} in season {season} of tv/{tmdb_id}",
                    }
                )
    if deferred:
        warnings.append(
            {
                "reason": f"{deferred} TV seasons are waiting for a TMDB lookup "
                f"(at most {max_fetches} per run); later runs will fill them in"
            }
        )
    return seasons, warnings


def _fill(keys, cache, lookup, *, now, max_fetches, workers, incomplete=lambda key, data: False):
    """
    The cache-then-TMDB loop behind enrich and enrich_seasons. Returns
    ({key: data}, [(key, error) for keys left with nothing], how many keys
    with no usable entry didn't fit in the budget, how many were fetched).
    Saves the cache once, if anything was fetched.
    """
    entries = cache.load()
    found, missing, stale = {}, [], []

    for key in keys:
        entry = entries.get(key)
        if entry is None:
            missing.append(key)
            continue
        data, cached_at = entry
        found[key] = data
        if incomplete(key, data):
            missing.append(key)
        elif now - cached_at >= CACHE_MAX_AGE:
            stale.append(key)

    stale.sort(key=lambda k: entries[k][1])
    todo = (missing + stale)[: max(max_fetches, 0)]
    deferred = len([k for k in missing[len(todo) :] if k not in found])

    def attempt(key):
        try:
            return lookup(key), None
        except TmdbError as exc:
            return None, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(attempt, todo))

    fetched, failures = 0, []
    for key, (data, exc) in zip(todo, results, strict=True):
        if data is not None:
            found[key] = data
            entries[key] = (data, now)
            fetched += 1
        elif key not in found:
            failures.append((key, exc))

    if fetched:
        cache.save(entries)
    return found, failures, deferred, fetched


def _cached_at(entry, version):
    """An entry trimmed by an older trim() counts as cached at the epoch, so it's refreshed before any other."""
    if entry.get("version") != version:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(entry["cached_at"])


def _person(raw):
    return {"id": raw.get("id"), "name": raw.get("name")}


def _cast(raw):
    cast = sorted(raw.get("credits", {}).get("cast", []), key=lambda c: c.get("order", 0))
    return [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "character": c.get("character") or None,
            "profile_path": c.get("profile_path"),
        }
        for c in cast[:CAST_LIMIT]
    ]


def _movie_certification(raw):
    for country in (raw.get("release_dates") or {}).get("results", []):
        if country.get("iso_3166_1") != CERTIFICATION_COUNTRY:
            continue
        # One entry per release type (premiere, theatrical, digital...); any
        # non-blank one will do, they almost always agree.
        for release in country.get("release_dates", []):
            if release.get("certification"):
                return release["certification"]
    return None


def _tv_certification(raw):
    for rating in (raw.get("content_ratings") or {}).get("results", []):
        if rating.get("iso_3166_1") == CERTIFICATION_COUNTRY and rating.get("rating"):
            return rating["rating"]
    return None


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
