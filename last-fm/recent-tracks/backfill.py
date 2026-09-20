"""
One-time backfill: pulls the user's ENTIRE last.fm scrobble history (not
just "yesterday", like main.py's daily job) via paginated
user.getRecentTracks calls, and appends whatever isn't already in BigQuery.

Not part of the deployed container — the Dockerfile only copies main.py, so
this never ships. Run manually, once, from a machine with:
  - LASTFM_API_KEY / LASTFM_USERNAME env vars (same as the Cloud Run job)
  - Application Default Credentials for a principal with BigQuery
    read+write on elliotx.lastfm.recent_tracks (the Cloud Run job's own
    runtime SA only has write via insert_rows_json at request time, not a
    principal you'd run this script as — use your own `gcloud auth
    application-default login` instead)

Usage: LASTFM_API_KEY=... LASTFM_USERNAME=... python backfill.py
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

import requests
from google.cloud import bigquery

API_KEY = os.environ["LASTFM_API_KEY"]
LASTFM_USER = os.environ["LASTFM_USERNAME"]
BASE_URL = "https://ws.audioscrobbler.com/2.0"

BQ_PROJECT = os.environ.get("BQ_PROJECT", "elliotx")
BQ_DATASET = os.environ.get("BQ_DATASET", "lastfm")
BQ_TABLE = os.environ.get("BQ_TABLE", "recent_tracks")

PAGE_SIZE = 200  # last.fm's max per page
REQUEST_DELAY_S = 0.3  # conservative — last.fm doesn't publish a hard limit


def fetch_page(page, max_retries=5):
    params = {
        "method": "user.getRecentTracks",
        "api_key": API_KEY,
        "format": "json",
        "user": LASTFM_USER,
        "limit": PAGE_SIZE,
        "page": page,
    }

    # last.fm occasionally throws a transient 500 partway through a long
    # paginated pull — worth a few retries rather than losing everything
    # fetched so far.
    for attempt in range(max_retries):
        try:
            r = requests.get(BASE_URL, params=params, timeout=30)
            r.raise_for_status()
            break
        except requests.exceptions.HTTPError as exc:
            if r.status_code < 500 or attempt == max_retries - 1:
                raise
            backoff = 2**attempt
            print(
                f"page {page}: {exc}, retrying in {backoff}s (attempt {attempt + 1}/{max_retries})",
                file=sys.stderr,
            )
            time.sleep(backoff)

    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Last.fm API error {data['error']}: {data.get('message')}")
    return data["recenttracks"]


def clean_track(t):
    """Mirrors main.py's fetch_recent_tracks filtering: drop the
    now-playing entry and anything without a real timestamp."""
    if "@attr" in t and t["@attr"].get("nowplaying") == "true":
        return None
    if "date" not in t:
        return None

    uts = int(t["date"]["uts"])
    return {
        "scrobble_uts": uts,
        "scrobble_datetime": datetime.fromtimestamp(uts, tz=timezone.utc).isoformat(),
        "artist_name": t["artist"]["#text"],
        "track_name": t["name"],
        "album_name": t.get("album", {}).get("#text", ""),
        "artist_mbid": t["artist"].get("mbid", ""),
        "track_mbid": t.get("mbid", ""),
        "album_mbid": t.get("album", {}).get("mbid", ""),
        "url": t["url"],
    }


def fetch_all_scrobbles():
    """Walks every page, oldest call last (last.fm returns newest first).
    Returns a flat list in that same newest-first order."""
    all_tracks = []
    page = 1
    total_pages = None

    while total_pages is None or page <= total_pages:
        recenttracks = fetch_page(page)
        attr = recenttracks.get("@attr", {})
        total_pages = int(attr.get("totalPages", 1))

        for t in recenttracks.get("track", []):
            cleaned = clean_track(t)
            if cleaned:
                all_tracks.append(cleaned)

        print(f"page {page}/{total_pages} — {len(all_tracks)} scrobbles so far", file=sys.stderr)
        page += 1
        if page <= total_pages:
            time.sleep(REQUEST_DELAY_S)

    return all_tracks


def existing_scrobble_uts(client, table_id):
    query = f"SELECT DISTINCT scrobble_uts FROM `{table_id}`"
    return {row.scrobble_uts for row in client.query(query).result()}


def assign_ranks(tracks):
    """rank = position within that calendar day (UTC), 1-indexed, matching
    main.py's per-day rank semantics — tracks arrive newest-first overall,
    so within any single day's group they're already in the right order."""
    per_day_count = {}
    for t in tracks:
        day = t["scrobble_datetime"][:10]
        per_day_count[day] = per_day_count.get(day, 0) + 1
        t["rank"] = per_day_count[day]
    return tracks


def main():
    client = bigquery.Client(project=BQ_PROJECT)
    table_id = f"{client.project}.{BQ_DATASET}.{BQ_TABLE}"

    print("Fetching existing scrobble_uts from BigQuery for dedup...", file=sys.stderr)
    existing = existing_scrobble_uts(client, table_id)
    print(f"{len(existing)} scrobbles already loaded", file=sys.stderr)

    print("Fetching full last.fm history (this can take a while)...", file=sys.stderr)
    all_tracks = fetch_all_scrobbles()
    print(f"{len(all_tracks)} total scrobbles from the API", file=sys.stderr)

    new_tracks = [t for t in all_tracks if t["scrobble_uts"] not in existing]
    print(f"{len(new_tracks)} new scrobbles to load", file=sys.stderr)
    if not new_tracks:
        print("Nothing new — done.", file=sys.stderr)
        return

    ingest_ts = datetime.now(timezone.utc).isoformat()
    assign_ranks(new_tracks)
    for t in new_tracks:
        t["timestamp"] = ingest_ts

    schema = [
        bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("scrobble_uts", "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("scrobble_datetime", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("rank", "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("artist_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("track_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("album_name", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("artist_mbid", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("track_mbid", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("album_mbid", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("url", "STRING", mode="NULLABLE"),
    ]

    # A load job, not insert_rows_json (streaming): far cheaper and quota-
    # friendlier for a potentially large one-time bulk backfill, and load
    # jobs don't hit the streaming buffer's brief post-insert query lag.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
        for t in new_tracks:
            f.write(json.dumps(t) + "\n")
        ndjson_path = f.name

    try:
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        with open(ndjson_path, "rb") as f:
            job = client.load_table_from_file(f, table_id, job_config=job_config)
        job.result()
        print(f"Loaded {len(new_tracks)} rows into {table_id}", file=sys.stderr)
    finally:
        os.remove(ndjson_path)


if __name__ == "__main__":
    main()
