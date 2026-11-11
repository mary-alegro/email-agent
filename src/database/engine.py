"""Database engine and session helpers (SQLite)."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from src.config import settings
from src.database import models  # noqa: F401  (registers the tables with SQLModel.metadata)


def make_engine(url: str = settings.database_url) -> Engine:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, connect_args={"check_same_thread": False})

    # SQLite ignores foreign keys unless enabled per connection.
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


def init_db(engine: Engine) -> None:
    """Create any missing tables."""
    SQLModel.metadata.create_all(engine)


def reset_db(engine: Engine) -> None:
    """Drop and recreate all tables."""
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


engine = make_engine()


def get_session() -> Iterator[Session]:
    """One session per request (a FastAPI dependency)."""
    with Session(engine) as session:
        yield session
