"""Reads the watch-log Google Sheet and validates it into Viewing rows."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
VALUES_URL = "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{a1}"

# Row 1 is headers; data starts on row 2. The whole tab is read from A1, so
# values[0] is the header row and values[n] is sheet row n + 1.
FIRST_DATA_ROW = 2

# Columns are found by header name, not position, so they can be in any
# order and extra columns are ignored. notes is deliberately absent: it's
# never looked up, so it can't leak into the output. public_note is the one
# note column that is published, shown on the site next to the viewing.
REQUIRED_HEADERS = {"tmdb_id", "watched_date"}
OPTIONAL_HEADERS = {"title", "media_type", "rating", "public_note", "format"}

# Longer than one line on the site is a sign the wrong column was filled in.
PUBLIC_NOTE_MAX = 280

# Sheets' date serial epoch: serial 1 is 1899-12-31, so day 0 is the 30th.
SERIAL_EPOCH = date(1899, 12, 30)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MEDIA_TYPES = {"movie", "tv"}


@dataclass(frozen=True)
class Viewing:
    row: int
    title: str
    tmdb_id: int
    media_type: str
    watched_date: date
    rating: float | None
    public_note: str | None = None
    # Free text, e.g. Cinema, Home, Streaming; the site groups by it as typed.
    format: str | None = None


class SheetError(Exception):
    """The sheet's shape is wrong (not just a bad row), so the run should fail."""


def fetch_rows(sheet_id, tab, session=None):
    """
    Returns every row of the tab, header row first. Uses ADC with the read-only
    Sheets scope: on Cloud Run that's the runtime SA, which only needs Viewer
    on the sheet itself, no key file and no project IAM role.

    SERIAL_NUMBER, not FORMATTED_STRING, for dates: a formatted string follows
    the sheet's locale (26/10/2026 vs 10/26/2026), a serial number doesn't.
    Dates typed as plain text still come back as strings and get parsed as ISO.
    """
    if session is None:
        credentials, _ = google.auth.default(scopes=SCOPES)
        session = AuthorizedSession(credentials)

    # A bare tab name is the whole tab, always starting at A1 — so sheet row
    # and column positions line up with list indexes.
    a1 = "'{}'".format(tab.replace("'", "''"))
    r = session.get(
        VALUES_URL.format(sheet_id=sheet_id, a1=quote(a1, safe="")),
        params={
            "valueRenderOption": "UNFORMATTED_VALUE",
            "dateTimeRenderOption": "SERIAL_NUMBER",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("values", [])


def parse_rows(values):
    """
    Validates raw rows (header row first) into Viewings. A bad row is skipped
    and recorded in warnings (sheet row number + reason) instead of failing
    the run. A bad header row raises SheetError instead: every row would be
    misread, so publishing an empty stats.json would be worse than failing.
    """
    if not values:
        raise SheetError("sheet is empty: expected a header row")
    columns = _header_columns(values[0])

    viewings, warnings = [], []

    for offset, raw in enumerate(values[1:]):
        row = FIRST_DATA_ROW + offset
        if all(_blank(c) for c in raw):
            continue

        def cell(name, raw=raw):
            index = columns.get(name)
            return raw[index] if index is not None and index < len(raw) else ""

        title = cell("title")
        raw_id = cell("tmdb_id")
        raw_type = cell("media_type")
        raw_date = cell("watched_date")
        raw_rating = cell("rating")
        public_note = _text(cell("public_note"))
        if public_note and len(public_note) > PUBLIC_NOTE_MAX:
            warnings.append({"row": row, "reason": f"public_note over {PUBLIC_NOTE_MAX} characters, truncated"})
            public_note = public_note[: PUBLIC_NOTE_MAX - 1].rstrip() + "…"

        tmdb_id = _parse_id(raw_id)
        if tmdb_id is None:
            reason = "missing tmdb_id" if _blank(raw_id) else f"non-numeric tmdb_id {raw_id!r}"
            warnings.append({"row": row, "reason": reason})
            continue

        media_type = "movie" if _blank(raw_type) else str(raw_type).strip().lower()
        if media_type not in MEDIA_TYPES:
            warnings.append({"row": row, "reason": f"unknown media_type {raw_type!r}"})
            continue

        watched = _parse_date(raw_date)
        if watched is None:
            reason = "missing watched_date" if _blank(raw_date) else f"unparseable watched_date {raw_date!r}"
            warnings.append({"row": row, "reason": reason})
            continue

        rating = None
        if not _blank(raw_rating):
            rating = _parse_rating(raw_rating)
            if rating is None:
                # The viewing itself is still valid, so keep it, just unrated.
                warnings.append({"row": row, "reason": f"invalid rating {raw_rating!r}, ignored"})

        viewings.append(
            Viewing(
                row=row,
                title=str(title).strip(),
                tmdb_id=tmdb_id,
                media_type=media_type,
                watched_date=watched,
                rating=rating,
                public_note=public_note,
                format=_text(cell("format")),
            )
        )

    return viewings, warnings


def _header_columns(header_row):
    """Maps each known header (case/space-insensitive) to its column index."""
    columns = {}
    for index, raw in enumerate(header_row):
        name = str(raw).strip().lower().replace(" ", "_")
        if name not in REQUIRED_HEADERS | OPTIONAL_HEADERS:
            continue
        if name in columns:
            raise SheetError(f"duplicate {name!r} column in header row")
        columns[name] = index

    missing = REQUIRED_HEADERS - columns.keys()
    if missing:
        found = [str(h) for h in header_row]
        raise SheetError(f"header row is missing {sorted(missing)} (found {found})")
    return columns


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value):
    """A trimmed string, or None for a blank cell."""
    return None if _blank(value) else " ".join(str(value).split())


def _parse_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _parse_date(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Date-time cells carry a fractional day; only the date matters here.
        if value < 1:
            return None
        return SERIAL_EPOCH + timedelta(days=int(value))
    if isinstance(value, str) and ISO_DATE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _parse_rating(value):
    if isinstance(value, bool):
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    if 0.5 <= rating <= 5 and (rating * 2).is_integer():
        return rating
    return None
