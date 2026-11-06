"""Dataset and DataItem schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DatasetStatus, ItemStatus, MediaType


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class DataItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dataset_id: int
    filename: str
    media_type: MediaType
    status: ItemStatus
    raw_s3_key: str
    processed_s3_key: str | None = None
    size_bytes: int | None = None
    item_metadata: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class DatasetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    status: DatasetStatus
    owner_id: int
    created_at: datetime
    item_count: int = 0


class DatasetWithItems(DatasetRead):
    items: list[DataItemRead] = Field(default_factory=list)


class AnnotationUpdate(BaseModel):
    """Payload sent by the QA dashboard when a reviewer saves annotations."""

    annotations: dict[str, Any]
    mark_reviewed: bool = True
