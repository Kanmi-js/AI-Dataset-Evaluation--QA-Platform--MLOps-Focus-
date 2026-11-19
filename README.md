# TerrainAI — Sports Tactical Analysis & Dataset QA

Upload football or basketball footage and get back a **voiced tactical
breakdown**: the clip replayed with broadcast-style overlays, pausing on the key
moments to ring the players involved and draw the passes and runs, while an AI
analyst talks you through it the way ex-pros do on a podcast. The dashboard also
shows the detected events, possession share, a 2D tactical radar, and a frame
canvas for human annotation.

The interesting part is the pipeline shape rather than the CV: FastAPI accepts an
upload, streams it to object storage, and hands the heavy work to a Celery worker
so the request returns immediately. The dashboard polls until the worker is done
and then lets a human accept, correct, or re-drive the result.

> **What is measured and what is estimated.** Players and the ball are detected
> and tracked in the real footage (YOLO + ByteTrack). Speeds and distances are
> *estimated* from player height in pixels, with camera pan compensated but no
> pitch calibration, and events (passes, turnovers, sprints) are heuristics over
> those tracks. The commentary is written by an AI from those events. The UI
> labels each of these. See [Tactical breakdowns](#tactical-breakdowns).

```
browser ──> nginx ──> FastAPI ──> S3 (raw/)
                         │
                         └─> Redis ──> Celery worker ──> OpenCV ──> S3 (processed/)
                                              │                      image: edge overlay
                                              │                      text:  normalized copy
                                              │                      video: poster frame,
                                              │                        YOLO tracks, events
                                          PostgreSQL                         │
                                        (metadata + telemetry JSONB)         ▼
                                                              Celery: generate_breakdown
                                                       script (Claude) → voice (ElevenLabs)
                                                       → render (OpenCV + ffmpeg) → S3
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

Compose needs a `.env` file to exist, so create one once after cloning:

```bash
cp .env.example .env
```

Every setting in [`backend/app/config.py`](backend/app/config.py) has a working
default that matches the Compose services, so the copied values work as-is.
Add `ANTHROPIC_API_KEY` and `ELEVENLABS_API_KEY` there for AI commentary and
voice-over.

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

## Tactical breakdowns

A video goes through two Celery tasks.

**1. `process_data_item`: tracking and events.**
[`tracking.py`](backend/app/workers/tracking.py) runs YOLO (`yolo11n`) with
ByteTrack over the clip at `ANALYSIS_FPS` (10 by default), for up to
`MAX_ANALYSIS_SECONDS` (90). Along the way it:

- estimates camera pan/zoom from background optical flow and maps every frame
  onto the first one, so a panning camera does not read as players running;
- clusters each track's shirt colour into two teams (outliers such as referees
  become `other`);
- scales pixels to metres from player height (~1.8 m football, ~2.0 m
  basketball) and discards physically impossible speeds.

[`events.py`](backend/app/workers/events.py) then derives possession (nearest
player in control of the ball), passes and long passes (possession moves within
a team), turnovers (it moves between teams), sprints, pressure on the passer and
each team's width and depth, and picks up to four **key moments**. The full
tracks go to S3 as `*.tracks.json`; a downsampled copy goes in
`item_metadata.telemetry` for the radar, and the events in
`item_metadata.analysis`.

**2. `generate_breakdown`: script, voice, render.**

| Stage | Module | Without its key |
|---|---|---|
| Script | [`commentary.py`](backend/app/workers/commentary.py): Claude writes 3-6 segments in an ex-pro analyst's voice, from the detected events only (structured JSON output, validated) | `ANTHROPIC_API_KEY` unset → a template script from the same events |
| Voice | [`voiceover.py`](backend/app/workers/voiceover.py): ElevenLabs text-to-speech, one request per segment | `ELEVENLABS_API_KEY` unset → captions only, timed at reading pace |
| Render | [`render.py`](backend/app/workers/render.py): overlays, a freeze per segment (spotlight, rings, pass arrows, word-by-word captions), voice mixed in, H.264 via ffmpeg | always runs |

The result is `*.breakdown.mp4` in S3; its state, script and timeline are in
`item_metadata.breakdown`, and `GET /items/{id}` returns a presigned
`breakdown_url` once it is `ready`. `POST /items/{id}/breakdown` re-runs this
task alone, for example after adding a key. A failed breakdown never fails the
item.

**Voices.** `ELEVENLABS_VOICE_ID` picks the voice; the default is a stock
ElevenLabs voice. Pick or design any voice in your ElevenLabs library, but do not
clone a real player's or pundit's voice without their consent. ElevenLabs'
terms forbid it, and it would be impersonation.

**Honesty rules**, enforced in code rather than left to the prompt alone:

- Claude only receives the detected events, teams (by kit colour) and track
  numbers, never anything to invent names or scores from; `_clean` drops any
  player id it was not given and clamps times to the clip.
- Every breakdown records `script_source` (model or `template`),
  `voice_source` and `tracking_source`, and the UI shows all three.
- Speeds and distances are labelled as estimates everywhere they appear.

### Simulated telemetry (tests and CI)

`TRACKER=simulated` keeps the original kinematic placeholder, which the test
suite and CI use so they need neither torch nor model weights. A clip's
`item_metadata.telemetry` is then written by
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

Simulated clips get no `analysis` and no breakdown; the panel says so.

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
| `POST` | `/items/{id}/reprocess` | Re-run pre-processing (tracking included) |
| `POST` | `/items/{id}/breakdown` | Re-run script, voice-over and render only |
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
- **No pitch calibration yet.** Metres come from player height, so they are
  estimates, and "width"/"depth" assume a roughly side-on broadcast camera. A
  pitch-keypoint model plus homography is the next accuracy step.
- **CPU inference.** About 45 s of tracking per minute of 720p footage on two
  vCPUs at the default settings. A GPU instance, or `yolo11s` on a GPU for
  accuracy, is the upgrade path.
- **Ultralytics is AGPL-3.0.** Fine for an open-source or portfolio project; a
  closed-source commercial product needs an Ultralytics licence or a permissive
  detector.
- **Video decoding depends on the container's codecs.** `process_video` relies
  on the FFmpeg bundled with `opencv-python-headless`. A clip in an unsupported
  container fails the item with a clear error rather than producing empty
  telemetry; adding codec libraries to the runtime stage of
  [`backend/Dockerfile`](backend/Dockerfile) is the remedy if you hit one.
