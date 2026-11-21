import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# never touch a real database from tests
os.environ.setdefault("WEIDE_TODAY", "2026-11-17")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "weide.db"
