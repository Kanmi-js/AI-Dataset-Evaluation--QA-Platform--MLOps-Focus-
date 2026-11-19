"""Pure pre-processing functions (no DB / Celery coupling).

Kept dependency-free of the ORM so they are trivially unit-testable: give
them bytes, get back (derivative_bytes, metadata_dict, content_type).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from typing import Any

import cv2
import numpy as np

from app.config import settings
from app.workers.telemetry import DEFAULT_SPORT, simulate_tracking

TRACKER_SIMULATED = "simulated"

# Key under which processors hand extra output files to the task. The task pops
# it before persisting metadata: ``{suffix: (bytes, content_type)}``.
ARTIFACTS_KEY = "_artifacts"

# Used when the container does not report a frame rate. Recorded in the metadata
# as `fps_assumed` so a reader can tell a measured 25 fps from a guessed one.
ASSUMED_FPS = 25.0

# Where in the clip to grab the poster frame. Not frame 0: the first frame of a
# broadcast clip is often a fade or a black frame.
POSTER_FRAME_POSITION = 0.1


def process_image(raw: bytes) -> tuple[bytes, dict[str, Any], str]:
    """Run preliminary AI/CV pre-processing on an image.

    Produces an edge-detection overlay (a cheap stand-in for a segmentation
    mask) and basic statistics the QA dashboard can surface before a human
    reviews the item.

    Returns ``(png_bytes, metadata, content_type)``.
    """
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image bytes")

    height, width = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Canny edges as a preview "mask"; overlay in green on the original.
    edges = cv2.Canny(gray, 100, 200)
    overlay = img.copy()
    overlay[edges > 0] = (0, 255, 0)

    edge_ratio = float(np.count_nonzero(edges)) / float(edges.size)
    mean_brightness = float(gray.mean())

    metadata: dict[str, Any] = {
        "width": int(width),
        "height": int(height),
        "channels": int(img.shape[2]) if img.ndim == 3 else 1,
        "mean_brightness": round(mean_brightness, 2),
        "edge_pixel_ratio": round(edge_ratio, 4),
        # A crude "needs review" heuristic the dashboard can sort/filter on.
        "quality_flag": "low_contrast" if mean_brightness < 40 else "ok",
    }

    ok, buf = cv2.imencode(".png", overlay)
    if not ok:
        raise ValueError("Failed to encode processed image")
    return buf.tobytes(), metadata, "image/png"


def process_text(raw: bytes) -> tuple[bytes, dict[str, Any], str]:
    """Compute basic text statistics used for QA triage."""
    text = raw.decode("utf-8", errors="replace")
    words = text.split()
    lines = text.splitlines()

    metadata: dict[str, Any] = {
        "char_count": len(text),
        "word_count": len(words),
        "line_count": len(lines),
        "unique_words": len({w.lower() for w in words}),
        "quality_flag": "empty" if not words else "ok",
    }
    # The processed derivative is a normalized copy (stripped, single-spaced).
    normalized = "\n".join(line.strip() for line in lines).strip()
    return normalized.encode("utf-8"), metadata, "text/plain"


def _read_poster_frame(capture: cv2.VideoCapture, frame_count: int) -> tuple[Any, int]:
    """Grab a representative frame, falling back to the first readable one."""
    target = int(frame_count * POSTER_FRAME_POSITION) if frame_count > 1 else 0
    if target:
        capture.set(cv2.CAP_PROP_POS_FRAMES, target)

    ok, frame = capture.read()
    if ok:
        return frame, target

    # Seeking is unreliable on some containers; rewind and take frame 0.
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ok, frame = capture.read()
    if not ok:
        raise ValueError("Video contains no readable frames")
    return frame, 0


def process_video(
    raw: bytes,
    *,
    sport: str = DEFAULT_SPORT,
    suffix: str = ".mp4",
    tracker: str | None = None,
) -> tuple[bytes, dict[str, Any], str]:
    """Probe a match clip and attach tracking telemetry.

    Two outputs. The derivative is a JPEG poster frame, so the dashboard's
    existing image canvas can render a video item with no special-casing. The
    metadata carries the clip's real properties plus a telemetry block.

    With ``tracker="yolo"`` (the default, from settings) the telemetry comes
    from a detector and tracker run over the footage (``app.workers.tracking``)
    and the metadata also gets detected tactical events. The full-resolution
    tracks are returned under ``ARTIFACTS_KEY`` for the task to store, because
    they are too large for a JSONB column. With ``tracker="simulated"`` the
    kinematic placeholder is used and labelled as such.

    Returns ``(jpeg_bytes, metadata, content_type)``.
    """
    # cv2.VideoCapture takes a path, not a buffer, so the bytes have to hit disk.
    # delete=False plus an explicit unlink because Windows will not let
    # VideoCapture open a NamedTemporaryFile this process still holds open.
    handle = tempfile.NamedTemporaryFile(suffix=suffix or ".mp4", delete=False)
    try:
        handle.write(raw)
        handle.close()

        capture = cv2.VideoCapture(handle.name)
        if not capture.isOpened():
            raise ValueError(
                "Could not open video — the container or codec is not supported "
                "by this OpenCV/FFmpeg build"
            )
        try:
            reported_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            # NaN is possible here, and NaN > 0 is False, so this covers it.
            fps_ok = reported_fps > 0 and math.isfinite(reported_fps)
            fps = reported_fps if fps_ok else ASSUMED_FPS

            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            frame_count = max(frame_count, 0)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

            poster, poster_index = _read_poster_frame(capture, frame_count)
        finally:
            # Must release before the unlink below, or Windows refuses the delete.
            capture.release()

        if width <= 0 or height <= 0:
            # Fall back to the decoded frame; dimensions are needed to scale the
            # overlay, and a real frame is more trustworthy than the header.
            height, width = poster.shape[:2]

        analysis_block, artifacts = _analyse(
            handle.name,
            tracker=tracker or settings.tracker,
            sport=sport,
            fps=fps,
            frame_count=frame_count,
            width=int(width),
            height=int(height),
        )
    finally:
        os.unlink(handle.name)

    duration_sec = frame_count / fps if frame_count and fps else 0.0

    if not fps_ok or frame_count <= 0:
        quality_flag = "incomplete_metadata"
    elif width < 320:
        quality_flag = "low_resolution"
    else:
        quality_flag = "ok"

    metadata: dict[str, Any] = {
        "width": int(width),
        "height": int(height),
        "fps": round(fps, 3),
        "fps_assumed": not fps_ok,
        "frame_count": frame_count,
        "duration_sec": round(duration_sec, 2),
        "poster_frame_index": poster_index,
        "quality_flag": quality_flag,
        **analysis_block,
    }
    if artifacts:
        metadata[ARTIFACTS_KEY] = artifacts

    ok, buf = cv2.imencode(".jpg", poster, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise ValueError("Failed to encode poster frame")
    return buf.tobytes(), metadata, "image/jpeg"


def _analyse(
    path: str,
    *,
    tracker: str,
    sport: str,
    fps: float,
    frame_count: int,
    width: int,
    height: int,
) -> tuple[dict[str, Any], dict[str, tuple[bytes, str]]]:
    """Telemetry (and, for a real tracker, events + raw tracks) for a clip."""
    if tracker == TRACKER_SIMULATED:
        telemetry = simulate_tracking(
            frame_count=frame_count, fps=fps, width=width, height=height, sport=sport
        )
        return {"telemetry": telemetry}, {}

    # Imported lazily: tracking pulls in torch, which only the worker needs.
    from app.workers.events import detect_events
    from app.workers.tracking import track_clip

    telemetry, tracks = track_clip(
        path,
        sport=sport,
        fps=fps,
        frame_count=frame_count,
        width=width,
        height=height,
        weights=settings.detector_weights,
        analysis_fps=settings.analysis_fps,
        max_seconds=settings.max_analysis_seconds,
        inference_size=settings.inference_size,
    )
    analysis = detect_events(tracks)
    tracks_json = json.dumps(tracks, separators=(",", ":")).encode("utf-8")
    return (
        {"telemetry": telemetry, "analysis": analysis},
        {".tracks.json": (tracks_json, "application/json")},
    )
