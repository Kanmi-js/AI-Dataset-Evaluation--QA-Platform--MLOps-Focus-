"""Celery tasks orchestrating the pre-processing pipeline.

Flow per data item:
    1. Mark item PROCESSING.
    2. Download raw bytes from S3.
    3. Run pre-processing for the item's media type
       (``app.workers.processing``): edge overlay for images, statistics for
       text, poster frame plus tracking telemetry for video.
    4. Upload the processed derivative back to S3.
    5. Persist metadata + processed key, mark item PROCESSED.
On any failure the item is marked FAILED with the error recorded.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.database import SyncSessionLocal
from app.models import DataItem, Dataset, ProcessingTask
from app.models.enums import ItemStatus, MediaType, TaskStatus
from app.services import storage
from app.workers.processing import process_image, process_text, process_video
from app.workers.telemetry import DEFAULT_SPORT, PROFILES

logger = logging.getLogger(__name__)


def _processed_key(raw_key: str, *, extension: str | None = None) -> str:
    """Derive the processed object key from the raw key.

    ``extension`` overrides the suffix for processors whose output is a
    different format to their input — a video's derivative is a JPEG poster
    frame, so keeping ``.mp4`` would mislabel it.
    """
    if raw_key.startswith("raw/"):
        base = raw_key[len("raw/") :]
    else:
        base = raw_key
    if extension:
        base = f"{os.path.splitext(base)[0]}{extension}"
    return f"processed/{base}"


def _infer_sport(dataset_name: str | None) -> str:
    """Pick a sport profile from the dataset's name.

    A heuristic, and deliberately a shallow one: the sport only selects which
    surface dimensions and entity cast the simulation uses. Making it an
    explicit field on Dataset is the follow-up if this ever drives real
    analysis.
    """
    lowered = (dataset_name or "").lower()
    for name in PROFILES:
        if name in lowered:
            return name
    if "court" in lowered or "nba" in lowered:
        return "basketball"
    return DEFAULT_SPORT


def _run_processor(
    item: DataItem, raw: bytes, sport: str
) -> tuple[bytes, dict[str, Any], str]:
    """Dispatch on media type. Unhandled types fail loudly, by design."""
    if item.media_type == MediaType.IMAGE:
        return process_image(raw)
    if item.media_type == MediaType.TEXT:
        return process_text(raw)
    if item.media_type == MediaType.VIDEO:
        suffix = os.path.splitext(item.filename or "")[1]
        return process_video(raw, sport=sport, suffix=suffix or ".mp4")
    # Previously an `else` that handed anything unrecognised to the text
    # processor, which turned a new MediaType into silently corrupt output
    # rather than an error.
    raise ValueError(f"No processor for media type {item.media_type!r}")


@celery_app.task(bind=True, name="process_data_item", max_retries=2)
def process_data_item(self, data_item_id: int) -> dict:
    """Process a single :class:`DataItem` end to end."""
    session = SyncSessionLocal()
    try:
        item = session.get(DataItem, data_item_id)
        if item is None:
            logger.warning("DataItem %s no longer exists", data_item_id)
            return {"status": "missing", "data_item_id": data_item_id}

        # Track the task row (created by the API when it enqueued the job).
        task_row = (
            session.query(ProcessingTask)
            .filter(ProcessingTask.celery_task_id == self.request.id)
            .one_or_none()
        )
        if task_row is not None:
            task_row.status = TaskStatus.STARTED

        item.status = ItemStatus.PROCESSING
        item.error = None
        session.commit()

        raw = storage.download_bytes(item.raw_s3_key)

        dataset = session.get(Dataset, item.dataset_id)
        sport = _infer_sport(dataset.name if dataset else None)
        derivative, metadata, content_type = _run_processor(item, raw, sport)

        # Video derivatives are JPEG, so the key's suffix has to follow suit.
        processed_key = _processed_key(
            item.raw_s3_key,
            extension=".jpg" if item.media_type == MediaType.VIDEO else None,
        )
        storage.upload_bytes(processed_key, derivative, content_type=content_type)

        item.processed_s3_key = processed_key
        item.item_metadata = metadata
        item.status = ItemStatus.PROCESSED
        if task_row is not None:
            task_row.status = TaskStatus.SUCCESS
        session.commit()

        logger.info("Processed data item %s -> %s", data_item_id, processed_key)
        return {"status": "processed", "data_item_id": data_item_id}

    except SoftTimeLimitExceeded:
        session.rollback()
        _mark_failed(session, data_item_id, self.request.id, "processing timed out")
        raise
    except Exception as exc:  # noqa: BLE001 - persist then optionally retry
        session.rollback()
        logger.exception("Failed to process data item %s", data_item_id)
        _mark_failed(session, data_item_id, self.request.id, str(exc))
        # Retry transient errors (e.g. S3 hiccup) with exponential backoff.
        raise self.retry(exc=exc, countdown=2 ** self.request.retries) from exc
    finally:
        session.close()


def _mark_failed(session, data_item_id: int, celery_task_id: str, message: str) -> None:
    """Best-effort update of item + task rows to a failed state."""
    try:
        item = session.get(DataItem, data_item_id)
        if item is not None:
            item.status = ItemStatus.FAILED
            item.error = message[:2000]
        task_row = (
            session.query(ProcessingTask)
            .filter(ProcessingTask.celery_task_id == celery_task_id)
            .one_or_none()
        )
        if task_row is not None:
            task_row.status = TaskStatus.FAILURE
            task_row.error = message[:2000]
        session.commit()
    except Exception:  # pragma: no cover - failure while recording failure
        logger.exception("Could not record failure for item %s", data_item_id)
        session.rollback()
