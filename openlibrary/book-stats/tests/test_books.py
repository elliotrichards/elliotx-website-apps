from datetime import UTC, date, datetime

from books import SCHEMA_VERSION, books_export
from sheet import Read

GENERATED = datetime(2026, 10, 31, 5, 0, 7, tzinfo=UTC)


def read(row, title, day, isbn=None, score=None, author="Author", **extra):
    return Read(
        row=row, title=title, author=author, finished_date=date.fromisoformat(day), score=score, isbn=isbn, **extra
    )


def test_export_shape_and_order():
    reads = [
        read(3, "Second", "2026-10-12", format="ebook"),
        read(2, "First", "2026-10-01", isbn="9780099598244", score=0, public_note="Fun"),
    ]
    metadata = {
        "isbn/9780099598244": {
            "work_key": "OL1W",
            "cover_id": 111,
            "first_publish_year": 2015,
            "pages": 352,
            "subjects": ["Thrillers"],
        },
        "title/second|author": None,
    }
    out = books_export(reads, metadata, generated_at=GENERATED, warnings=[{"row": 9, "reason": "x"}])

    assert out["schemaVersion"] == SCHEMA_VERSION
    assert out["generatedAt"] == "2026-10-31T05:00:07Z"
    assert out["warnings"] == [{"row": 9, "reason": "x"}]
    assert [r["title"] for r in out["reads"]] == ["First", "Second"]
    assert out["reads"][0] == {
        "id": "2026-10-01-first",
        "book": "isbn/9780099598244",
        "title": "First",
        "author": "Author",
        "finishedOn": "2026-10-01",
        "order": 1,
        "score": 0,
        "format": None,
        "note": "Fun",
        "isbn": "9780099598244",
        "reread": False,
    }
    assert out["books"]["isbn/9780099598244"] == {
        "matched": True,
        "workKey": "OL1W",
        "coverId": 111,
        "firstPublishYear": 2015,
        "pages": 352,
        "subjects": ["Thrillers"],
    }
    # No match still gets an entry, so the site can render it with a placeholder.
    assert out["books"]["title/second|author"] == {
        "matched": False,
        "workKey": None,
        "coverId": None,
        "firstPublishYear": None,
        "pages": None,
        "subjects": [],
    }


def test_never_publishes_private_notes():
    out = books_export([read(2, "A", "2026-01-01")], {}, generated_at=GENERATED)
    assert "notes" not in out["reads"][0]


def test_reread_matches_the_work_across_editions():
    meta = {"isbn/1111111111": {"work_key": "OL1W"}, "isbn/2222222222": {"work_key": "OL1W"}}
    out = books_export(
        [
            read(2, "Dune", "2020-01-01", isbn="1111111111"),
            read(3, "Dune", "2024-01-01", isbn="2222222222"),
            read(4, "Unmatched", "2024-02-01"),
            read(5, "unmatched", "2025-02-01"),
        ],
        meta,
        generated_at=GENERATED,
    )
    assert [r["reread"] for r in out["reads"]] == [False, True, False, True]


def test_same_day_ids_and_order():
    out = books_export(
        [read(2, "A", "2026-01-01"), read(3, "A", "2026-01-01", author="Other"), read(4, "B", "2026-01-01")],
        {},
        generated_at=GENERATED,
    )
    assert [(r["id"], r["order"]) for r in out["reads"]] == [
        ("2026-01-01-a", 1),
        ("2026-01-01-a-2", 2),
        ("2026-01-01-b", 3),
    ]
