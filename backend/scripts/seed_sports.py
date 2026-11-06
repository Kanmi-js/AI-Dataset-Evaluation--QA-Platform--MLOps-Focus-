"""Seed two example sports datasets for local development.

Run from the ``backend`` directory with the stack up (Postgres + MinIO):

    python -m scripts.seed_sports

It downloads two match stills, pushes them through the *real* image processor
(``app.workers.processing.process_image``), and stores the raw and processed
objects under the same key layout the upload route uses — so presigned URLs
resolve and the dashboard renders them.

Two deliberate properties:

* Item metadata is whatever the processor measured. Nothing here invents
  statistics; an earlier version hard-coded plausible-looking brightness and
  edge ratios, which is indistinguishable from real output once stored.
* The bounding boxes are example *annotations* — the human-QA half of the
  pipeline, not model predictions — which is why each item is REVIEWED and no
  box carries a confidence score.
"""

import urllib.request
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from sqlalchemy.orm import Session

from app.database import SyncSessionLocal
from app.models import DataItem, Dataset, User
from app.models.enums import ItemStatus, MediaType
from app.services import storage
from app.workers.processing import process_image

SAMPLES: list[dict[str, Any]] = [
    {
        "sport": "Football",
        "task_name": "UCL Tactical Transition - High Press",
        "task_desc": (
            "4-3-3 counter-pressing analysis and half-space recovery line tracking."
        ),
        "image_url": (
            "https://images.unsplash.com/photo-1574629810360-7efbbe195018"
            "?auto=format&fit=crop&w=1920&q=80"
        ),
        "filename": "ucl_high_press.jpg",
        "annotations": {
            "boxes": [
                {"id": "1", "x": 350, "y": 420, "width": 80, "height": 190,
                 "label": "Pressing Forward #9", "role": "Attacker"},
                {"id": "2", "x": 580, "y": 480, "width": 80, "height": 180,
                 "label": "Center Midfielder #8", "role": "Attacker"},
                {"id": "3", "x": 820, "y": 510, "width": 75, "height": 185,
                 "label": "Inverted Fullback #2", "role": "Defender"},
                {"id": "4", "x": 1100, "y": 560, "width": 85, "height": 195,
                 "label": "Center Back #4", "role": "Defender"},
                {"id": "5", "x": 710, "y": 620, "width": 30, "height": 30,
                 "label": "Match Ball", "role": "Ball"},
            ]
        },
    },
    {
        "sport": "Basketball",
        "task_name": "NBA Fast-Break 5-Out Spacing",
        "task_desc": (
            "Transition perimeter spacing, weak-side corner cut, and paint rim "
            "protection."
        ),
        "image_url": (
            "https://images.unsplash.com/photo-1546519638-68e109498ffc"
            "?auto=format&fit=crop&w=1920&q=80"
        ),
        "filename": "nba_fast_break.jpg",
        "annotations": {
            "boxes": [
                {"id": "1", "x": 420, "y": 380, "width": 90, "height": 220,
                 "label": "Point Guard #2", "role": "Attacker"},
                {"id": "2", "x": 280, "y": 440, "width": 90, "height": 210,
                 "label": "Corner Shooter #11", "role": "Attacker"},
                {"id": "3", "x": 650, "y": 400, "width": 95, "height": 230,
                 "label": "Drop Defender #33", "role": "Defender"},
                {"id": "4", "x": 850, "y": 460, "width": 100, "height": 240,
                 "label": "Rim Protector #15", "role": "Defender"},
                {"id": "5", "x": 480, "y": 430, "width": 32, "height": 32,
                 "label": "Game Ball", "role": "Ball"},
            ]
        },
    },
]

# Outside the repo on purpose, so a re-run is cheap without adding untracked
# binaries to the working tree.
CACHE_DIR = Path(gettempdir()) / "terrainai_seed_cache"
USER_AGENT = "TerrainAI-seeder/1.0 (local development)"


def _fetch(url: str, filename: str) -> bytes:
    """Download a sample image, caching it between runs."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / filename
    if cached.exists():
        return cached.read_bytes()

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    cached.write_bytes(data)
    return data


def seed(db: Session) -> None:
    user = db.query(User).first()
    if user is None:
        raise SystemExit(
            "No user in the database. Register one through POST /api/v1/auth/register "
            "first — datasets are owner-scoped."
        )

    storage.ensure_bucket()

    for sample in SAMPLES:
        # Idempotent: re-running must not pile up duplicate datasets.
        existing = (
            db.query(Dataset)
            .filter(Dataset.owner_id == user.id, Dataset.name == sample["task_name"])
            .one_or_none()
        )
        if existing is not None:
            print(f"skip   {sample['task_name']} (already seeded as #{existing.id})")
            continue

        raw = _fetch(sample["image_url"], sample["filename"])
        derivative, metadata, content_type = process_image(raw)

        dataset = Dataset(
            name=sample["task_name"],
            description=sample["task_desc"],
            owner_id=user.id,
        )
        db.add(dataset)
        db.flush()  # need dataset.id to build the object keys

        stem = Path(sample["filename"]).stem
        raw_key = f"raw/{dataset.id}/{sample['filename']}"
        processed_key = f"processed/{dataset.id}/{stem}.png"
        storage.upload_bytes(raw_key, raw, content_type="image/jpeg")
        storage.upload_bytes(processed_key, derivative, content_type=content_type)

        db.add(
            DataItem(
                dataset_id=dataset.id,
                filename=sample["filename"],
                media_type=MediaType.IMAGE,
                status=ItemStatus.REVIEWED,
                raw_s3_key=raw_key,
                processed_s3_key=processed_key,
                size_bytes=len(raw),
                item_metadata=metadata,
                annotations=sample["annotations"],
            )
        )
        print(f"seeded {sample['sport']}: dataset #{dataset.id} {sample['task_name']}")

    db.commit()


def main() -> None:
    db = SyncSessionLocal()
    try:
        seed(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
