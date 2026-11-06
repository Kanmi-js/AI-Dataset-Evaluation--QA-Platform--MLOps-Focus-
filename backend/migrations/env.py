"""Alembic migration environment.

Deliberately reads the DSN from ``app.config.settings.database_url_sync``
instead of ``alembic.ini`` so that migrations always target the same database
the application does — one source of truth, driven by the environment.

Uses the *sync* (psycopg2) DSN because Alembic's runner is synchronous; the
FastAPI app uses the asyncpg DSN for the same database.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings

# Importing the models package populates Base.metadata with every table, which
# is what --autogenerate diffs against.
from app.database import Base
from app.models import (  # noqa: F401 - imported for the side effect above
    DataItem,
    Dataset,
    ProcessingTask,
    User,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.database_url_sync)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations against the live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
