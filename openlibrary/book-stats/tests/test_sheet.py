from datetime import date

import pytest

from sheet import Read, SheetError, parse_rows

HEADER = ["finished_date", "title", "author", "score", "isbn", "format", "public_note", "notes"]


def parse(*rows, header=HEADER):
    return parse_rows([header, *rows])


def test_reads_a_full_row_by_header_name():
    reads, warnings = parse(
        ["2026-10-12", "In a Dark, Dark Wood", "Ruth Ware", "5", "9780099598244", "paperback", "Twisty", "private"]
    )
    assert warnings == []
    assert reads == [
        Read(
            row=2,
            title="In a Dark, Dark Wood",
            author="Ruth Ware",
            finished_date=date(2026, 10, 12),
            score=5,
            isbn="9780099598244",
            format="paperback",
            public_note="Twisty",
        )
    ]


def test_category_defaults_to_read_and_extras_are_empty():
    reads, warnings = parse(["2026-10-12", "Book"])
    assert warnings == []
    assert (reads[0].category, reads[0].tags, reads[0].series) == ("read", (), None)


def test_reads_category_tags_and_series():
    header = [*HEADER, "category", "tags", "series"]
    row = ["2026-02-21", "Why Mummy Swears", *[""] * 6, "Read", " Fiction, humour ,FICTION,,", "Why Mummy #2"]
    reads, warnings = parse(row, header=header)
    assert warnings == []
    assert reads[0].category == "read"
    assert reads[0].tags == ("fiction", "humour")  # trimmed, lower-cased, de-duplicated, in order typed
    assert reads[0].series == "Why Mummy #2"


@pytest.mark.parametrize("category", ["reading-list", "currently-reading", "Reading List"])
def test_unfinished_books_need_no_date(category):
    reads, warnings = parse(["", "Artemis", "Andy Weir", "", "", "", "", "", category], header=[*HEADER, "category"])
    assert warnings == []
    assert reads[0].finished_date is None
    assert reads[0].category in ("reading-list", "currently-reading")


def test_a_wrong_date_on_an_unfinished_book_still_warns():
    reads, warnings = parse(["12/10/2026", "A", "", "", "", "", "", "", "reading-list"], header=[*HEADER, "category"])
    assert reads == []
    assert warnings == [{"row": 2, "reason": "unparseable finished_date '12/10/2026'"}]


def test_unknown_category_skips_the_row_with_a_warning():
    reads, warnings = parse(["2026-01-01", "A", "", "", "", "", "", "", "finished"], header=[*HEADER, "category"])
    assert reads == []
    assert warnings == [{"row": 2, "reason": "unknown category 'finished', row skipped"}]


def test_never_reads_notes():
    reads, _ = parse(["2026-10-12", "Book", "", "", "", "", "", "secret"])
    assert "secret" not in repr(reads)


def test_columns_can_be_in_any_order_and_extras_are_ignored():
    reads, _ = parse([7, "Klara and the Sun", "x", 46000], header=["score", "Title", "extra", "Finished Date"])
    assert reads[0].title == "Klara and the Sun"
    assert reads[0].score == 7
    assert reads[0].finished_date == date(2025, 12, 9)  # serial 46000
    assert reads[0].author is None


def test_skips_rows_with_no_title_quietly():
    reads, warnings = parse(["2026-10-12", "", "Someone", 8], ["", "  "], [])
    assert reads == [] and warnings == []


def test_missing_or_bad_date_skips_the_row_with_a_warning():
    reads, warnings = parse(["", "A"], ["12/10/2026", "B"])
    assert reads == []
    assert warnings == [
        {"row": 2, "reason": "missing finished_date"},
        {"row": 3, "reason": "unparseable finished_date '12/10/2026'"},
    ]


@pytest.mark.parametrize(("raw", "score"), [(0, 0), ("10", 10), (7.0, 7), ("0", 0)])
def test_scores_zero_to_ten(raw, score):
    reads, warnings = parse(["2026-01-01", "A", "", raw])
    assert reads[0].score == score and warnings == []


@pytest.mark.parametrize("raw", [11, -1, 7.5, "great", True])
def test_bad_score_warns_and_keeps_the_read(raw):
    reads, warnings = parse(["2026-01-01", "A", "", raw])
    assert reads[0].score is None
    assert warnings[0]["row"] == 2 and "invalid score" in warnings[0]["reason"]


@pytest.mark.parametrize(
    ("raw", "isbn"),
    [
        ("978-0-09-959824-4", "9780099598244"),
        (9780099598244, "9780099598244"),
        (9780099598244.0, "9780099598244"),
        (571334652, "0571334652"),  # a numeric cell drops ISBN-10's leading 0
        ("0-8044-2957-x", "080442957X"),
    ],
)
def test_isbn_is_normalised(raw, isbn):
    reads, warnings = parse(["2026-01-01", "A", "", "", raw])
    assert reads[0].isbn == isbn and warnings == []


def test_bad_isbn_warns_and_falls_back_to_title():
    reads, warnings = parse(["2026-01-01", "A", "", "", "12345"])
    assert reads[0].isbn is None
    assert warnings == [{"row": 2, "reason": "invalid isbn '12345', looked up by title instead"}]


def test_long_public_note_is_truncated():
    reads, warnings = parse(["2026-01-01", "A", "", "", "", "", "x" * 300])
    assert len(reads[0].public_note) == 280 and reads[0].public_note.endswith("…")
    assert "truncated" in warnings[0]["reason"]


def test_header_problems_fail_the_run():
    with pytest.raises(SheetError, match="missing"):
        parse_rows([["title", "author"]])
    with pytest.raises(SheetError, match="duplicate"):
        parse_rows([["title", "finished_date", "Title"]])
    with pytest.raises(SheetError, match="empty"):
        parse_rows([])
