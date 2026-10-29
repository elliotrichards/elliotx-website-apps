"""
Pure function: validated viewings + trimmed TMDB metadata -> films.json, the
site's data file. It does no aggregating: it's every viewing plus one
metadata entry per title, and the site works out its own stats at build
time, so new charts don't need a change here.

Keys are camelCase (the site's convention). Films are keyed "movie/603" / "tv/1396", the same as the TMDB cache, because
a movie and a show can share an ID.
"""

from collections import Counter
from datetime import UTC

SCHEMA_VERSION = 2
TMDB_ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."


def films_export(viewings, metadata, *, generated_at, warnings=()):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": _utc_iso(generated_at),
        "warnings": list(warnings),
        "attribution": TMDB_ATTRIBUTION,
        "viewings": export_viewings(viewings),
        "films": {
            f"{media_type}/{tmdb_id}": export_film(metadata.get((media_type, tmdb_id)), fallback_title=title)
            for (media_type, tmdb_id), title in _titles(viewings).items()
        },
    }


def export_viewings(viewings):
    """
    Oldest first, by watch date and then sheet row: backfilled rows can be
    anywhere in the sheet, so sheet order alone says nothing. `order` is the
    position within the day; `rewatch` means an earlier viewing exists.
    """
    seen, per_day, ids = set(), Counter(), Counter()
    out = []
    for v in sorted(viewings, key=lambda v: (v.watched_date, v.row)):
        key = (v.media_type, v.tmdb_id)
        day = v.watched_date.isoformat()
        per_day[day] += 1
        base = f"{day}-{v.media_type}-{v.tmdb_id}"
        ids[base] += 1
        out.append(
            {
                "id": base if ids[base] == 1 else f"{base}-{ids[base]}",
                "mediaType": v.media_type,
                "tmdbId": v.tmdb_id,
                "watchedOn": day,
                "order": per_day[day],
                "rating": v.rating,
                "rewatch": key in seen,
                "note": v.public_note,
                "format": v.format,
            }
        )
        seen.add(key)
    return out


def export_film(m, fallback_title=None):
    """One film or show. With no metadata (TMDB lookup failed) every field is null but the sheet's title."""
    m = m or {}
    return {
        "mediaType": m.get("media_type"),
        "title": m.get("title") or fallback_title or None,
        "originalTitle": m.get("original_title"),
        "releaseDate": m.get("release_date"),
        "runtime": m.get("runtime"),
        "genres": list(m.get("genres") or []),
        "directors": _people(m.get("directors")),
        "creators": _people(m.get("creators")),
        "cast": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "character": c.get("character"),
                "profilePath": c.get("profile_path"),
            }
            for c in m.get("cast") or []
        ],
        "originalLanguage": m.get("original_language"),
        "countries": list(m.get("countries") or []),
        "tagline": m.get("tagline"),
        "overview": m.get("overview"),
        "posterPath": m.get("poster_path"),
        "backdropPath": m.get("backdrop_path"),
        "tmdbVote": m.get("vote_average"),
        "tmdbVoteCount": m.get("vote_count"),
        "collection": m.get("collection"),
        "certification": m.get("certification"),
        "imdbId": m.get("imdb_id"),
        "seasons": m.get("seasons"),
        "episodes": m.get("episodes"),
    }


def _people(people):
    # Entries cached before people had IDs are plain names; keep the name, null the ID.
    return [p if isinstance(p, dict) else {"id": None, "name": p} for p in people or []]


def _titles(viewings):
    """Each title once, with the sheet's title for it (the last non-blank one) as a fallback."""
    titles = {}
    for v in sorted(viewings, key=lambda v: (v.media_type, v.tmdb_id)):
        key = (v.media_type, v.tmdb_id)
        titles[key] = v.title or titles.get(key) or None
    return dict(sorted(titles.items()))


def _utc_iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
