"""Dataset and DataItem models.

A ``Dataset`` is a named collection owned by a user. Each ``DataItem`` is a
single uploaded file (image or text) that flows through the pre-processing and
human QA pipeline. Raw bytes live in S3; only keys + metadata live in Postgres.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import DatasetStatus, ItemStatus, MediaType

# PostgreSQL gets real JSONB (indexable, binary); SQLite — used by the test
# suite so it runs without a database container — falls back to plain JSON.
JSONVariant = JSON().with_variant(JSONB, "postgresql")
EMPTY_JSON = text("'{}'")


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DatasetStatus] = mapped_column(
        Enum(DatasetStatus, name="dataset_status"),
        default=DatasetStatus.ACTIVE,
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    owner: Mapped["User"] = relationship(back_populates="datasets")  # noqa: F821
    items: Mapped[list["DataItem"]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
    )


class DataItem(Base):
    __tablename__ = "data_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, name="media_type")
    )
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus, name="item_status"),
        default=ItemStatus.PENDING,
        index=True,
    )

    # S3 object keys for raw upload and pre-processed derivative.
    # raw_s3_key is indexed because the S3-event Lambda looks items up by it.
    raw_s3_key: Mapped[str] = mapped_column(String(1024), index=True)
    processed_s3_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Free-form, worker-populated results (dimensions, detected regions, stats).
    item_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, default=dict, nullable=False, server_default=EMPTY_JSON
    )
    # Human QA output (labels, bounding boxes, notes).
    annotations: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, default=dict, nullable=False, server_default=EMPTY_JSON
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    dataset: Mapped["Dataset"] = relationship(back_populates="items")
    tasks: Mapped[list["ProcessingTask"]] = relationship(  # noqa: F821
        back_populates="data_item",
        cascade="all, delete-orphan",
    )
