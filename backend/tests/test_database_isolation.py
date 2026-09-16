from pathlib import Path

from sqlalchemy.engine import make_url

from app.config.settings import Settings, _BACKEND_DIR, settings
from app.db.session import engine


def test_relative_sqlite_url_is_anchored_to_backend_directory():
    normalized = Settings.normalize_sqlite_database_url(
        "sqlite+aiosqlite:///./nested/test-claw.db?timeout=30"
    )
    url = make_url(normalized)

    assert Path(url.database or "").resolve() == (
        _BACKEND_DIR / "nested" / "test-claw.db"
    ).resolve()
    assert dict(url.query) == {"timeout": "30"}


def test_pytest_global_engine_uses_the_isolated_database():
    configured_path = Path(make_url(settings.DATABASE_URL).database or "").resolve()
    engine_path = Path(engine.url.database or "").resolve()

    assert configured_path == engine_path
    assert configured_path.name == "claw-pytest.db"
    assert configured_path != (_BACKEND_DIR / "claw.db").resolve()
