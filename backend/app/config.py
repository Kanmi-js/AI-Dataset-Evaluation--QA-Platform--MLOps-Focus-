"""Application settings loaded from environment variables via pydantic-settings.

All secrets and routing configuration are injected through the environment
(see `.env.example`) so the same image runs unchanged across dev/CI/prod.
"""

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shipped defaults for the two secrets. Any of these reaching a non-development
# environment means the operator never set a real value — see the validator at
# the bottom of Settings.
PLACEHOLDER_SECRETS = frozenset(
    {
        "change-me-in-production",
        "change-me-internal-token",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General ---
    app_name: str = Field(default="TerrainAI")
    environment: str = Field(default="development")
    debug: bool = Field(default=True)
    api_v1_prefix: str = "/api/v1"

    # --- CORS ---
    # Comma-separated list of allowed origins for the browser dashboard.
    cors_origins: str = Field(default="http://localhost:5173,http://localhost:3000")

    # Adds `X-Tunnel-Skip-Anti-Phishing-Page` to every response, which stops VS
    # Code devtunnels / ngrok-style proxies interposing their interstitial page
    # on API calls. Off by default: it is a workaround for a specific local
    # setup, not something production should advertise. It deliberately does not
    # touch CORS headers — the origin allowlist above is the only thing that
    # decides who may call the API.
    enable_tunnel_headers: bool = Field(default=False)

    # --- Database ---
    # Async DSN used by FastAPI endpoints. Defaults match the `db` service in
    # docker-compose.yml so a fresh clone boots without a .env; hostname `db` is
    # the Compose service name. Point these at localhost to run outside Docker.
    database_url: str = Field(
        default="postgresql+asyncpg://mlops:mlops_password@db:5432/mlops_qa"
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://mlops:mlops_password@db:5432/mlops_qa"
    )
    # --- Celery / Redis ---
    celery_broker_url: str = Field(default="redis://redis:6379/0")
    celery_result_backend: str = Field(default="redis://redis:6379/1")

    # --- Object storage (S3-compatible; MinIO in local dev) ---
    #
    # All four of these are `| None` so that an empty value in the environment
    # means "unset", not "the empty string". That distinction is load-bearing:
    # boto3 checks `is not None`, so passing "" as an endpoint produces an
    # invalid-endpoint error, and passing "" as a credential overrides the
    # instance role instead of falling through to it. Production writes
    # `S3_ENDPOINT_URL=` and `S3_ACCESS_KEY=` precisely to mean "use real AWS
    # and the instance role" — see infrastructure/ec2/user-data.sh.tftpl. The
    # validator below turns those into None.

    # Internal endpoint the backend/worker use to talk to storage.
    s3_endpoint_url: str | None = Field(default="http://minio:9000")
    # Endpoint used only when signing URLs handed to the browser. In local dev
    # the browser cannot resolve the internal `minio` hostname, so presigned
    # URLs must be signed against a host it can reach (localhost:9000). Leave
    # equal to s3_endpoint_url in production (real S3).
    s3_public_endpoint_url: str | None = Field(default="http://localhost:9000")
    s3_access_key: str | None = Field(default="minioadmin")
    s3_secret_key: str | None = Field(default="minioadmin")
    s3_bucket: str = Field(default="datasets")
    s3_region: str = Field(default="us-east-1")

    # --- Upload limits ---
    #
    # Two caps because video is an order of magnitude larger than a still. Note
    # that the upload route buffers each file in memory before handing it to S3
    # (app/api/routes/uploads.py), so raising the video cap raises peak API
    # memory with it — see "Known limits" in the README.
    max_upload_bytes: int = Field(default=25 * 1024 * 1024)        # images, text
    max_video_upload_bytes: int = Field(default=200 * 1024 * 1024)  # match clips

    # --- Video analysis (app/workers/tracking.py) ---
    #
    # "yolo" runs the real detector + tracker over the footage. "simulated"
    # keeps the kinematic placeholder (app/workers/telemetry.py) — the test
    # suite and CI use it so they do not need torch or model weights.
    tracker: str = Field(default="yolo")
    # Ultralytics weight name or path. A bare name is downloaded on first use;
    # the Dockerfile pre-fetches it so workers do not hit the network per job.
    detector_weights: str = Field(default="yolo11n.pt")
    # Frames per second actually run through the detector. Broadcast footage is
    # 25-60 fps; 10 is enough to follow players and keeps CPU cost bounded.
    analysis_fps: float = Field(default=10.0)
    # Only the first N seconds of a clip are analysed and rendered. Breakdowns
    # are of plays, not full matches.
    max_analysis_seconds: float = Field(default=90.0)

    # --- Breakdown: script, voice-over, rendered clip ---
    #
    # Both keys are optional. Without ANTHROPIC_API_KEY the script falls back to
    # a template written from the detected events; without ELEVENLABS_API_KEY
    # the clip renders with captions and no voice. Neither ever blocks the
    # pipeline.
    anthropic_api_key: str | None = Field(default=None)
    commentary_model: str = Field(default="claude-opus-5-5")
    elevenlabs_api_key: str | None = Field(default=None)
    # A stock ElevenLabs voice by default. Swap in any voice from your
    # ElevenLabs library — but do not clone a real person's voice without
    # their consent (ElevenLabs' terms forbid it too).
    elevenlabs_voice_id: str = Field(default="pNInz6obpgDQGcFmaJgB")
    elevenlabs_model_id: str = Field(default="eleven_multilingual_v2")
    # Longest edge of the rendered breakdown clip, in pixels.
    render_max_dim: int = Field(default=1280)

    @field_validator(
        "anthropic_api_key",
        "elevenlabs_api_key",
        "s3_endpoint_url",
        "s3_public_endpoint_url",
        "s3_access_key",
        "s3_secret_key",
        mode="before",
    )
    @classmethod
    def _empty_string_is_unset(cls, value: object) -> object:
        """Treat `KEY=` in a dotenv file as absent rather than as ""."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # --- Auth ---
    secret_key: str = Field(default="change-me-in-production")
    access_token_expire_minutes: int = Field(default=60 * 24)
    jwt_algorithm: str = "HS256"

    # Shared secret guarding /internal/*, presented by the S3-event Lambda as
    # an X-Internal-Token header. Generated by Terraform and injected from SSM
    # in production (infrastructure/terraform/secrets.tf).
    #
    # The default is a non-empty placeholder on purpose: the header parameter
    # defaults to "" when absent, and comparing "" against "" succeeds, so an
    # empty token here would authenticate every unauthenticated request.
    internal_api_token: str = Field(default="change-me-internal-token")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_development(self) -> bool:
        return self.environment.lower() == "development"

    @model_validator(mode="after")
    def _reject_placeholder_secrets_outside_development(self) -> "Settings":
        """Refuse to boot a non-development environment on the shipped secrets.

        Both defaults are deliberately non-empty (see the comments above), which
        means a missing environment variable fails open rather than loudly: the
        app would run, sign JWTs with a public string, and accept any caller on
        /internal/*. This turns that into a startup error instead, which is the
        behaviour .env.example has always promised.
        """
        if self.is_development:
            return self

        unset = [
            name
            for name, value in (
                ("SECRET_KEY", self.secret_key),
                ("INTERNAL_API_TOKEN", self.internal_api_token),
            )
            if value in PLACEHOLDER_SECRETS
        ]
        if unset:
            raise ValueError(
                f"ENVIRONMENT={self.environment} but {', '.join(unset)} still "
                "hold the placeholder default. Generate real values "
                '(python -c "import secrets; print(secrets.token_urlsafe(48))") '
                "and inject them through the environment."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()


settings = get_settings()
