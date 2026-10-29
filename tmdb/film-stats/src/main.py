import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.cloud import storage

import films
import publish
import sheet
import tmdb


def run():
    # Read at call time, not import time, so `import main` works without any
    # runtime env (the PR build-check imports it to smoke-test the image).
    sheet_id = os.environ["SHEET_ID"]
    sheet_tab = os.environ.get("SHEET_TAB", "Watch Log")
    cache_bucket = os.environ["CACHE_BUCKET"]
    stats_bucket = os.environ["STATS_BUCKET"]
    films_object = os.environ.get("FILMS_OBJECT", "films.json")

    values = sheet.fetch_rows(sheet_id, sheet_tab)
    viewings, row_warnings = sheet.parse_rows(values)

    gcs = storage.Client()
    client = tmdb.TmdbClient(os.environ["TMDB_API_TOKEN"])
    keys = sorted({(v.media_type, v.tmdb_id) for v in viewings})
    metadata, tmdb_warnings, cache_hits = tmdb.enrich(keys, client, tmdb.GcsCache(gcs.bucket(cache_bucket)))

    warnings = row_warnings + tmdb_warnings
    export = films.films_export(viewings, metadata, generated_at=datetime.now(UTC), warnings=warnings)
    size = publish.publish(gcs.bucket(stats_bucket), export, films_object)

    summary = {
        "rows_read": max(len(values) - 1, 0),  # excluding the header row
        "viewings": len(viewings),
        # A bad score or rating warns but keeps its row, so not every warned row was skipped.
        "rows_skipped": len({w["row"] for w in row_warnings} - {v.row for v in viewings}),
        "warnings": len(warnings),
        "titles": len(keys),
        "tmdb_calls": client.calls,
        "cache_hits": cache_hits,
        "bytes": size,
    }
    print(f"film-stats run: {json.dumps(summary)}")
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
