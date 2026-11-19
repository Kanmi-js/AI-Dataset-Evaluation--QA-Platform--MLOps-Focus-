"""Tests for the breakdown stages: events, script, voice fallback, render.

The real tracker needs torch and model weights, so these build a ``tracks``
payload by hand — the same shape ``app.workers.tracking`` writes — describing a
scripted play:

* red #1 passes to red #2 (0-2 s),
* blue #3 intercepts red #2's next ball (3-3.4 s),
* blue #3 then sprints away with it.

Every stage after tracking is then exercised for real, including an ffmpeg
render of a synthetic clip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from app.workers import commentary
from app.workers.events import detect_events, team_shape
from app.workers.voiceover import Speech, estimate_duration, speak

FPS = 10.0
FRAMES = 60
W, H = 1600, 900
BOX_W, BOX_H = 40, 90  # 90 px tall -> 1.8 m / 90 px = 0.02 m per pixel
MPP = 1.8 / BOX_H

RED = [40, 40, 220]
BLUE = [220, 80, 30]


def _positions(
    frame: int,
) -> tuple[dict[int, tuple[float, float]], int | None, tuple[float, float] | None]:
    """Foot positions per player, the ball carrier, and a loose ball position."""
    pos = {
        1: (200.0, 500.0),
        2: (800.0, 500.0),
        3: (900.0, 520.0),
        4: (300.0, 700.0),
        5: (1200.0, 300.0),
        6: (1300.0, 700.0),
    }
    if frame >= 34:  # blue #3 sprints right: 40 px / 0.1 s = 8 m/s
        pos[3] = (900.0 + 40.0 * min(frame - 34, 15), 520.0)

    def lerp(a, b, t):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    if frame < 15:
        return pos, 1, None
    if frame < 20:
        return pos, None, lerp(pos[1], pos[2], (frame - 15) / 5)
    if frame < 30:
        return pos, 2, None
    if frame < 34:
        return pos, None, lerp(pos[2], pos[3], (frame - 30) / 4)
    return pos, 3, None


TEAM = {1: "A", 2: "A", 4: "A", 3: "B", 5: "B", 6: "B"}


def build_tracks() -> dict[str, Any]:
    frames = []
    previous: dict[int, tuple[float, float]] = {}
    for i in range(FRAMES):
        pos, carrier, loose = _positions(i)
        players = []
        for pid, (fx, fy) in pos.items():
            speed = 0.0
            if pid in previous:
                px, py = previous[pid]
                speed = ((fx - px) ** 2 + (fy - py) ** 2) ** 0.5 * MPP * FPS
            players.append(
                {
                    "id": pid,
                    "team": TEAM[pid],
                    "box": [fx - BOX_W / 2, fy - BOX_H, fx + BOX_W / 2, fy],
                    "conf": 0.9,
                    "speed_mps": round(speed, 1),
                }
            )
        previous = pos
        if carrier is not None:
            bx, by = pos[carrier][0] + 10, pos[carrier][1] - 5
        else:
            bx, by = loose  # type: ignore[misc]
        frames.append(
            {
                "i": i,
                "t": i / FPS,
                "to_ref": [1, 0, 0, 0, 1, 0],
                "players": players,
                "ball": [bx, by, 6.0],
                "ball_interpolated": False,
            }
        )

    def team(key, kit, name):
        return {
            "label": f"Team {key}",
            "kit_bgr": kit,
            "colour_name": name,
            "overlay_bgr": kit,
            "players": sorted(p for p, t in TEAM.items() if t == key),
        }

    return {
        "version": 1,
        "source": "test-fixture",
        "sport": "football",
        "fps": FPS,
        "stride": 1,
        "frame_width": W,
        "frame_height": H,
        "analysed_frames": FRAMES,
        "analysed_until_sec": (FRAMES - 1) / FPS,
        "ball_detection_rate": 1.0,
        "teams": {"A": team("A", RED, "red"), "B": team("B", BLUE, "blue")},
        "players": {
            str(pid): {
                "team": t,
                "observations": FRAMES,
                "first_t": 0.0,
                "last_t": (FRAMES - 1) / FPS,
                "distance_m": 0.0,
                "top_speed_mps": 8.0 if pid == 3 else 0.0,
                "metres_per_ref_px": MPP,
            }
            for pid, t in TEAM.items()
        },
        "frames": frames,
    }


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_detects_pass_turnover_and_sprint():
    analysis = detect_events(build_tracks())
    passes = [e for e in analysis["events"] if e["type"] in ("pass", "long_pass")]
    assert len(passes) == 1
    assert passes[0]["players"] == [1, 2]
    assert passes[0]["team"] == "A"
    # 600 px at 0.02 m/px.
    assert passes[0]["detail"]["distance_m"] == pytest.approx(12.0, abs=0.5)

    turnovers = [e for e in analysis["events"] if e["type"] == "turnover"]
    assert len(turnovers) == 1
    assert turnovers[0]["players"] == [2, 3]
    assert turnovers[0]["team"] == "B"

    sprints = [e for e in analysis["events"] if e["type"] == "sprint"]
    assert [s["players"] for s in sprints] == [[3]]
    assert sprints[0]["detail"]["peak_speed_mps"] == pytest.approx(8.0, abs=0.2)

    times = [e["t"] for e in analysis["events"]]
    assert times == sorted(times)


def test_possession_and_key_moments():
    analysis = detect_events(build_tracks())
    assert set(analysis["possession"]) == {"A", "B"}
    assert sum(analysis["possession"].values()) == pytest.approx(100, abs=0.2)
    assert analysis["possession"]["B"] > analysis["possession"]["A"]

    ids = {e["id"]: e for e in analysis["events"]}
    assert analysis["key_moments"], "the turnover at least should be a key moment"
    assert any(ids[k]["type"] == "turnover" for k in analysis["key_moments"])
    key_times = [ids[k]["t"] for k in analysis["key_moments"]]
    assert key_times == sorted(key_times)


def test_pressure_is_reported_on_the_intercepted_pass():
    analysis = detect_events(build_tracks())
    turnover = next(e for e in analysis["events"] if e["type"] == "turnover")
    # Blue #3 is 100 px (~2 m) from red #2: inside the 4 m football radius.
    assert turnover["detail"]["pressure_on_passer"] == [3]


def test_team_shape_measures_in_metres():
    tracks = build_tracks()
    shape = team_shape(tracks, tracks["frames"][0])
    # Red: x 200..800 -> 600 px -> 12 m; y 500..700 -> 4 m.
    assert shape["A"]["width_m"] == pytest.approx(12.0, abs=0.1)
    assert shape["A"]["depth_m"] == pytest.approx(4.0, abs=0.1)
    assert shape["A"]["visible_players"] == 3


def test_no_ball_means_no_events():
    tracks = build_tracks()
    for frame in tracks["frames"]:
        frame["ball"] = None
    analysis = detect_events(tracks)
    assert [e for e in analysis["events"] if e["type"] != "sprint"] == []
    assert analysis["possession"] == {}


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------


def test_template_script_without_api_key():
    tracks = build_tracks()
    analysis = detect_events(tracks)
    script = commentary.write_script(tracks, analysis)

    assert script["source"] == commentary.SOURCE_TEMPLATE
    assert "ANTHROPIC_API_KEY" in script["fallback_reason"]
    assert script["segments"][0]["kind"] == "intro"
    assert "red" in script["segments"][0]["text"] and "blue" in script["segments"][0]["text"]
    moments = [s for s in script["segments"] if s["kind"] == "moment"]
    assert len(moments) == len(analysis["key_moments"])
    assert all(s["rings"] for s in moments)


def test_clean_rejects_unknown_players_and_clamps_time():
    tracks = build_tracks()
    analysis = detect_events(tracks)
    raw = {
        "title": "x" * 500,
        "segments": [
            {
                "kind": "moment",
                "event_id": "does-not-exist",
                "at_sec": 999,
                "text": "word " * 200,
                "rings": [1, 999],
                "arrows": [
                    {"from_player": 1, "to_player": 999},
                    {"from_player": 1, "to_player": 2},
                ],
            }
        ],
    }
    cleaned = commentary._clean(raw, tracks, analysis)
    seg = cleaned["segments"][0]
    assert len(cleaned["title"]) == 120
    assert seg["at_sec"] == tracks["analysed_until_sec"]
    assert seg["rings"] == [1]
    assert seg["arrows"] == [{"from_player": 1, "to_player": 2}]
    assert len(seg["text"].split()) <= commentary.MAX_WORDS_PER_SEGMENT
    assert seg["event_id"] is None


def test_clean_snaps_segment_to_its_event_time():
    tracks = build_tracks()
    analysis = detect_events(tracks)
    event = analysis["events"][0]
    raw = {
        "title": "t",
        "segments": [
            {
                "kind": "moment",
                "event_id": event["id"],
                "at_sec": 0.0,
                "text": "hi",
                "rings": [],
                "arrows": [],
            }
        ],
    }
    assert commentary._clean(raw, tracks, analysis)["segments"][0]["at_sec"] == event["t"]


def test_claude_failure_falls_back_to_template(monkeypatch):
    tracks = build_tracks()
    analysis = detect_events(tracks)
    monkeypatch.setattr(commentary.settings, "anthropic_api_key", "test-key")

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(commentary, "_claude_script", boom)
    script = commentary.write_script(tracks, analysis)
    assert script["source"] == commentary.SOURCE_TEMPLATE
    assert "network down" in script["fallback_reason"]


def test_claude_output_is_cleaned(monkeypatch):
    tracks = build_tracks()
    analysis = detect_events(tracks)
    monkeypatch.setattr(commentary.settings, "anthropic_api_key", "test-key")
    reply = {
        "title": "Red press broken",
        "segments": [
            {
                "kind": "intro",
                "event_id": "",
                "at_sec": 0,
                "text": "Watch this.",
                "rings": [42],
                "arrows": [],
            }
        ],
    }
    monkeypatch.setattr(commentary, "_claude_script", lambda *_: (reply, "claude-opus-5-5"))
    script = commentary.write_script(tracks, analysis)
    assert script["source"] == "claude-opus-5-5"
    assert script["segments"][0]["rings"] == []  # 42 is not a tracked player


def test_brief_is_valid_json_and_only_carries_tracked_facts():
    tracks = build_tracks()
    brief = commentary._brief(tracks, detect_events(tracks))
    json.dumps(brief)
    assert set(brief["teams"]) == {"A", "B"}
    assert {p["player"] for p in brief["fastest_players"]} <= {int(k) for k in tracks["players"]}


def test_script_schema_is_strict():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(commentary.SCRIPT_SCHEMA)


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------


def test_voice_without_key_falls_back_to_reading_time():
    speech, source, error = speak(["one two three four five six seven eight nine ten"])
    assert source == "none"
    assert "ELEVENLABS_API_KEY" in error
    assert speech[0].pcm is None
    assert speech[0].duration_sec == pytest.approx(estimate_duration("x " * 10))


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _write_clip(path: Path, tracks: dict[str, Any]) -> bool:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    if not writer.isOpened():
        return False
    try:
        for frame in tracks["frames"]:
            img = np.full((H, W, 3), (40, 120, 40), dtype=np.uint8)
            for p in frame["players"]:
                x1, y1, x2, y2 = (int(v) for v in p["box"])
                colour = RED if p["team"] == "A" else BLUE
                cv2.rectangle(img, (x1, y1), (x2, y2), colour, -1)
            bx, by, r = frame["ball"]
            cv2.circle(img, (int(bx), int(by)), int(r), (255, 255, 255), -1)
            writer.write(img)
    finally:
        writer.release()
    return path.exists() and path.stat().st_size > 0


@needs_ffmpeg
def test_render_inserts_a_freeze_per_segment(tmp_path):
    from app.workers.render import LEAD_IN_SEC, TAIL_SEC, render_breakdown

    tracks = build_tracks()
    analysis = detect_events(tracks)
    script = commentary.write_script(tracks, analysis)
    # Fixed durations and a real tone so the audio track has content.
    speech = []
    for _ in script["segments"]:
        tone = (np.sin(np.arange(44100) * 2 * np.pi * 440 / 44100) * 8000).astype(np.int16)
        speech.append(Speech(tone, 1.0))

    clip = tmp_path / "clip.avi"
    if not _write_clip(clip, tracks):
        pytest.skip("No MJPG encoder in this OpenCV build")
    out = tmp_path / "breakdown.mp4"
    info = render_breakdown(
        clip, out, tracks=tracks, analysis=analysis, script=script, speech=speech, max_dim=800
    )

    assert out.exists()
    assert (info["width"], info["height"]) == (800, 450)
    assert len(info["timeline"]) == len(script["segments"])

    hold = LEAD_IN_SEC + 1.0 + TAIL_SEC
    expected = FRAMES / FPS + len(script["segments"]) * hold
    assert info["duration_sec"] == pytest.approx(expected, abs=0.5)

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sorted(probe.stdout.split()) == ["audio", "video"]

    # Freezes start in output-time order and never overlap.
    spans = [(t["output_start_sec"], t["output_end_sec"]) for t in info["timeline"]]
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert start >= end
