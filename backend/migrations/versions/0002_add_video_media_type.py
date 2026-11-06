"""add VIDEO to the media_type enum

Revision ID: 0002_add_video_media_type
Revises: 0001_initial
Create Date: 2026-10-04

TerrainAI accepts match clips, so ``MediaType`` gained a ``VIDEO`` member
(``app/models/enums.py``). The label is uppercase because SQLAlchemy's
``Enum(PyEnum)`` persists member *names* — same convention as 0001.

PostgreSQL 12+ allows ``ALTER TYPE ... ADD VALUE`` inside a transaction (so it
is safe under Alembic's transactional DDL) provided the new label is not *used*
in the same transaction. This migration only adds it, so that holds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_video_media_type"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_NAME = "media_type"
NEW_LABEL = "VIDEO"
PRE_EXISTING_LABELS = ("IMAGE", "TEXT")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Only PostgreSQL has a native enum type to alter. Elsewhere (SQLite in
        # tests) `sa.Enum` is a VARCHAR + CHECK constraint created by
        # `Base.metadata.create_all`, which already reflects the new member.
        return

    op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_LABEL}'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # PostgreSQL cannot drop an enum label, so removing it means building a
    # replacement type and re-pointing the column at it. CI exercises this path
    # (`alembic downgrade base` in .github/workflows/ci.yml), so it has to work
    # rather than just raise NotImplementedError.
    remaining = bind.execute(
        # `media_type::text` because comparing the enum column directly against
        # a bound text parameter has no matching operator in PostgreSQL.
        sa.text("SELECT count(*) FROM data_items WHERE media_type::text = :label"),
        {"label": NEW_LABEL},
    ).scalar_one()
    if remaining:
        # Casting these rows would fail anyway; say so clearly instead of
        # letting Postgres raise an opaque invalid-input-value error, and do not
        # silently delete the operator's data.
        raise RuntimeError(
            f"Cannot remove the '{NEW_LABEL}' label: {remaining} data_items row(s) "
            "still use it. Delete or re-classify those items, then retry the "
            "downgrade."
        )

    labels = ", ".join(f"'{label}'" for label in PRE_EXISTING_LABELS)
    op.execute(f"ALTER TYPE {ENUM_NAME} RENAME TO {ENUM_NAME}_old")
    op.execute(f"CREATE TYPE {ENUM_NAME} AS ENUM ({labels})")
    op.execute(
        "ALTER TABLE data_items ALTER COLUMN media_type "
        f"TYPE {ENUM_NAME} USING media_type::text::{ENUM_NAME}"
    )
    op.execute(f"DROP TYPE {ENUM_NAME}_old")
