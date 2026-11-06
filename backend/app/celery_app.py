"""Celery application instance.

Import path used by the worker container:

    celery -A app.celery_app.celery_app worker --loglevel=info
"""

from celery import Celery

from app.config import settings

celery_app = Celery(
    "mlops_qa",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_time_limit=600,          # hard limit: 10 min per item
    task_soft_time_limit=540,
    worker_max_tasks_per_child=50,  # recycle workers to bound OpenCV memory growth
    result_expires=3600,
)
