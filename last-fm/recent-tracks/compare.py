"""
Pre-switch-over check: compares ListenBrainz with the last.fm rows already
in BigQuery, day by day, over a range both services were recording. Read
only — loads nothing.

For each UTC day it prints both counts, and any (artist, track) whose count
differs between the two. Small differences around midnight or from
rescrobbles are expected; a whole player missing from ListenBrainz shows up
as a run of tracks only last.fm has.

Usage:
  LISTENBRAINZ_USER=... python compare.py 2026-11-19 2026-11-25
"""

import argparse
import os
from collections import Counter
from datetime import date, timedelta

from google.cloud import bigquery

import listenbrainz
from backfill import BQ_DATASET, BQ_PROJECT, BQ_TABLE, day_bounds


def lastfm_rows(client, table_id, start_uts, end_uts):
    query = f"""
        SELECT scrobble_uts, artist_name, track_name FROM `{table_id}`
        WHERE scrobble_uts BETWEEN @start AND @end AND source IS NULL
    """
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start", "INT64", start_uts),
            bigquery.ScalarQueryParameter("end", "INT64", end_uts),
        ]
    )
    return list(client.query(query, job_config=config).result())


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("start", type=date.fromisoformat)
    parser.add_argument("end", type=date.fromisoformat)
    args = parser.parse_args()

    user = os.environ["LISTENBRAINZ_USER"]
    client = bigquery.Client(project=BQ_PROJECT)
    table_id = f"{client.project}.{BQ_DATASET}.{BQ_TABLE}"

    day = args.start
    total_lfm = total_lb = 0
    while day <= args.end:
        start_uts, end_uts = day_bounds(day, day)
        lfm = Counter((r.artist_name, r.track_name) for r in lastfm_rows(client, table_id, start_uts, end_uts))
        lb = Counter(
            (l["track_metadata"]["artist_name"], l["track_metadata"]["track_name"])
            for l in listenbrainz.fetch_listens(user, start_uts, end_uts)
        )
        n_lfm, n_lb = sum(lfm.values()), sum(lb.values())
        total_lfm += n_lfm
        total_lb += n_lb
        print(f"{day}  last.fm {n_lfm:4d}  ListenBrainz {n_lb:4d}  {'OK' if lfm == lb else 'DIFF'}")
        for key in sorted(set(lfm) | set(lb)):
            if lfm[key] != lb[key]:
                print(f"    last.fm {lfm[key]}  LB {lb[key]}  {key[0]} — {key[1]}")
        day += timedelta(days=1)

    print(f"total  last.fm {total_lfm}  ListenBrainz {total_lb}")


if __name__ == "__main__":
    main()
