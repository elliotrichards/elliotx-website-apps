"""
Pure functions: validated viewings + trimmed TMDB metadata -> the stats.json
dict. No I/O and no clock reads ("today" and "generated_at" are passed in),
so the same inputs always produce the same output.

Counts are per viewing, not per unique title: a rewatch counts again toward
genres, decades, directors and so on, the same way it does toward by_month.
Viewings whose metadata couldn't be found (see warnings) still count toward
totals, dates, streaks and ratings, just not the metadata-derived breakdowns.
"""

from collections import Counter
from datetime import UTC, date, timedelta

SCHEMA_VERSION = 1
TMDB_ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."
RECENT_LIMIT = 12
TOP_PEOPLE = 10
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
RATING_STEPS = [step / 2 for step in range(1, 11)]  # 0.5 .. 5.0


def compute_stats(viewings, metadata, *, today, generated_at, warnings=()):
    def meta(v):
        return metadata.get((v.media_type, v.tmdb_id))

    with_meta = [(v, meta(v)) for v in viewings if meta(v) is not None]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_iso(generated_at),
        "warnings": list(warnings),
        "totals": totals(viewings, metadata),
        "by_year": by_year(viewings, today),
        "by_month": by_month(viewings, today),
        "genres": ranked(g for _, m in with_meta for g in m.get("genres", [])),
        "decades": decades(m.get("release_date") for _, m in with_meta),
        "directors": ranked(d for _, m in with_meta for d in names(m.get("directors", [])))[:TOP_PEOPLE],
        "creators": ranked(c for _, m in with_meta for c in names(m.get("creators", [])))[:TOP_PEOPLE],
        "languages": ranked(m["original_language"] for _, m in with_meta if m.get("original_language")),
        "streaks": streaks([v.watched_date for v in viewings], today),
        "busiest": busiest(viewings),
        "ratings": ratings(viewings),
        "recent": recent(viewings, metadata),
        "attribution": TMDB_ATTRIBUTION,
    }


def totals(viewings, metadata):
    unique = {(v.media_type, v.tmdb_id) for v in viewings}
    minutes = sum((metadata.get((v.media_type, v.tmdb_id)) or {}).get("runtime") or 0 for v in viewings)
    by_type = Counter(v.media_type for v in viewings)
    return {
        "viewings": len(viewings),
        "unique": len(unique),
        "rewatches": len(viewings) - len(unique),
        "runtime_hours": round(minutes / 60, 1),
        "by_media_type": {"movie": by_type.get("movie", 0), "tv": by_type.get("tv", 0)},
    }


def by_year(viewings, today):
    if not viewings:
        return []
    counts = Counter(v.watched_date.year for v in viewings)
    first = min(counts)
    last = max(max(counts), today.year)
    return [{"year": y, "count": counts.get(y, 0)} for y in range(first, last + 1)]


def by_month(viewings, today):
    """Every month from the first viewing to this month, zero-filled so charts have no gaps."""
    if not viewings:
        return []
    counts = Counter(_month_key(v.watched_date) for v in viewings)
    start = min(v.watched_date for v in viewings).replace(day=1)
    end = max(max(v.watched_date for v in viewings), today).replace(day=1)

    months = []
    cursor = start
    while cursor <= end:
        key = _month_key(cursor)
        months.append({"month": key, "count": counts.get(key, 0)})
        cursor = _next_month(cursor)
    return months


def names(people):
    """Person names from trimmed metadata: {id, name} now, plain strings in entries cached before that."""
    return [p["name"] if isinstance(p, dict) else p for p in people]


def ranked(items):
    """[{name, count}] sorted by count descending, then name for a stable order."""
    counts = Counter(items)
    return [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def decades(release_dates):
    """Chronological, not by count: decades read naturally as a timeline."""
    counts = Counter()
    for release in release_dates:
        year = _year(release)
        if year is not None:
            counts[year // 10 * 10] += 1
    return [{"decade": d, "count": counts[d]} for d in sorted(counts)]


def streaks(watch_dates, today):
    """
    Runs of consecutive calendar days with at least one viewing. The current
    streak is still alive if the last viewing was today or yesterday, so a run
    that fires early in the morning doesn't reset it before tonight's film.
    """
    empty = {"days": 0, "start": None, "end": None}
    days = sorted(set(watch_dates))
    if not days:
        return {"current": empty, "longest": empty}

    runs = []
    run_start = prev = days[0]
    for d in days[1:]:
        if d - prev != timedelta(days=1):
            runs.append((run_start, prev))
            run_start = d
        prev = d
    runs.append((run_start, prev))

    def as_dict(run):
        start, end = run
        return {"days": (end - start).days + 1, "start": start.isoformat(), "end": end.isoformat()}

    # max() keeps the first of equal-length runs: the earliest one wins ties.
    longest = max(runs, key=lambda r: (r[1] - r[0]).days)
    last = runs[-1]
    current = as_dict(last) if last[1] >= today - timedelta(days=1) else empty
    return {"current": current, "longest": as_dict(longest)}


def busiest(viewings):
    if not viewings:
        return {"month": None, "weekday": None, "weekdays": [{"day": d, "count": 0} for d in WEEKDAYS]}

    months = Counter(_month_key(v.watched_date) for v in viewings)
    # Sorted first so ties go to the earliest month, deterministically.
    month, month_count = max(sorted(months.items()), key=lambda kv: kv[1])

    weekday_counts = Counter(v.watched_date.weekday() for v in viewings)
    weekdays = [{"day": name, "count": weekday_counts.get(i, 0)} for i, name in enumerate(WEEKDAYS)]
    top_day = max(weekdays, key=lambda d: d["count"])

    return {
        "month": {"month": month, "count": month_count},
        "weekday": {"day": top_day["day"], "count": top_day["count"]},
        "weekdays": weekdays,
    }


def ratings(viewings):
    rated = [v.rating for v in viewings if v.rating is not None]
    if not rated:
        return None
    counts = Counter(rated)
    return {
        "count": len(rated),
        "average": round(sum(rated) / len(rated), 2),
        "distribution": [{"rating": step, "count": counts.get(step, 0)} for step in RATING_STEPS],
    }


def viewings_log(viewings, metadata, *, generated_at):
    """
    The whole watch log for /data/movies/log: every viewing, newest first,
    in the same row shape as `recent`. Published as its own file so the
    dashboard's stats.json stays small however long the log gets.
    """
    rows = recent(viewings, metadata, limit=None)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_iso(generated_at),
        "count": len(rows),
        "viewings": rows,
    }


def recent(viewings, metadata, limit=RECENT_LIMIT):
    # Same-day viewings: the row further down the sheet counts as the later watch.
    latest = sorted(viewings, key=lambda v: (v.watched_date, v.row), reverse=True)[:limit]
    out = []
    for v in latest:
        m = metadata.get((v.media_type, v.tmdb_id)) or {}
        out.append(
            {
                "title": m.get("title") or v.title,
                "year": _year(m.get("release_date")),
                "poster_path": m.get("poster_path"),
                "watched_date": v.watched_date.isoformat(),
                "rating": v.rating,
                "tmdb_id": v.tmdb_id,
                "media_type": v.media_type,
            }
        )
    return out


def _utc_iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def _next_month(d):
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _year(release_date):
    if not release_date or len(release_date) < 4 or not release_date[:4].isdigit():
        return None
    return int(release_date[:4])
