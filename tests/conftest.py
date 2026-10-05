import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point every test at a fresh SQLite file."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
