"""
Gap-fill: pulls ListenBrainz listens for a date range (UTC, inclusive) and
appends whatever isn't already in BigQuery. Use it to recover days the
daily job missed, e.g. a failed run or a day before the switch-over that
last.fm stopped serving.

Dedup is by scrobble_uts against rows already in the range (from either
source), so re-running over the same range is safe.

Not part of the deployed container — the Dockerfile only copies main.py
and listenbrainz.py. Run it manually from a machine with:
  - LISTENBRAINZ_USER (and optionally LISTENBRAINZ_TOKEN, for a higher
    rate limit) env vars
  - Application Default Credentials for a principal with BigQuery
    read+write on elliotx.lastfm.recent_tracks (your own
    `gcloud auth application-default login`, not the job's runtime SA)

Usage:
  LISTENBRAINZ_USER=... python backfill.py 2026-11-28 2026-11-30 [--dry-run]

The full last.fm history up to the switch-over was loaded by the previous
(last.fm) version of this script; see git history.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, time, timezone

from google.cloud import bigquery

import listenbrainz

BQ_PROJECT = os.environ.get("BQ_PROJECT", "elliotx")
BQ_DATASET = os.environ.get("BQ_DATASET", "lastfm")
BQ_TABLE = os.environ.get("BQ_TABLE", "recent_tracks")

SCHEMA = [
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
    bigquery.SchemaField("source", "STRING", mode="NULLABLE"),
]


def day_bounds(start_day, end_day):
    start = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_day, time(23, 59, 59), tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def existing_scrobble_uts(client, table_id, start_uts, end_uts):
    query = f"""
        SELECT DISTINCT scrobble_uts FROM `{table_id}`
        WHERE scrobble_uts BETWEEN @start AND @end
    """
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start", "INT64", start_uts),
            bigquery.ScalarQueryParameter("end", "INT64", end_uts),
        ]
    )
    return {row.scrobble_uts for row in client.query(query, job_config=config).result()}


def assign_ranks(rows):
    """rank = position within that calendar day (UTC), 1-indexed, matching
    main.py — rows arrive newest-first, so each day's group is already in
    the right order."""
    per_day = {}
    for r in rows:
        day = r["scrobble_datetime"][:10]
        per_day[day] = per_day.get(day, 0) + 1
        r["rank"] = per_day[day]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("start", type=date.fromisoformat, help="first UTC day, YYYY-MM-DD")
    parser.add_argument("end", type=date.fromisoformat, help="last UTC day (inclusive), YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="report what would be loaded, load nothing")
    args = parser.parse_args()

    user = os.environ["LISTENBRAINZ_USER"]
    start_uts, end_uts = day_bounds(args.start, args.end)

    client = bigquery.Client(project=BQ_PROJECT)
    table_id = f"{client.project}.{BQ_DATASET}.{BQ_TABLE}"

    existing = existing_scrobble_uts(client, table_id, start_uts, end_uts)
    print(f"{len(existing)} rows already in BigQuery for {args.start}..{args.end}", file=sys.stderr)

    listens = listenbrainz.fetch_listens(user, start_uts, end_uts)
    print(f"{len(listens)} listens from ListenBrainz", file=sys.stderr)

    rows = [listenbrainz.to_row(listen) for listen in listens]
    rows = [r for r in rows if r["scrobble_uts"] not in existing]
    print(f"{len(rows)} new rows to load", file=sys.stderr)
    if not rows or args.dry_run:
        return

    assign_ranks(rows)
    written_at = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r["timestamp"] = written_at

    # A load job, not insert_rows_json (streaming): cheaper for bulk loads,
    # and no streaming-buffer lag before the rows are queryable.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        ndjson_path = f.name

    try:
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=SCHEMA,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        with open(ndjson_path, "rb") as f:
            client.load_table_from_file(f, table_id, job_config=job_config).result()
        print(f"Loaded {len(rows)} rows into {table_id}", file=sys.stderr)
    finally:
        os.remove(ndjson_path)


if __name__ == "__main__":
    main()
