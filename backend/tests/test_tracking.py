"""Unit tests for the tracker's pure helpers.

``track_clip`` itself needs torch and model weights, which CI does not install,
so these exercise the parts that decide what the model's raw output means:
kit colours, ball selection, track stitching and speed estimation.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.workers import tracking
from app.workers.tracking import (
    _Frame,
    _kinematics,
    _lab_to_bgr,
    _pick_ball,
    _PlayerObs,
    _renumber_tracks,
    _stitch_tracklets,
    _torso_colour,
    colour_name,
)

IDENTITY = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


def _bgr(r: int, g: int, b: int) -> list[int]:
    return [b, g, r]


# ---------------------------------------------------------------------------
# Kit colours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rgb", "name"),
    [
        ((135, 206, 235), "light blue"),
        ((20, 40, 100), "navy"),
        ((30, 60, 200), "blue"),
        ((255, 140, 0), "orange"),
        ((240, 240, 240), "white"),
        ((20, 20, 20), "black"),
        ((200, 20, 30), "red"),
        ((20, 160, 60), "green"),
    ],
)
def test_colour_name(rgb, name):
    assert colour_name(_bgr(*rgb)) == name


def test_torso_colour_ignores_a_wooden_court():
    """A navy shirt half-obscured by court colour must still read as navy.

    The old filter only removed pitch green, so on a wooden floor the court
    leaked into the median and the team was called orange.
    """
    court = _bgr(205, 150, 90)
    navy = _bgr(20, 40, 100)
    frame = np.full((200, 200, 3), court, dtype=np.uint8)
    box = (80.0, 40.0, 120.0, 160.0)
    frame[40:160, 80:120] = navy
    # Court showing through most of the torso band (an arm gap, a lean).
    frame[58:100, 90:103] = court

    lab = _torso_colour(frame, box)
    assert lab is not None
    assert colour_name(_lab_to_bgr(lab)) == "navy"


def test_torso_colour_keeps_a_kit_that_matches_the_floor():
    """White kit on a white floor: fall back rather than return nothing."""
    white = _bgr(240, 240, 240)
    frame = np.full((200, 200, 3), white, dtype=np.uint8)
    lab = _torso_colour(frame, (80.0, 40.0, 120.0, 160.0))
    assert lab is not None
    assert colour_name(_lab_to_bgr(lab)) == "white"


# ---------------------------------------------------------------------------
# Ball selection
# ---------------------------------------------------------------------------


def test_ball_rejects_head_sized_and_tiny_blobs():
    person_h = 100.0
    head = (0.9, 50.0, 50.0, 20.0)  # 40 px across: too big for a ball
    speck = (0.9, 60.0, 60.0, 1.0)  # 2 px across: noise
    ball = (0.5, 70.0, 70.0, 6.0)
    picked = _pick_ball(
        [head, speck, ball], previous=None, person_height_px=person_h, max_jump_px=None
    )
    assert picked == (70.0, 70.0, 6.0)


def test_ball_needs_confidence_to_start_a_trail():
    weak = (0.08, 70.0, 70.0, 6.0)
    assert _pick_ball([weak], previous=None, person_height_px=100.0, max_jump_px=None) is None
    # The same weak detection is fine once it continues an existing trail.
    picked = _pick_ball(
        [weak], previous=(65.0, 68.0, 6.0), person_height_px=100.0, max_jump_px=80.0
    )
    assert picked == (70.0, 70.0, 6.0)


def test_ball_prefers_continuity_over_a_slightly_stronger_far_blob():
    near = (0.2, 105.0, 100.0, 6.0)
    far = (0.3, 400.0, 100.0, 6.0)
    picked = _pick_ball(
        [far, near], previous=(100.0, 100.0, 6.0), person_height_px=100.0, max_jump_px=80.0
    )
    assert picked == (105.0, 100.0, 6.0)


def test_ball_teleport_needs_high_confidence():
    prev = (100.0, 100.0, 6.0)
    jumpy = (0.25, 500.0, 100.0, 6.0)
    assert _pick_ball([jumpy], previous=prev, person_height_px=100.0, max_jump_px=80.0) is None
    sure = (0.6, 500.0, 100.0, 6.0)
    assert _pick_ball([sure], previous=prev, person_height_px=100.0, max_jump_px=80.0) == (
        500.0,
        100.0,
        6.0,
    )


# ---------------------------------------------------------------------------
# Track identity
# ---------------------------------------------------------------------------

FPS = 10.0
BOX_H = 90.0  # 1.8 m tall -> 0.02 m per pixel


def _box(x: float, y: float) -> tuple[float, float, float, float]:
    return (x - 20, y - BOX_H, x + 20, y)


def _frames(spec: dict[int, dict[int, tuple[float, float]]], n: int) -> list[_Frame]:
    """``spec[track_id][frame] = foot position``."""
    frames = []
    for i in range(n):
        frame = _Frame(index=i, timestamp=i / FPS, to_ref=IDENTITY)
        for tid, positions in spec.items():
            if i in positions:
                frame.players.append(_PlayerObs(tid, _box(*positions[i]), 0.9))
        frames.append(frame)
    return frames


def _colours(ids: list[int], lab: tuple[float, float, float]) -> dict[int, list[np.ndarray]]:
    return {tid: [np.array(lab, dtype=np.float32)] * 5 for tid in ids}


def test_stitch_joins_a_broken_track():
    # Track 7 walks right, vanishes behind a screen for 0.3 s, reappears as 42
    # a little further on.
    spec = {
        7: {i: (100.0 + 10 * i, 300.0) for i in range(0, 10)},
        42: {i: (100.0 + 10 * i, 300.0) for i in range(13, 25)},
    }
    frames = _frames(spec, 25)
    colours = _colours([7, 42], (60.0, 140.0, 110.0))
    mapping = _stitch_tracklets(frames, colours, "basketball")
    assert mapping[42] == mapping[7]
    ids = {p.track_id for f in frames for p in f.players}
    assert len(ids) == 1


def test_stitch_refuses_an_impossible_run():
    spec = {
        7: {i: (100.0, 300.0) for i in range(0, 10)},
        # 1500 px = 30 m away 0.3 s later: no one runs that.
        42: {i: (1600.0, 300.0) for i in range(13, 25)},
    }
    frames = _frames(spec, 25)
    mapping = _stitch_tracklets(frames, _colours([7, 42], (60.0, 140.0, 110.0)), "football")
    assert mapping[42] != mapping[7]


def test_stitch_refuses_a_different_kit():
    spec = {
        7: {i: (100.0 + 10 * i, 300.0) for i in range(0, 10)},
        42: {i: (100.0 + 10 * i, 300.0) for i in range(13, 25)},
    }
    frames = _frames(spec, 25)
    colours = {
        7: [np.array((60.0, 140.0, 110.0), dtype=np.float32)] * 5,
        42: [np.array((200.0, 128.0, 128.0), dtype=np.float32)] * 5,
    }
    mapping = _stitch_tracklets(frames, colours, "football")
    assert mapping[42] != mapping[7]


def test_stitch_never_merges_players_seen_together():
    spec = {
        7: {i: (100.0, 300.0) for i in range(0, 20)},
        42: {i: (110.0, 300.0) for i in range(5, 20)},
    }
    frames = _frames(spec, 20)
    mapping = _stitch_tracklets(frames, _colours([7, 42], (60.0, 140.0, 110.0)), "football")
    assert mapping[42] != mapping[7]


def test_renumber_by_first_appearance():
    spec = {316: {0: (100.0, 300.0)}, 12: {1: (300.0, 300.0)}, 905: {0: (500.0, 300.0)}}
    frames = _frames(spec, 2)
    colours = _colours([316, 12, 905], (60.0, 140.0, 110.0))
    _renumber_tracks(frames, colours)
    assert [p.track_id for p in frames[0].players] == [1, 2]
    assert [p.track_id for p in frames[1].players] == [3]
    assert set(colours) == {1, 2, 3}


# ---------------------------------------------------------------------------
# Speed
# ---------------------------------------------------------------------------


def test_box_jitter_is_not_a_sprint():
    rng = np.random.default_rng(0)
    spec = {1: {i: (300.0 + rng.uniform(-4, 4), 300.0 + rng.uniform(-4, 4)) for i in range(40)}}
    stats, _ = _kinematics(_frames(spec, 40), {1: "A"}, "basketball")
    assert stats[1]["top_speed_mps"] < 1.0


def test_steady_run_speed_is_recovered():
    # 30 px per 0.1 s at 0.02 m/px = 6 m/s.
    spec = {1: {i: (100.0 + 30 * i, 300.0) for i in range(30)}}
    stats, _ = _kinematics(_frames(spec, 30), {1: "A"}, "football")
    assert stats[1]["top_speed_mps"] == pytest.approx(6.0, abs=0.3)
    assert stats[1]["distance_m"] == pytest.approx(29 * 0.6, rel=0.05)


@pytest.mark.parametrize(("sport", "kept"), [("football", True), ("basketball", False)])
def test_speed_cap_is_per_sport(sport, kept):
    # 50 px per 0.1 s at 0.02 m/px = 10 m/s: plausible on a pitch, not a court.
    spec = {1: {i: (100.0 + 50 * i, 300.0) for i in range(30)}}
    stats, _ = _kinematics(_frames(spec, 30), {1: "A"}, sport)
    if kept:
        assert stats[1]["top_speed_mps"] == pytest.approx(10.0, abs=0.5)
    else:
        assert stats[1]["top_speed_mps"] == 0.0


def test_basketball_cap_is_lower_than_football():
    assert (
        tracking.MAX_PLAUSIBLE_SPEED_MPS["basketball"]
        < tracking.MAX_PLAUSIBLE_SPEED_MPS["football"]
    )
