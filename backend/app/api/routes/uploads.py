"""Upload routes: accept files into a dataset and enqueue pre-processing."""

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.config import settings
from app.database import get_db
from app.models import DataItem, Dataset, ProcessingTask, User
from app.models.enums import ItemStatus, MediaType
from app.schemas import DataItemRead
from app.services import storage
from app.workers.tasks import process_data_item

router = APIRouter(prefix="/datasets", tags=["uploads"])

# Guard rails for the demo pipeline. Module-level rather than read from
# `settings` at the call site so tests can monkeypatch them.
MAX_FILE_BYTES = settings.max_upload_bytes
MAX_VIDEO_BYTES = settings.max_video_upload_bytes

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")


def _infer_media_type(content_type: str | None, filename: str) -> MediaType:
    """Classify an upload, preferring the MIME type and falling back to suffix.

    The fallback matters: browsers send ``application/octet-stream`` for plenty
    of real files, and a misclassified clip would be handed to the text
    processor.
    """
    if content_type:
        if content_type.startswith("image/"):
            return MediaType.IMAGE
        if content_type.startswith("video/"):
            return MediaType.VIDEO

    lowered = filename.lower()
    if lowered.endswith(IMAGE_SUFFIXES):
        return MediaType.IMAGE
    if lowered.endswith(VIDEO_SUFFIXES):
        return MediaType.VIDEO
    return MediaType.TEXT


def _max_bytes_for(media_type: MediaType) -> int:
    """Per-type size cap; video gets its own because clips are far larger."""
    return MAX_VIDEO_BYTES if media_type is MediaType.VIDEO else MAX_FILE_BYTES


@router.post(
    "/{dataset_id}/items",
    response_model=list[DataItemRead],
    status_code=status.HTTP_201_CREATED,
)
async def upload_items(
    dataset_id: int,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DataItem]:
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None or dataset.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found"
        )

    await storage.ensure_bucket_async()
    created: list[DataItem] = []

    for upload in files:
        # NOTE: this buffers the whole file in memory before it reaches S3, so
        # the video cap above is also the per-request memory cost. Streaming
        # straight to S3 is the follow-up — see "Known limits" in the README.
        data = await upload.read()
        media_type = _infer_media_type(upload.content_type, upload.filename or "")
        limit = _max_bytes_for(media_type)
        if len(data) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{upload.filename} exceeds the {limit} byte limit",
            )

        raw_key = f"raw/{dataset_id}/{uuid.uuid4().hex}_{upload.filename}"
        await storage.upload_bytes_async(
            raw_key,
            data,
            content_type=upload.content_type or "application/octet-stream",
        )

        item = DataItem(
            dataset_id=dataset_id,
            filename=upload.filename or raw_key,
            media_type=media_type,
            status=ItemStatus.PENDING,
            raw_s3_key=raw_key,
            size_bytes=len(data),
        )
        db.add(item)
        created.append(item)

    await db.flush()
    for item in created:
        await db.refresh(item)

    # Commit BEFORE enqueuing. The Celery worker runs in another process against
    # the same database; if it picks the task up while this transaction is still
    # open, `session.get(DataItem, id)` returns None and the upload is silently
    # dropped. `expire_on_commit=False` keeps `created` usable afterwards.
    await db.commit()

    for item in created:
        # .delay() is a blocking round-trip to Redis — keep it off the loop.
        async_result = await run_in_threadpool(process_data_item.delay, item.id)
        db.add(
            ProcessingTask(
                data_item_id=item.id,
                celery_task_id=async_result.id,
            )
        )

    await db.flush()
    return created
