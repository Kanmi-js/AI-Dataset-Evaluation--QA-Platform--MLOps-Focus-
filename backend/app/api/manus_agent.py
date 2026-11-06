"""Removed: superseded by the real upload/processing pipeline.

This module held an in-memory sports telemetry demo (`POST /manus/upload/video`
and `GET /manus/video/telemetry/{task_id}`). Both are replaced:

* upload   -> ``POST /api/v1/datasets/{dataset_id}/items``
              (app/api/routes/uploads.py) — authenticated, owner-scoped, size
              capped, stored in S3 rather than written to a client-controlled
              local path.
* telemetry -> ``GET /api/v1/items/{item_id}`` (app/api/routes/items.py), which
              returns ``item_metadata.telemetry`` persisted by the Celery
              worker (app/workers/telemetry.py).

Nothing imports this file; it is pending deletion from the tree.
"""
