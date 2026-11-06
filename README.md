# TerrainAI — Sports Tactical Analysis & Dataset QA

Upload match footage or stills, let a background worker pre-process them, then
review the result in a browser dashboard: bounding boxes on a frame canvas, and
a 2D tactical radar replaying tracked entities over a pitch or court.

The interesting part is the pipeline shape rather than the CV: FastAPI accepts an
upload, streams it to object storage, and hands the heavy work to a Celery worker
so the request returns immediately. The dashboard polls until the worker is done
and then lets a human accept, correct, or re-drive the result.

> **The tracking analysis is simulated.** Clip probing is real — dimensions,
> frame rate and duration are read off the container, and the poster frame is a
> decoded frame. The *trajectories* come from a kinematic model, not from
> tracking the footage. Every payload is tagged `source: "simulated"` and the UI
> badges it. See [Video telemetry](#video-telemetry).

```
browser ──> nginx ──> FastAPI ──> S3 (raw/)
                         │
                         └─> Redis ──> Celery worker ──> OpenCV ──> S3 (processed/)
                                              │                      image: edge overlay
                                              │                      text:  normalized copy
                                              │                      video: poster frame
                                          PostgreSQL
                                        (metadata + telemetry JSONB)
```

## Stack

| Layer | Choice |
|---|---|
| Frontend | React 18, Vite, TypeScript, TailwindCSS |
| API | FastAPI (async), SQLAlchemy 2.0, Pydantic v2 |
| Worker | Celery + Redis, OpenCV (headless), Pillow |
| Data | PostgreSQL, S3-compatible storage (MinIO locally) |
| Infra | Docker Compose, Terraform, GitHub Actions, AWS |

## Run it locally

Docker is the only prerequisite.

```bash
docker compose up --build
```

| | |
|---|---|
| Dashboard | http://localhost:5173 |
| API docs | http://localhost:8000/docs |
| MinIO console | http://localhost:9001 (`minioadmin` / `minioadmin`) |

Register any email and password on the login page — accounts are created
locally, there is no external identity provider.

`.env` is optional. Every setting in [`backend/app/config.py`](backend/app/config.py)
has a working default that matches the Compose services, so a fresh clone boots
without one. Copy [`.env.example`](.env.example) when you want to change
something.

Those defaults include placeholder values for `SECRET_KEY` and
`INTERNAL_API_TOKEN`, which are fine for local work and unacceptable anywhere
else. Any `ENVIRONMENT` other than `development` refuses to start while either
still holds its placeholder, rather than quietly signing JWTs with a string
that is published in this repository:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

In `ENVIRONMENT=development` the API creates its tables and the MinIO bucket at
startup, so there is no migration step to remember. Production is the opposite —
see [Schema changes](#schema-changes).

Want something to look at immediately? With the stack up and an account
registered, this creates two example datasets (it downloads two match stills and
runs them through the real image processor):

```bash
cd backend && python -m scripts.seed_sports
```

### Without Docker

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```bash
cd frontend
npm install
npm run dev
```

You still need PostgreSQL, Redis, and something S3-shaped reachable at the URLs
in `.env` — `docker compose up db redis minio` is the easy way to get them.

## Tests

```bash
cd backend
pytest
```

The suite runs against in-memory SQLite by default, so it needs no services.
That is a deliberate trade: it keeps the tests fast enough to run constantly,
but SQLite does not reproduce JSONB, native enums, or `ON DELETE CASCADE`. CI
runs the same suite against real PostgreSQL, which is where those differences
get caught:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://mlops:mlops_password@localhost:5432/mlops_qa pytest
```

Linting and the frontend checks:

```bash
cd backend && ruff check .
```

```bash
cd frontend && npm run lint && npm run typecheck
```

## How the pipeline works

1. `POST /api/v1/datasets/{id}/items` streams each file to S3 under `raw/`,
   writes a `DataItem` row with status `pending`, **commits**, and only then
   enqueues a Celery task.

   The commit-before-enqueue order is load-bearing. The worker is a separate
   process with its own database connection; if the task were enqueued inside
   the transaction, the worker could pick it up and query for a row that has not
   been committed yet. [`backend/tests/test_uploads.py`](backend/tests/test_uploads.py)
   has a regression test that asserts this ordering directly.

2. The worker sets `processing`, runs the media-specific pre-processing
   ([`backend/app/workers/processing.py`](backend/app/workers/processing.py)),
   writes derivatives to `processed/`, and stores the extracted metadata on the
   row as JSON. Failures land in `failed` with the error text, not a silent
   retry loop.

   | Media | Derivative | Metadata |
   |---|---|---|
   | image | Canny edge overlay (PNG) | dimensions, brightness, edge ratio |
   | text | normalized UTF-8 copy | char/word/line counts |
   | video | poster frame (JPEG) | dimensions, fps, duration + `telemetry` |

   The media type is inferred from the MIME type, falling back to the file
   suffix — browsers send `application/octet-stream` often enough that without
   the fallback a clip would be processed as text. Images and text share a 25 MB
   cap; clips get their own, larger one.

3. The dashboard polls the dataset until nothing is `pending` or `processing`.
   Images render on a canvas where boxes are drawn in normalized `[0,1]`
   coordinates — so an annotation stays correct at any display size. A video
   item also renders the tactical radar, driven by `item_metadata.telemetry`.

4. Saving annotations moves the item to `reviewed`. **Reprocess** resets it to
   `pending` and re-enqueues, which is the escape hatch when pre-processing got
   something wrong.

## Video telemetry

A clip's `item_metadata.telemetry` is written by
[`backend/app/workers/telemetry.py`](backend/app/workers/telemetry.py) and read
by `GET /api/v1/items/{id}` — there is no separate telemetry endpoint and no
in-process cache. It survives restarts and is visible to every worker, because
it is just another column on the item row.

What is real and what is not:

| | |
|---|---|
| Measured from the clip | frame count, frame rate, dimensions, duration, the poster frame |
| Generated | entity trajectories, per-entity speed and distance, the summary |

`simulate_tracking` walks each entity along fixed normalised waypoints, scales
them into the clip's own pixel space, and derives speed from each sample
interval's true duration. So the numbers are internally consistent and respond
to the clip you actually uploaded — but they are not derived from its pixels.
The pixel-to-metre scale assumes the frame shows the whole playing surface,
which is reported back in `telemetry.surface.assumption`.

Three things keep that from drifting into a false claim:

- every payload carries `source: "simulated"`;
- the UI badges it **Simulated** and labels the exported report the same way;
- the tactical summary is computed from the generated numbers, so it cannot say
  "6.2 m/s" unless the data does.

Replacing the simulation means swapping `simulate_tracking` for a real tracker
(detector → tracker → homography) that returns the same structure with `source`
set to the model's name. Nothing downstream changes: the schema, the API and the
radar already speak that shape, which was the point of building it this way
first.

## API

All routes are under `/api/v1`. Authentication is a bearer token from
`/auth/login`.

| Method | Path | |
|---|---|---|
| `POST` | `/auth/register` | Create an account |
| `POST` | `/auth/login` | Exchange credentials for a JWT |
| `GET` | `/auth/me` | Current user |
| `POST` | `/datasets` | Create a dataset |
| `GET` | `/datasets` | List your datasets, with item counts |
| `GET` | `/datasets/{id}` | Dataset with its items |
| `POST` | `/datasets/{id}/archive` | Archive |
| `DELETE` | `/datasets/{id}` | Delete (cascades to items) |
| `POST` | `/datasets/{id}/items` | Upload files and start processing |
| `GET` | `/items/{id}` | Item detail, with presigned URLs |
| `PATCH` | `/items/{id}/annotations` | Save annotations |
| `POST` | `/items/{id}/reprocess` | Re-run pre-processing |
| `GET` | `/health`, `/health/ready` | Liveness and readiness |
| `POST` | `/internal/s3-events` | Lambda-only; shared-secret guarded |

Datasets are scoped to their owner. Another user's dataset returns **404**, not
403 — a stranger should not be able to learn that an id exists.

Interactive docs: http://localhost:8000/docs

## Schema changes

Development creates tables from the models at startup. Everything else uses
Alembic, and the deploy runs `alembic upgrade head` in a one-shot container
before the new API starts.

```bash
cd backend
alembic revision --autogenerate -m "add whatever"
alembic upgrade head
```

One trap worth knowing: SQLAlchemy's `Enum(PyEnum)` persists each member's
*name*, so the PostgreSQL enum labels are uppercase (`PENDING`) while the API
returns lowercase values (`pending`). Autogenerate gets this right; hand-written
migrations often do not.

## Deployment

ALB → one EC2 instance running the whole stack under Docker Compose, with RDS
PostgreSQL and S3 alongside. Terraform provisions it; GitHub Actions builds
images, pushes to ECR, and rolls the instance over SSM — no SSH key, no
long-lived AWS credentials.

See [`infrastructure/README.md`](infrastructure/README.md) for the full walkthrough.

```bash
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
terraform init && terraform apply
```

## Layout

```
backend/
  app/
    api/routes/      auth, datasets, uploads, items, internal, health
    models/          SQLAlchemy models and enums
    schemas/         Pydantic request/response models
    services/        storage (S3), security (JWT, hashing)
    workers/         Celery tasks, pre-processing, telemetry simulation
  migrations/        Alembic
  scripts/           seed_sports.py — two example datasets for local dev
  tests/             pytest, defaults to SQLite
frontend/
  src/
    api/             typed client and response types
    components/      ImageCanvas, TacticalRadar, ...
    context/         auth state
    pages/           Login, Dashboard, DatasetDetail
infrastructure/
  terraform/         VPC, ALB, ASG, RDS, S3, ECR, IAM, Lambda
  ec2/               boot script and production compose file
  lambda/            S3 event handler
.github/workflows/   ci.yml, deploy.yml
```

## Known limits

Worth being explicit about, since the topology implies them:

- **One instance.** The ASG is capped at 1 because Redis and the Celery worker
  run on the instance; raising `max_size` means two nodes with two separate
  brokers, which is a design change rather than a config one.
- **No HTTPS out of the box.** That needs a domain and an ACM certificate, which
  Terraform cannot guess. [`infrastructure/terraform/compute.tf`](infrastructure/terraform/compute.tf)
  documents the three resources to add. Don't put real user data on it before then.
- **Deploys drop in-flight work.** `docker compose up -d` replaces containers
  without draining the Celery queue. Items caught mid-flight stay `processing`
  until someone hits Reprocess.
- **Uploads are buffered in memory.** `POST /datasets/{id}/items` does
  `await upload.read()` before handing the bytes to S3, so peak API memory
  tracks the size cap — which matters most for the video cap
  (`MAX_VIDEO_UPLOAD_BYTES`, 200 MB by default). Streaming straight to S3, or
  presigned direct-to-bucket uploads, is the fix; raising the cap without that
  change raises the memory ceiling with it.
- **Video decoding depends on the container's codecs.** `process_video` relies
  on the FFmpeg bundled with `opencv-python-headless`. A clip in an unsupported
  container fails the item with a clear error rather than producing empty
  telemetry; adding codec libraries to the runtime stage of
  [`backend/Dockerfile`](backend/Dockerfile) is the remedy if you hit one.
