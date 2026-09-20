import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from google.cloud import bigquery

API_KEY = os.environ["LASTFM_API_KEY"]
LASTFM_USER = os.environ["LASTFM_USERNAME"]
BASE_URL = "https://ws.audioscrobbler.com/2.0"

BQ_PROJECT = os.environ.get("BQ_PROJECT")  # falls back to the runtime SA's ADC project
BQ_DATASET = os.environ.get("BQ_DATASET", "lastfm")
BQ_TABLE = os.environ.get("BQ_TABLE", "recent_tracks")


def yesterday_range_utc():
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    start = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59, tzinfo=timezone.utc)

    return int(start.timestamp()), int(end.timestamp())


def api_call(method, limit, y_start, y_end):
    params = {
        "method": method,
        "api_key": API_KEY,
        "format": "json",
        "user": LASTFM_USER,
        "from": y_start,
        "to": y_end,
        "limit": limit,
    }

    r = requests.get(BASE_URL, params=params, timeout=30)
    r.raise_for_status()

    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Last.fm API error {data['error']}: {data.get('message')}")

    return data


def fetch_recent_tracks(limit=750):
    """
    Fetches yesterday's (UTC) scrobbles. The API always includes the
    currently-playing track (if any) as an extra leading entry regardless of
    the from/to range — this filters that out, along with anything outside
    the requested day or missing a timestamp.
    """
    y_start, y_end = yesterday_range_utc()
    data = api_call("user.getRecentTracks", limit, y_start, y_end)
    tracks = data["recenttracks"]["track"]

    cleaned = []
    for t in tracks:
        if "@attr" in t and t["@attr"].get("nowplaying") == "true":
            continue
        if "date" not in t:
            continue

        uts = int(t["date"]["uts"])
        if uts < y_start or uts > y_end:
            continue

        cleaned.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scrobble_uts": uts,
                "scrobble_datetime": datetime.fromtimestamp(uts, tz=timezone.utc).isoformat(),
                "rank": len(cleaned) + 1,
                "artist_name": t["artist"]["#text"],
                "track_name": t["name"],
                "album_name": t.get("album", {}).get("#text", ""),
                "artist_mbid": t["artist"].get("mbid", ""),
                "track_mbid": t.get("mbid", ""),
                "album_mbid": t.get("album", {}).get("mbid", ""),
                "url": t["url"],
            }
        )

    return cleaned


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
