import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# never touch a real database from tests
os.environ.setdefault("HENHOUSE_TODAY", "2026-11-17")
# the private preview's curated feed is off unless a test turns it on
for _name in ("HENHOUSE_FEED_DIR", "HENHOUSE_CHARACTERS_DIR"):
    os.environ.pop(_name, None)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "weide.db"
