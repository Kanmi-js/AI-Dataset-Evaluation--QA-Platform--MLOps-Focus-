# Project Overview
**Name:** TerrainAI — Sports Tactical Analysis & Dataset QA Platform
**Purpose:** A full-stack web application for uploading match footage and stills,
running preliminary AI pre-processing, and presenting the results on a dashboard
for human-in-the-loop quality assurance, annotation, and tactical review.

The generic dataset-QA pipeline (upload → S3 → Celery → Postgres → review) is the
substrate; sports telemetry is the product built on it. Video is one more
`MediaType` through that same pipeline, not a parallel path.

**Honesty constraint:** the tracking *analysis* is simulated
(`backend/app/workers/telemetry.py`). Clip probing is real — dimensions, frame
rate, duration, poster frame — but the trajectories come from a kinematic model.
Every telemetry payload carries `source: "simulated"`, the UI badges it, and the
summary text is derived from the generated numbers. Do not remove that flag,
present simulated figures as measurements, or hard-code statistics that look like
model output.

# Tech Stack & Architecture
* **Frontend:** React (Vite, TypeScript, TailwindCSS) for the QA dashboard, the
  annotation canvas, and the 2D tactical radar.
* **Backend:** Python (FastAPI) for high-performance, asynchronous API routing and
  file handling.
* **Task Queue:** Celery with Redis (to handle heavy OpenCV/ML masking and video
  probing in the background without blocking the API).
* **Database:** PostgreSQL for user data, task statuses, dataset metadata, and
  telemetry (JSONB). S3-compatible storage for raw/processed media.
* **Infrastructure (DevOps):**
  * Containerization: Docker & Docker Compose (Local Dev)
  * CI/CD: GitHub Actions (Linting, Testing, Image Builds)
  * Deployment: AWS (EC2/ECS for the app, S3 for storage, Lambda for serverless
    pre-processing triggers)
  * IaC: Terraform for provisioning cloud resources.

# Directory Structure
* `/frontend/` - React application.
* `/backend/` - FastAPI application, database models, and Celery worker scripts.
* `/infrastructure/` - Terraform scripts and CI/CD workflow definitions.
* `docker-compose.yml` - Local multi-container orchestration.

# Coding Conventions
* **Python:** Use strict type hinting (`def process_image(file: UploadFile) -> dict:`).
  Use `pydantic` for schema validation. Ensure asynchronous endpoints (`async def`).
* **React:** Functional components with React Hooks. Modularize UI components
  (e.g., `<ImageCanvas />`, `<TacticalRadar />`). Components render data the page
  owns; they do not fetch for themselves.
* **DevOps:** Keep Dockerfiles multi-staged and lightweight. Use environment
  variables for all secrets and routing configurations.

# Rules that exist because they were broken once
* **Mount every router unconditionally** in `app/main.py`. No `try/except` around
  `include_router` — a swallowed import error produces an API that answers 200 on
  `/` while a whole route group 404s. `/api/v1/health` in particular is the ALB
  target-group check, the production container healthcheck, and the EC2 boot gate,
  so losing it boot-loops deploys. `backend/tests/test_api.py` asserts the full
  route set.
* **`engine` is an `AsyncEngine`.** `Base.metadata.create_all(bind=engine)` raises;
  use `async with engine.begin() as conn: await conn.run_sync(...)`.
* **Never swallow startup errors.** Schema creation failing must stop the process.
  (The bucket step is the one exception, and says why inline.)
* **CORS origins come from `settings.cors_origin_list`.** `allow_origins=["*"]`
  with `allow_credentials=True` is a combination browsers reject, and no
  middleware should set `Access-Control-Allow-Origin` behind the allowlist's back.
* **Commit before enqueuing Celery work.** The worker is a separate process; a row
  still inside an open transaction is invisible to it.
* **Uploads go to S3 under `raw/{dataset_id}/{uuid}_{name}`.** Never join a
  client-supplied filename onto a local path, and never write files from a route.
* **Adding a `MediaType`** needs four edits: the enum, an Alembic migration adding
  the *uppercase* label (SQLAlchemy persists member names), inference in
  `uploads._infer_media_type`, and a branch in `tasks._run_processor` — which
  raises on anything unhandled rather than defaulting to text.

# Development Phases
1. Scaffold the monorepo, Docker Compose, and base FastAPI/React apps. ✅
2. Build the S3 upload and Celery background worker pipeline. ✅
3. Develop the React dashboard for dataset visualization. ✅
4. Write Terraform scripts and configure GitHub Actions for deployment. ✅
5. Replace simulated tracking with a real detector/tracker, keeping the
   `telemetry` contract and setting `source` to the model's name.
