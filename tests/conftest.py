from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_bytes():
    def load(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()
    return load


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Every test gets its own data dir + DB and no scheduler."""
    from app.config import get_settings
    from app.db import init_db

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_SECONDS", "0")
    monkeypatch.setenv("APP_PASSWORD", "")
    get_settings.cache_clear()
    init_db()
    yield
    get_settings.cache_clear()
