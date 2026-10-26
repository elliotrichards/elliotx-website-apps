import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from google.cloud import storage

import publish
import sheet
import stats
import tmdb

# Past this, split `recent` (or a future `all_viewings`) into its own file.
SIZE_WARNING_BYTES = 300_000


def run():
    # Read at call time, not import time, so `import main` works without any
    # runtime env (the PR build-check imports it to smoke-test the image).
    sheet_id = os.environ["SHEET_ID"]
    sheet_tab = os.environ.get("SHEET_TAB", "Watch Log")
    sheet_range = os.environ.get("SHEET_RANGE", "A2:F")
    cache_bucket = os.environ["CACHE_BUCKET"]
    stats_bucket = os.environ["STATS_BUCKET"]
    stats_object = os.environ.get("STATS_OBJECT", "stats.json")
    # "Today" for streaks and month filling is the watcher's local date, not UTC.
    time_zone = ZoneInfo(os.environ.get("TIME_ZONE", "Europe/London"))

    values = sheet.fetch_rows(sheet_id, sheet_tab, sheet_range)
    viewings, row_warnings = sheet.parse_rows(values)

    gcs = storage.Client()
    client = tmdb.TmdbClient(os.environ["TMDB_API_TOKEN"])
    keys = sorted({(v.media_type, v.tmdb_id) for v in viewings})
    metadata, tmdb_warnings, cache_hits = tmdb.enrich(keys, client, tmdb.GcsCache(gcs.bucket(cache_bucket)))

    now = datetime.now(UTC)
    payload = stats.compute_stats(
        viewings,
        metadata,
        today=now.astimezone(time_zone).date(),
        generated_at=now,
        warnings=row_warnings + tmdb_warnings,
    )
    size = publish.publish(gcs.bucket(stats_bucket), payload, stats_object)

    summary = {
        "rows_read": len(values),
        "viewings": len(viewings),
        # A bad rating warns but keeps its row, so not every warned row was skipped.
        "rows_skipped": len({w["row"] for w in row_warnings} - {v.row for v in viewings}),
        "warnings": len(payload["warnings"]),
        "titles": len(keys),
        "tmdb_calls": client.calls,
        "cache_hits": cache_hits,
        "bytes": size,
    }
    print(f"film-stats run: {json.dumps(summary)}")
    if size > SIZE_WARNING_BYTES:
        print(f"film-stats: {stats_object} is {size} bytes, consider splitting it")
    return summary


class Handler(BaseHTTPRequestHandler):
    # Cloud Scheduler calls this via POST; GET is accepted too so the job
    # can also be triggered by hand (e.g. `curl` with an identity token).
    def do_POST(self):
        self._run()

    def do_GET(self):
        self._run()

    def _run(self):
        try:
            status, body = 200, run()
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
