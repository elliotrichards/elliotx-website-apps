import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.cloud import bigquery

import listenbrainz

LISTENBRAINZ_USER = os.environ["LISTENBRAINZ_USER"]

BQ_PROJECT = os.environ.get("BQ_PROJECT")  # falls back to the runtime SA's ADC project
BQ_DATASET = os.environ.get("BQ_DATASET", "lastfm")
BQ_TABLE = os.environ.get("BQ_TABLE", "recent_tracks")


def yesterday_range_utc():
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    start = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59, tzinfo=timezone.utc)

    return int(start.timestamp()), int(end.timestamp())


def fetch_recent_tracks():
    """
    Yesterday's (UTC) listens from ListenBrainz, newest first. Unlike
    last.fm's getRecentTracks, ListenBrainz's /listens never includes the
    playing-now track, so there's nothing extra to filter out.
    """
    y_start, y_end = yesterday_range_utc()
    listens = listenbrainz.fetch_listens(LISTENBRAINZ_USER, y_start, y_end)

    written_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for rank, listen in enumerate(listens, start=1):
        row = listenbrainz.to_row(listen)
        row["timestamp"] = written_at
        row["rank"] = rank
        rows.append(row)

    return rows


def append_to_bigquery(rows):
    if not rows:
        return 0

    client = bigquery.Client(project=BQ_PROJECT)
    table_id = f"{client.project}.{BQ_DATASET}.{BQ_TABLE}"

    errors = client.insert_rows_json(table_id, rows)
    if errors:
        raise RuntimeError(f"BigQuery insert errors: {errors}")

    return len(rows)


class Handler(BaseHTTPRequestHandler):
    # Cloud Scheduler calls this via POST; GET is accepted too so the job
    # can also be triggered by hand (e.g. `curl` with an identity token).
    def do_POST(self):
        self._run()

    def do_GET(self):
        self._run()

    def _run(self):
        try:
            rows = fetch_recent_tracks()
            inserted = append_to_bigquery(rows)
            status, body = 200, {"inserted": inserted}
        except Exception as exc:
            print(f"run failed: {exc}")
            status, body = 500, {"error": str(exc)}

        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # noqa: A002 - matches BaseHTTPRequestHandler's signature
        print(f"{self.address_string()} - {format % args}")


def main():
    port = int(os.environ.get("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"listening on {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
