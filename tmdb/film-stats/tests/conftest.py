import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sheet_values():
    return json.loads((FIXTURES / "sheet_values.json").read_text())


@pytest.fixture
def metadata():
    raw = json.loads((FIXTURES / "metadata.json").read_text())
    out = {}
    for key, value in raw.items():
        media_type, tmdb_id = key.split("/")
        out[(media_type, int(tmdb_id))] = value
    return out
