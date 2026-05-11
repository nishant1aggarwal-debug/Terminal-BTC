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


def _migrate_missing_columns() -> None:
    """Postgres-only: add any model columns that aren't on the live tables yet.

    SQLModel.metadata.create_all() creates new TABLES but never adds columns to
    pre-existing ones. When we add a field to a model after a deploy, Postgres
    keeps the old schema and the next SELECT raises UndefinedColumn. SQLite
    tests always start fresh so this isn't visible locally.

    We introspect the model's column list vs information_schema and emit
    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` for the gap. Idempotent and
    cheap — runs on every boot.
    """
    if not engine.url.drivername.startswith("postgresql"):
        return
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table in SQLModel.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        live_cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in live_cols:
                continue
            try:
                type_sql = col.type.compile(engine.dialect)
                ddl = (
                    f'ALTER TABLE "{table.name}" '
                    f'ADD COLUMN IF NOT EXISTS "{col.name}" {type_sql}'
                )
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception:
                # Best-effort — never block startup on a migration glitch.
                pass


def init_db() -> None:
    # Import models so SQLModel.metadata is populated before create_all.
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _migrate_missing_columns()


def get_session() -> Session:
    return Session(engine)
