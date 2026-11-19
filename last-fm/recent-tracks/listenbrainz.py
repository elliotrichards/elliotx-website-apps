"""
ListenBrainz client shared by main.py (daily job), backfill.py and
compare.py. Maps listens onto the existing recent_tracks row shape, which
was designed around last.fm's user.getRecentTracks — the table, dataset and
service keep their "lastfm" names (BigQuery can't rename a dataset), and the
`source` column records which service each row came from (NULL = last.fm).

Reads are public: LISTENBRAINZ_TOKEN is optional and only raises the rate
limit, which matters for backfill.py's long walks, not the daily job's one
or two requests.
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.listenbrainz.org/1"
MAX_COUNT = 1000  # ListenBrainz's per-request ceiling for /listens
SOURCE = "listenbrainz"


def _headers():
    token = os.environ.get("LISTENBRAINZ_TOKEN")
    return {"Authorization": f"Token {token}"} if token else {}


# (connect, read) seconds. Sized with MAX_RETRIES so a run that times out on
# every attempt still finishes inside Cloud Scheduler's default 180s attempt
# deadline: 4 x 35s + 2 + 4 + 8s of backoff = 154s. Scheduler then retries
# the whole run (retry_count = 3 in the Terraform).
TIMEOUT = (10, 25)
MAX_RETRIES = 4


def _get(path, params, max_retries=MAX_RETRIES):
    """GET with retries on timeouts and dropped connections (seen from
    api.listenbrainz.org on 2026-11-19), 429 (honouring
    X-RateLimit-Reset-In) and 5xx."""
    for attempt in range(max_retries):
        try:
            r = requests.get(f"{BASE_URL}{path}", params=params, headers=_headers(), timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"ListenBrainz {type(exc).__name__} on {path}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == max_retries - 1:
                r.raise_for_status()
            wait = int(r.headers.get("X-RateLimit-Reset-In", 2**attempt)) + 1
            print(f"ListenBrainz {r.status_code} on {path}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def fetch_listens(user, start_uts, end_uts):
    """
    Every listen with start_uts <= listened_at <= end_uts, newest first
    (the same order last.fm returned scrobbles in, so per-day `rank` keeps
    its meaning). Pages backwards with max_ts only: max_ts is exclusive, so
    each page asks for listens strictly older than the last one seen.
    """
    listens = []
    max_ts = end_uts + 1
    while True:
        payload = _get(f"/user/{user}/listens", {"max_ts": max_ts, "count": MAX_COUNT})["payload"]
        page = payload.get("listens", [])
        if not page:
            break

        for listen in page:
            if listen["listened_at"] >= start_uts:
                listens.append(listen)

        oldest = page[-1]["listened_at"]
        if oldest < start_uts or len(page) < MAX_COUNT:
            break
        max_ts = oldest

    return listens


def _first(*values):
    """First truthy value, or "" — mbid fields are stored as "" (not NULL)
    when missing, matching how last.fm rows were written."""
    for v in values:
        if v:
            return v
    return ""


def to_row(listen):
    """Map one ListenBrainz listen to a recent_tracks row (minus `timestamp`
    and `rank`, which the caller assigns). Names are the ones the player
    submitted, not MusicBrainz's canonical ones, so they line up with the
    last.fm history (same players, same strings). MBIDs prefer what the
    player sent, falling back to ListenBrainz's own MusicBrainz mapping —
    album_mbid is a *release* mbid, the same kind last.fm stored."""
    meta = listen["track_metadata"]
    info = meta.get("additional_info") or {}
    mapping = meta.get("mbid_mapping") or {}
    uts = int(listen["listened_at"])

    track_mbid = _first(info.get("recording_mbid"), mapping.get("recording_mbid"))
    return {
        "scrobble_uts": uts,
        "scrobble_datetime": datetime.fromtimestamp(uts, tz=timezone.utc).isoformat(),
        "artist_name": meta["artist_name"],
        "track_name": meta["track_name"],
        "album_name": meta.get("release_name") or "",
        "artist_mbid": _first(*(info.get("artist_mbids") or []), *(mapping.get("artist_mbids") or [])),
        "track_mbid": track_mbid,
        "album_mbid": _first(info.get("release_mbid"), mapping.get("release_mbid")),
        "url": f"https://musicbrainz.org/recording/{track_mbid}" if track_mbid else None,
        "source": SOURCE,
    }
