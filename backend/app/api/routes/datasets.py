"""Dataset CRUD routes."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.database import get_db
from app.models import DataItem, Dataset, User
from app.models.enums import DatasetStatus
from app.schemas import DatasetCreate, DatasetRead, DatasetWithItems

router = APIRouter(prefix="/datasets", tags=["datasets"])


async def _get_owned_dataset(
    dataset_id: int, db: AsyncSession, user: User
) -> Dataset:
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None or dataset.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found"
        )
    return dataset


@router.post("", response_model=DatasetRead, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    payload: DatasetCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DatasetRead:
    dataset = Dataset(
        name=payload.name,
        description=payload.description,
        owner_id=user.id,
    )
    db.add(dataset)
    await db.flush()
    await db.refresh(dataset)
    return DatasetRead.model_validate(dataset, from_attributes=True)


@router.get("", response_model=list[DatasetRead])
async def list_datasets(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DatasetRead]:
    # Fetch datasets with their item counts in a single grouped query.
    item_count = func.count(DataItem.id)
    stmt = (
        select(Dataset, item_count)
        .outerjoin(DataItem, DataItem.dataset_id == Dataset.id)
        .where(Dataset.owner_id == user.id)
        .group_by(Dataset.id)
        .order_by(Dataset.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    result: list[DatasetRead] = []
    for dataset, count in rows:
        dto = DatasetRead.model_validate(dataset, from_attributes=True)
        dto.item_count = count
        result.append(dto)
    return result


@router.get("/{dataset_id}", response_model=DatasetWithItems)
async def get_dataset(
    dataset_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DatasetWithItems:
    stmt = (
        select(Dataset)
        .where(Dataset.id == dataset_id, Dataset.owner_id == user.id)
        .options(selectinload(Dataset.items))
    )
    dataset = (await db.execute(stmt)).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found"
        )
    dto = DatasetWithItems.model_validate(dataset, from_attributes=True)
    dto.item_count = len(dataset.items)
    return dto


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    dataset = await _get_owned_dataset(dataset_id, db, user)
    await db.delete(dataset)


@router.post("/{dataset_id}/archive", response_model=DatasetRead)
async def archive_dataset(
    dataset_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DatasetRead:
    dataset = await _get_owned_dataset(dataset_id, db, user)
    dataset.status = DatasetStatus.ARCHIVED
    await db.flush()
    await db.refresh(dataset)
    return DatasetRead.model_validate(dataset, from_attributes=True)
