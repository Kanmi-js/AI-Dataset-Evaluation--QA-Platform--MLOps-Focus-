"""Unit tests for the pure pre-processing functions.

These need neither a database nor object storage — they operate on bytes.
"""

import os
import tempfile

import cv2
import numpy as np
import pytest

from app.workers.processing import process_image, process_text, process_video
from app.workers.telemetry import SOURCE_SIMULATED, simulate_tracking

# Frame rate and length written into the synthetic clip below; the assertions
# check process_video reads these back rather than guessing them.
CLIP_FPS = 10.0
CLIP_FRAMES = 20
CLIP_W = 160
CLIP_H = 120

# MJPG/AVI first: it is present in effectively every OpenCV build, including
# the headless wheel. mp4v is tried second for builds that prefer it.
CODEC_CANDIDATES = (("MJPG", ".avi"), ("mp4v", ".mp4"))


def _sample_png() -> bytes:
    """Build a small gradient image with a bright square, encoded as PNG."""
    img = np.zeros((64, 64, 3), dtype=np.uint8)
    img[16:48, 16:48] = 255  # white square -> produces edges
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _sample_clip() -> tuple[bytes, str]:
    """Write a short clip with a moving square; return ``(bytes, suffix)``.

    Skips rather than fails when the OpenCV build cannot encode video at all —
    a missing encoder is an environment fact, not a defect in the code here.
    """
    for fourcc_name, suffix in CODEC_CANDIDATES:
        path = os.path.join(tempfile.mkdtemp(), f"clip{suffix}")
        fourcc = cv2.VideoWriter_fourcc(*fourcc_name)
        writer = cv2.VideoWriter(path, fourcc, CLIP_FPS, (CLIP_W, CLIP_H))
        if not writer.isOpened():
            writer.release()
            continue
        try:
            for i in range(CLIP_FRAMES):
                frame = np.zeros((CLIP_H, CLIP_W, 3), dtype=np.uint8)
                x = 5 + i * 5
                frame[40:80, x : x + 30] = 255
                writer.write(frame)
        finally:
            writer.release()

        with open(path, "rb") as handle:
            data = handle.read()
        os.unlink(path)
        if data:
            return data, suffix

    pytest.skip("No usable video encoder in this OpenCV build")


def test_process_image_returns_metadata_and_png():
    derivative, metadata, content_type = process_image(_sample_png())

    assert content_type == "image/png"
    assert metadata["width"] == 64
    assert metadata["height"] == 64
    assert metadata["channels"] == 3
    assert 0.0 <= metadata["edge_pixel_ratio"] <= 1.0
    assert "quality_flag" in metadata

    # The derivative must itself be a decodable image.
    decoded = cv2.imdecode(np.frombuffer(derivative, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == (64, 64, 3)


def test_process_image_rejects_garbage():
    with pytest.raises(ValueError):
        process_image(b"not-an-image")


def test_process_text_counts():
    derivative, metadata, content_type = process_text(b"  hello world\nhello again  ")

    assert content_type == "text/plain"
    assert metadata["word_count"] == 4
    assert metadata["unique_words"] == 3  # hello, world, again
    assert metadata["line_count"] == 2
    assert metadata["quality_flag"] == "ok"
    # Normalized copy is stripped per line.
    assert derivative.decode() == "hello world\nhello again"


def test_process_text_flags_empty():
    _, metadata, _ = process_text(b"   \n  ")
    assert metadata["quality_flag"] == "empty"


# --------------------------------------------------------------------------
# Video -> poster frame + telemetry
# --------------------------------------------------------------------------


def test_process_video_reads_real_clip_properties():
    raw, suffix = _sample_clip()
    derivative, metadata, content_type = process_video(raw, suffix=suffix)

    assert content_type == "image/jpeg"
    # Dimensions and frame rate come from the container, not from defaults.
    assert metadata["width"] == CLIP_W
    assert metadata["height"] == CLIP_H
    assert metadata["fps"] == pytest.approx(CLIP_FPS, abs=0.5)
    assert metadata["fps_assumed"] is False
    assert metadata["frame_count"] == CLIP_FRAMES
    assert metadata["duration_sec"] == pytest.approx(CLIP_FRAMES / CLIP_FPS, abs=0.2)

    # The derivative is a real decodable still at the clip's resolution.
    decoded = cv2.imdecode(np.frombuffer(derivative, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == (CLIP_H, CLIP_W, 3)


def test_process_video_labels_telemetry_as_simulated():
    raw, suffix = _sample_clip()
    _, metadata, _ = process_video(raw, suffix=suffix)

    telemetry = metadata["telemetry"]
    # The honesty flag the dashboard badges off. If this ever stops saying
    # "simulated", the UI starts presenting a model's output as measurement.
    assert telemetry["source"] == SOURCE_SIMULATED
    assert "not from tracking this footage" in telemetry["tactical_summary"]

    assert telemetry["frame_width"] == CLIP_W
    assert telemetry["frame_height"] == CLIP_H
    assert telemetry["total_frames_sampled"] == len(telemetry["frames"])
    assert telemetry["frames"], "no sampled frames"

    first, last = telemetry["frames"][0], telemetry["frames"][-1]
    assert first["timestamp_sec"] == 0.0
    assert last["timestamp_sec"] > first["timestamp_sec"]
    # Coordinates live in the clip's own pixel space.
    for entity in last["tracked_entities"]:
        assert 0 <= entity["coordinates"]["x"] <= CLIP_W
        assert 0 <= entity["coordinates"]["y"] <= CLIP_H


def test_process_video_rejects_garbage():
    with pytest.raises(ValueError):
        process_video(b"not-a-video", suffix=".mp4")


def test_simulate_tracking_scales_with_clip_properties():
    """Timestamps follow fps and distance accumulates monotonically."""
    telemetry = simulate_tracking(
        frame_count=50, fps=25.0, width=1920, height=1080, sport="football"
    )

    assert telemetry["duration_sec"] == pytest.approx(50 / 25.0, abs=0.01)
    timestamps = [f["timestamp_sec"] for f in telemetry["frames"]]
    assert timestamps == sorted(timestamps)

    distances = [
        next(e for e in f["tracked_entities"] if e["id"] == "player_10")[
            "distance_covered_m"
        ]
        for f in telemetry["frames"]
    ]
    assert distances[0] == 0.0
    assert distances == sorted(distances)
    assert distances[-1] > 0.0


def test_simulate_tracking_caps_sample_count():
    telemetry = simulate_tracking(
        frame_count=100_000, fps=25.0, width=640, height=480, max_samples=12
    )
    assert telemetry["total_frames_sampled"] == 12


def test_simulate_tracking_falls_back_for_unknown_sport():
    telemetry = simulate_tracking(
        frame_count=10, fps=25.0, width=640, height=480, sport="kabaddi"
    )
    assert telemetry["sport"] == "football"


def test_simulate_tracking_survives_missing_frame_rate():
    """A container with no fps must still produce usable, ordered frames."""
    telemetry = simulate_tracking(frame_count=5, fps=0.0, width=640, height=480)

    assert telemetry["duration_sec"] == 0.0
    assert telemetry["total_frames_sampled"] == 5
    timestamps = [f["timestamp_sec"] for f in telemetry["frames"]]
    assert timestamps == sorted(timestamps)
