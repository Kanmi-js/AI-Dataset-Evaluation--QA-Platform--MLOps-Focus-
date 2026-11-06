"""Shared enumerations used across models, schemas, and workers."""

import enum


class MediaType(str, enum.Enum):
    """Kind of raw data stored in a dataset item.

    Adding a member here needs three other edits: a migration adding the label
    to the PostgreSQL ``media_type`` type (SQLAlchemy persists member *names*,
    so the label is uppercase), a branch in
    ``app.workers.tasks.process_data_item``, and inference in
    ``app.api.routes.uploads._infer_media_type``.
    """

    IMAGE = "image"
    TEXT = "text"
    VIDEO = "video"


class ItemStatus(str, enum.Enum):
    """Lifecycle of a single data item through the QA pipeline."""

    PENDING = "pending"          # uploaded, awaiting pre-processing
    PROCESSING = "processing"    # Celery worker is running
    PROCESSED = "processed"      # pre-processing succeeded, ready for QA
    REVIEWED = "reviewed"        # a human has completed QA/annotation
    FAILED = "failed"            # pre-processing errored


class DatasetStatus(str, enum.Enum):
    """Aggregate status of a dataset."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class TaskStatus(str, enum.Enum):
    """Status of a background processing task."""

    QUEUED = "queued"
    STARTED = "started"
    SUCCESS = "success"
    FAILURE = "failure"
