from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_settings = get_settings()


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


# SQLite needs a couple of local niceties (directory creation, thread-safety flag).
# Postgres/MySQL etc just need the URL and a pool — we enable pool_pre_ping so
# connections dropped by Render/Neon idle timeouts get recycled transparently.
if _is_sqlite(_settings.database_url):
    sqlite_path = _settings.database_url.replace("sqlite:///", "", 1)
    if sqlite_path and sqlite_path != ":memory:":
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False}
    engine_kwargs: dict = {"connect_args": connect_args}
else:
    engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 300}

engine = create_engine(_settings.database_url, echo=False, **engine_kwargs)


def init_db() -> None:
    # Import models so SQLModel.metadata is populated before create_all.
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
