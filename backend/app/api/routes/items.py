"""Data item routes: detail with presigned URLs, annotations, reprocess,
and the tactical breakdown video."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.database import get_db
from app.models import DataItem, Dataset, ProcessingTask, User
from app.models.enums import ItemStatus
from app.schemas import DataItemRead
from app.schemas.dataset import AnnotationUpdate
from app.services import storage
from app.workers.tasks import generate_breakdown, process_data_item

router = APIRouter(prefix="/items", tags=["items"])


class DataItemDetail(DataItemRead):
    """Item detail enriched with temporary URLs the browser can load."""

    raw_url: str | None = None
    processed_url: str | None = None
    # The rendered breakdown clip, once ``item_metadata.breakdown.status`` is
    # "ready".
    breakdown_url: str | None = None


BREAKDOWN_IN_FLIGHT = {"queued", "running"}


async def _get_owned_item(item_id: int, db: AsyncSession, user: User) -> DataItem:
    stmt = (
        select(DataItem)
        .join(Dataset, Dataset.id == DataItem.dataset_id)
        .where(DataItem.id == item_id, Dataset.owner_id == user.id)
    )
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
        )
    return item


@router.get("/{item_id}", response_model=DataItemDetail)
async def get_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DataItemDetail:
    item = await _get_owned_item(item_id, db, user)
    detail = DataItemDetail.model_validate(item, from_attributes=True)
    detail.raw_url = storage.generate_presigned_url(item.raw_s3_key)
    if item.processed_s3_key:
        detail.processed_url = storage.generate_presigned_url(item.processed_s3_key)
    breakdown = (item.item_metadata or {}).get("breakdown") or {}
    if breakdown.get("status") == "ready" and breakdown.get("video_key"):
        detail.breakdown_url = storage.generate_presigned_url(breakdown["video_key"])
    return detail


@router.patch("/{item_id}/annotations", response_model=DataItemRead)
async def save_annotations(
    item_id: int,
    payload: AnnotationUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DataItem:
    item = await _get_owned_item(item_id, db, user)
    item.annotations = payload.annotations
    if payload.mark_reviewed:
        item.status = ItemStatus.REVIEWED
    await db.flush()
    await db.refresh(item)
    return item


@router.post("/{item_id}/reprocess", response_model=DataItemRead)
async def reprocess_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DataItem:
    item = await _get_owned_item(item_id, db, user)
    item.status = ItemStatus.PENDING
    item.error = None
    # Commit before enqueuing so the worker cannot observe (or overwrite) a
    # half-reset row — see the same guard in uploads.upload_items.
    await db.commit()

    # `updated_at` uses a server-side `onupdate=func.now()`, so the UPDATE above
    # leaves it expired and SQLAlchemy has to SELECT the new value back.
    # `expire_on_commit=False` does not cover that. FastAPI serialises the
    # response *outside* this coroutine's greenlet context, where a lazy reload
    # raises MissingGreenlet and the route 500s — so reload it here, while we
    # still can. Same reason save_annotations refreshes after its flush.
    await db.refresh(item)

    async_result = await run_in_threadpool(process_data_item.delay, item.id)
    db.add(ProcessingTask(data_item_id=item.id, celery_task_id=async_result.id))
    await db.flush()
    return item


@router.post("/{item_id}/breakdown", response_model=DataItemRead)
async def regenerate_breakdown(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DataItem:
    """Re-run script, voice-over and render for a tracked clip.

    Tracking is not repeated — use ``/reprocess`` for that. Useful after adding
    an API key, or to get a fresh take on the commentary.
    """
    item = await _get_owned_item(item_id, db, user)
    metadata = dict(item.item_metadata or {})
    if not metadata.get("tracks_key"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This item has no tracking data to break down. Reprocess the clip first.",
        )
    if (metadata.get("breakdown") or {}).get("status") in BREAKDOWN_IN_FLIGHT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A breakdown is already being generated for this item.",
        )
    metadata["breakdown"] = {"status": "queued"}
    item.item_metadata = metadata
    # Commit before enqueuing: the worker must see the queued state, or its
    # first write could be overwritten by this transaction.
    await db.commit()
    await db.refresh(item)

    await run_in_threadpool(generate_breakdown.delay, item.id)
    return item
