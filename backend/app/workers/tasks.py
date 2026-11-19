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

Video items with real tracks then get a second task, ``generate_breakdown``:
script -> voice-over -> rendered tactical clip. It runs separately so a slow or
failed render never holds up (or fails) the item's own processing, and so it
can be re-run on its own from the dashboard. Its state lives in
``item_metadata["breakdown"]``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.database import SyncSessionLocal
from app.models import DataItem, Dataset, ProcessingTask
from app.models.enums import ItemStatus, MediaType, TaskStatus
from app.services import storage
from app.workers.processing import (
    ARTIFACTS_KEY,
    process_image,
    process_text,
    process_video,
)
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


# Tracking a 90-second clip with yolo11s at 1280 px can take well over the
# app-wide 10-minute limit on a laptop CPU, so this task gets 30 minutes.
@celery_app.task(
    bind=True,
    name="process_data_item",
    max_retries=2,
    soft_time_limit=1740,
    time_limit=1800,
)
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
        sport = _infer_sport(f"{dataset.name if dataset else ''} {item.filename or ''}")
        derivative, metadata, content_type = _run_processor(item, raw, sport)
        artifacts: dict[str, tuple[bytes, str]] = metadata.pop(ARTIFACTS_KEY, {})

        # Video derivatives are JPEG, so the key's suffix has to follow suit.
        processed_key = _processed_key(
            item.raw_s3_key,
            extension=".jpg" if item.media_type == MediaType.VIDEO else None,
        )
        storage.upload_bytes(processed_key, derivative, content_type=content_type)

        base = os.path.splitext(processed_key)[0]
        for suffix, (data, artifact_type) in artifacts.items():
            key = f"{base}{suffix}"
            storage.upload_bytes(key, data, content_type=artifact_type)
            if suffix == ".tracks.json":
                metadata["tracks_key"] = key

        wants_breakdown = "tracks_key" in metadata
        if wants_breakdown:
            metadata["breakdown"] = {"status": "queued"}

        item.processed_s3_key = processed_key
        item.item_metadata = metadata
        item.status = ItemStatus.PROCESSED
        if task_row is not None:
            task_row.status = TaskStatus.SUCCESS
        # Commit before enqueueing: the breakdown worker must see this row.
        session.commit()

        if wants_breakdown:
            generate_breakdown.delay(data_item_id)

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


# ---------------------------------------------------------------------------
# Breakdown: script -> voice-over -> rendered tactical clip
# ---------------------------------------------------------------------------


def _set_breakdown(session, item: DataItem, **state: Any) -> None:
    """Replace the breakdown block. A new dict, so SQLAlchemy sees the change."""
    metadata = dict(item.item_metadata or {})
    metadata["breakdown"] = state
    item.item_metadata = metadata
    session.commit()


@celery_app.task(
    bind=True,
    name="generate_breakdown",
    max_retries=1,
    soft_time_limit=840,
    time_limit=900,
)
def generate_breakdown(self, data_item_id: int) -> dict:
    """Write, voice and render the tactical breakdown for one video item."""
    # Imported here so the API process (which imports this module to enqueue
    # tasks) never loads the rendering stack.
    from app.config import settings
    from app.workers.commentary import write_script
    from app.workers.render import render_breakdown
    from app.workers.voiceover import speak

    session = SyncSessionLocal()
    try:
        item = session.get(DataItem, data_item_id)
        if item is None:
            return {"status": "missing", "data_item_id": data_item_id}
        metadata = item.item_metadata or {}
        tracks_key = metadata.get("tracks_key")
        if not tracks_key or "analysis" not in metadata:
            _set_breakdown(
                session,
                item,
                status="unavailable",
                error="This clip has no real tracking data. Reprocess it with the YOLO tracker.",
            )
            return {"status": "unavailable", "data_item_id": data_item_id}

        _set_breakdown(session, item, status="running", stage="script")
        tracks = json.loads(storage.download_bytes(tracks_key))
        analysis = metadata["analysis"]

        script = write_script(tracks, analysis)
        _set_breakdown(session, item, status="running", stage="voice")
        speech, voice_source, voice_error = speak([s["text"] for s in script["segments"]])

        _set_breakdown(session, item, status="running", stage="render")
        suffix = os.path.splitext(item.filename or "")[1] or ".mp4"
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / f"clip{suffix}"
            clip.write_bytes(storage.download_bytes(item.raw_s3_key))
            out = Path(tmp) / "breakdown.mp4"
            render = render_breakdown(
                clip,
                out,
                tracks=tracks,
                analysis=analysis,
                script=script,
                speech=speech,
                max_dim=settings.render_max_dim,
            )
            video_key = f"{os.path.splitext(_processed_key(item.raw_s3_key))[0]}.breakdown.mp4"
            storage.upload_bytes(video_key, out.read_bytes(), content_type="video/mp4")

        _set_breakdown(
            session,
            item,
            status="ready",
            video_key=video_key,
            title=script["title"],
            segments=script["segments"],
            script_source=script["source"],
            script_fallback_reason=script["fallback_reason"],
            voice_source=voice_source,
            voice_error=voice_error,
            tracking_source=tracks["source"],
            render=render,
            generated_at=datetime.now(UTC).isoformat(),
        )
        logger.info("Breakdown for item %s -> %s", data_item_id, video_key)
        return {"status": "ready", "data_item_id": data_item_id}

    except SoftTimeLimitExceeded:
        session.rollback()
        _breakdown_failed(session, data_item_id, "rendering timed out")
        raise
    except Exception as exc:  # noqa: BLE001 - record, never touch the item's own status
        session.rollback()
        logger.exception("Breakdown failed for item %s", data_item_id)
        _breakdown_failed(session, data_item_id, f"{type(exc).__name__}: {exc}")
        return {"status": "failed", "data_item_id": data_item_id}
    finally:
        session.close()


def _breakdown_failed(session, data_item_id: int, message: str) -> None:
    try:
        item = session.get(DataItem, data_item_id)
        if item is not None:
            _set_breakdown(session, item, status="failed", error=message[:2000])
    except Exception:  # pragma: no cover - failure while recording failure
        logger.exception("Could not record breakdown failure for item %s", data_item_id)
        session.rollback()
