"""initial schema: users, datasets, data_items, processing_tasks

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-11

Mirrors the SQLAlchemy models in ``app/models`` at the point this project
switched from ``Base.metadata.create_all`` (dev only, see app/main.py) to
managed migrations.

Note on enum labels: SQLAlchemy's ``Enum(PyEnum)`` persists each member's
*name*, not its value, so the PostgreSQL types below use ``PENDING`` rather
than ``pending``. The API still returns lowercase values — Pydantic serialises
the enum value on the way out.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# JSONB on PostgreSQL, plain JSON elsewhere — matches app/models/dataset.py.
JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB, "postgresql")
EMPTY_JSON = sa.text("'{}'")

MEDIA_TYPE = sa.Enum("IMAGE", "TEXT", name="media_type")
ITEM_STATUS = sa.Enum(
    "PENDING", "PROCESSING", "PROCESSED", "REVIEWED", "FAILED", name="item_status"
)
DATASET_STATUS = sa.Enum("ACTIVE", "ARCHIVED", name="dataset_status")
TASK_STATUS = sa.Enum("QUEUED", "STARTED", "SUCCESS", "FAILURE", name="task_status")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", DATASET_STATUS, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_datasets_name", "datasets", ["name"])
    op.create_index("ix_datasets_owner_id", "datasets", ["owner_id"])

    op.create_table(
        "data_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("media_type", MEDIA_TYPE, nullable=False),
        sa.Column("status", ITEM_STATUS, nullable=False),
        sa.Column("raw_s3_key", sa.String(length=1024), nullable=False),
        sa.Column("processed_s3_key", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "item_metadata", JSON_VARIANT, server_default=EMPTY_JSON, nullable=False
        ),
        sa.Column(
            "annotations", JSON_VARIANT, server_default=EMPTY_JSON, nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_data_items_dataset_id", "data_items", ["dataset_id"])
    op.create_index("ix_data_items_status", "data_items", ["status"])
    # Looked up by the S3-event Lambda (app/api/routes/internal.py).
    op.create_index("ix_data_items_raw_s3_key", "data_items", ["raw_s3_key"])

    op.create_table(
        "processing_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("data_item_id", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("status", TASK_STATUS, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["data_item_id"], ["data_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_processing_tasks_data_item_id", "processing_tasks", ["data_item_id"]
    )
    op.create_index(
        "ix_processing_tasks_celery_task_id", "processing_tasks", ["celery_task_id"]
    )


def downgrade() -> None:
    op.drop_table("processing_tasks")
    op.drop_table("data_items")
    op.drop_table("datasets")
    op.drop_table("users")

    # drop_table leaves PostgreSQL enum types behind; remove them explicitly so
    # a downgrade/upgrade cycle does not fail on "type already exists".
    bind = op.get_bind()
    for enum_type in (TASK_STATUS, ITEM_STATUS, MEDIA_TYPE, DATASET_STATUS):
        enum_type.drop(bind, checkfirst=True)
