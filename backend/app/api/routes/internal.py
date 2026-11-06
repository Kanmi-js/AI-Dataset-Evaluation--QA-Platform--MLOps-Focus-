"""Internal routes called by infrastructure, not by the browser.

The S3-event Lambda (``infrastructure/lambda/s3_trigger``) posts here when an
object lands under the ``raw/`` prefix. Normally the upload endpoint has already
enqueued pre-processing, so this is a safety net: it re-drives any object that
reached the bucket without a queued task (a dropped enqueue, an out-of-band
``aws s3 cp``, a worker that died before it started).

Authentication is a shared bearer token rather than a user JWT — there is no
human on this path. The token is injected from SSM Parameter Store in
production; see ``infrastructure/terraform/secrets.tf``.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import get_db
from app.models import DataItem, ProcessingTask
from app.models.enums import ItemStatus
from app.workers.tasks import process_data_item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

# Statuses that mean "already done" — an S3 event for one of these is a
# duplicate delivery and must not restart work or clobber a human's review.
TERMINAL_STATUSES = frozenset({ItemStatus.PROCESSED, ItemStatus.REVIEWED})


async def require_internal_token(
    x_internal_token: str = Header(default=""),
) -> None:
    """Constant-time check of the shared secret guarding this router.

    The empty-string rejection is not redundant with the comparison below:
    the header defaults to "" when absent, so a misconfigured deployment that
    left the token empty would otherwise compare "" against "" and let every
    caller through.
    """
    expected = settings.internal_api_token

    if not x_internal_token or not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal token",
        )

    if not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal token",
        )


class S3EventIn(BaseModel):
    """One flattened S3 ``ObjectCreated`` record."""

    bucket: str
    key: str = Field(min_length=1, max_length=1024)


class S3EventResult(BaseModel):
    key: str
    # queued | already_processed | unknown_key | already_running
    action: str
    data_item_id: int | None = None


@router.post(
    "/s3-events",
    response_model=list[S3EventResult],
    dependencies=[Depends(require_internal_token)],
)
async def handle_s3_events(
    events: list[S3EventIn],
    db: AsyncSession = Depends(get_db),
) -> list[S3EventResult]:
    """Re-drive pre-processing for raw objects that have no completed run.

    Idempotent by design: S3 delivers events at least once, and the Lambda
    retries on failure, so the same key can arrive several times.
    """
    results: list[S3EventResult] = []

    for event in events:
        stmt = select(DataItem).where(DataItem.raw_s3_key == event.key)
        item = (await db.execute(stmt)).scalar_one_or_none()

        if item is None:
            # The object exists in S3 but no row references it — nothing to
            # attach metadata to. Log for operator follow-up; do not 404, or
            # the Lambda will retry this event forever.
            logger.warning("S3 event for unknown key %s", event.key)
            results.append(S3EventResult(key=event.key, action="unknown_key"))
            continue

        if item.status in TERMINAL_STATUSES:
            results.append(
                S3EventResult(
                    key=event.key,
                    action="already_processed",
                    data_item_id=item.id,
                )
            )
            continue

        if item.status == ItemStatus.PROCESSING:
            results.append(
                S3EventResult(
                    key=event.key,
                    action="already_running",
                    data_item_id=item.id,
                )
            )
            continue

        item.status = ItemStatus.PENDING
        item.error = None
        await db.commit()  # visible to the worker before we enqueue

        async_result = await run_in_threadpool(process_data_item.delay, item.id)
        db.add(
            ProcessingTask(data_item_id=item.id, celery_task_id=async_result.id)
        )
        await db.flush()

        results.append(
            S3EventResult(key=event.key, action="queued", data_item_id=item.id)
        )

    return results
