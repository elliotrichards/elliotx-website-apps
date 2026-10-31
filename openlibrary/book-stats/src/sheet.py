"""Reads the reading-log Google Sheet and validates it into Read rows."""

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
# note column that is published, shown on the site next to the book.
REQUIRED_HEADERS = {"title", "finished_date"}
OPTIONAL_HEADERS = {"author", "score", "isbn", "format", "public_note"}

# My score out of 10, a whole number. Unlike the film log, 0 is allowed.
SCORE_MIN, SCORE_MAX = 0, 10

# Longer than one line on the site is a sign the wrong column was filled in.
PUBLIC_NOTE_MAX = 280

# Sheets' date serial epoch: serial 1 is 1899-12-31, so day 0 is the 30th.
SERIAL_EPOCH = date(1899, 12, 30)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISBN = re.compile(r"^(\d{9}[\dX]|\d{13})$")


@dataclass(frozen=True)
class Read:
    row: int
    title: str
    author: str | None
    finished_date: date
    # Out of 10, 0 to 10, or None if unscored.
    score: int | None
    # Digits only (and a final X for ISBN-10), or None.
    isbn: str | None = None
    # Free text, e.g. paperback, ebook, audiobook; the site groups by it lower-cased.
    format: str | None = None
    public_note: str | None = None


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
    Validates raw rows (header row first) into Reads. A row with no title is
    skipped quietly, as a placeholder; any other bad row is skipped and
    recorded in warnings (sheet row number + reason) instead of failing the
    run. A bad header row raises SheetError instead: every row would be
    misread, so publishing an empty books.json would be worse than failing.
    """
    if not values:
        raise SheetError("sheet is empty: expected a header row")
    columns = _header_columns(values[0])

    reads, warnings = [], []

    for offset, raw in enumerate(values[1:]):
        row = FIRST_DATA_ROW + offset

        def cell(name, raw=raw):
            index = columns.get(name)
            return raw[index] if index is not None and index < len(raw) else ""

        title = _text(cell("title"))
        if title is None:
            continue

        raw_date = cell("finished_date")
        finished = _parse_date(raw_date)
        if finished is None:
            reason = "missing finished_date" if _blank(raw_date) else f"unparseable finished_date {raw_date!r}"
            warnings.append({"row": row, "reason": reason})
            continue

        # A bad score or ISBN warns but the read itself is still valid, so
        # it's kept: unscored, or looked up by title and author instead.
        score = None
        raw_score = cell("score")
        if not _blank(raw_score):
            score = _parse_score(raw_score)
            if score is None:
                warnings.append({"row": row, "reason": f"invalid score {raw_score!r}, ignored"})

        isbn = None
        raw_isbn = cell("isbn")
        if not _blank(raw_isbn):
            isbn = _parse_isbn(raw_isbn)
            if isbn is None:
                warnings.append({"row": row, "reason": f"invalid isbn {raw_isbn!r}, looked up by title instead"})

        public_note = _text(cell("public_note"))
        if public_note and len(public_note) > PUBLIC_NOTE_MAX:
            warnings.append({"row": row, "reason": f"public_note over {PUBLIC_NOTE_MAX} characters, truncated"})
            public_note = public_note[: PUBLIC_NOTE_MAX - 1].rstrip() + "…"

        reads.append(
            Read(
                row=row,
                title=title,
                author=_text(cell("author")),
                finished_date=finished,
                score=score,
                isbn=isbn,
                format=_text(cell("format")),
                public_note=public_note,
            )
        )

    return reads, warnings


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


def _parse_score(value):
    """A whole number 0 to 10; 7, 7.0 and "7" all count."""
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if SCORE_MIN <= score <= SCORE_MAX and score.is_integer():
        return int(score)
    return None


def _parse_isbn(value):
    """
    An ISBN-10 or -13 with hyphens and spaces removed. A cell Sheets treats
    as a number comes back as an int (or a float); an ISBN-10 starting with
    0 loses that 0 on the way in, so a 9-digit number gets it back.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int):
        text = str(value)
        if len(text) == 9:
            text = "0" + text
    else:
        text = re.sub(r"[\s-]", "", str(value)).upper()
    return text if ISBN.match(text) else None
