# syntax=docker/dockerfile:1

# --- Stage 1: build wheels ---
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# --- Stage 2: runtime ---
FROM python:3.12-slim AS runtime

# OpenCV (headless) still needs libGL/glib at runtime. ffmpeg decodes and
# encodes video for the breakdown renderer and voice-over; DejaVu is the
# caption font it draws with.
#
# Debian's mirror is switched from http:// to https:// first: some networks
# block or drop plain port-80 traffic, which makes `apt-get update` time out.
# The python:slim base already ships ca-certificates, so HTTPS works as-is.
RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \
        if [ -f "$f" ]; then sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; fi; \
    done \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 curl ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Run as non-root.
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Tracking stack. CPU-only torch from PyTorch's index (the PyPI default bundles
# CUDA). Ultralytics depends on the GUI build of OpenCV, which would shadow the
# pinned headless one, so it is swapped back out afterwards.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        torch torchvision \
    && pip install --no-cache-dir -r requirements-ml.txt \
    && pip uninstall -y opencv-python \
    && pip install --no-cache-dir --force-reinstall --no-deps \
        "$(grep '^opencv-python-headless' requirements.txt)"

# Bake the detector weights into the image so workers never download per job.
# Outside /app because local dev bind-mounts the source over /app.
ENV DETECTOR_WEIGHTS=/opt/models/yolo11n.pt \
    YOLO_CONFIG_DIR=/tmp/Ultralytics
RUN mkdir -p /opt/models \
    && curl -fsSL -o /opt/models/yolo11n.pt \
        https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt \
    && python -c "import cv2, ultralytics, torch; print(cv2.__version__, ultralytics.__version__, torch.__version__)"

COPY . .
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Overridden by docker-compose for the worker service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
