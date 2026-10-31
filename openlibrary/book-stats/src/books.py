"""
Pure function: validated reads + trimmed Open Library metadata -> books.json,
the site's data file. Like films.json it does no aggregating: it's every read
plus one metadata entry per book, and the site works out its own stats at
build time.

Keys are camelCase (the site's convention). Books are keyed by their cache
key ("isbn/9780099598244" or "title/…|…"), so a read always finds its entry
even when Open Library had no match. Title and author always come from the
sheet: Open Library's are often a different edition's or carry a subtitle.
"""

import re
from collections import Counter
from datetime import UTC

from openlibrary import lookup_key

SCHEMA_VERSION = 1
ATTRIBUTION = "Book metadata and covers from Open Library (openlibrary.org)."


def books_export(reads, metadata, *, generated_at, warnings=()):
    keys = {lookup_key(r) for r in reads}
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": _utc_iso(generated_at),
        "warnings": list(warnings),
        "attribution": ATTRIBUTION,
        "reads": export_reads(reads, metadata),
        "books": {key: export_book(metadata.get(key)) for key in sorted(keys)},
    }


def export_reads(reads, metadata):
    """
    Oldest first, by finish date and then sheet row. `order` is the position
    within the day; `reread` means an earlier read of the same book exists:
    the same Open Library work when both matched, so a reread in another
    edition counts, otherwise the same ISBN or title and author.
    """
    seen, per_day, ids = set(), Counter(), Counter()
    out = []
    for r in sorted(reads, key=lambda r: (r.finished_date, r.row)):
        key = lookup_key(r)
        work = (metadata.get(key) or {}).get("work_key")
        same = f"work/{work}" if work else key
        day = r.finished_date.isoformat()
        per_day[day] += 1
        base = f"{day}-{_slug(r.title) or 'book'}"
        ids[base] += 1
        out.append(
            {
                "id": base if ids[base] == 1 else f"{base}-{ids[base]}",
                "book": key,
                "title": r.title,
                "author": r.author,
                "finishedOn": day,
                "order": per_day[day],
                "score": r.score,
                "format": r.format,
                "note": r.public_note,
                "isbn": r.isbn,
                "reread": same in seen,
            }
        )
        seen.add(same)
    return out


def export_book(m):
    """One book's metadata. With no match (or no lookup yet) every field is null or empty."""
    m = m or {}
    return {
        "matched": bool(m),
        "workKey": m.get("work_key"),
        "coverId": m.get("cover_id"),
        "firstPublishYear": m.get("first_publish_year"),
        "pages": m.get("pages"),
        "subjects": list(m.get("subjects") or []),
    }


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _utc_iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
