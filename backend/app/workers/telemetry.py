"""Simulated multi-object tracking telemetry.

This module **fabricates** player trajectories. It is not a tracker: there is no
detection, no re-identification and no homography. It exists so the rest of the
system — the Celery pipeline, the `item_metadata` schema, the dashboard radar —
has the exact shape a real tracker would emit and can be built and reviewed
before a model is wired in.

Two things keep that honest:

* every payload carries ``source: "simulated"``, which is the single flag the UI
  keys its badge off (``frontend/src/components/TacticalRadar.tsx``);
* the tactical summary is computed from the numbers actually generated, so it
  cannot drift away from the data the way a hand-written blurb does.

Swapping in a real tracker means replacing :func:`simulate_tracking` with
something that returns the same structure and sets ``source`` to the model's
name. Nothing downstream needs to change.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

SOURCE_SIMULATED = "simulated"

# Upper bound on sampled keyframes, so a 90-minute clip does not write a
# multi-megabyte JSONB blob. 40 samples animates smoothly in the UI.
MAX_SAMPLES = 40

# (progress in [0, 1], x as a fraction of frame width, y as a fraction of height)
Waypoint = tuple[float, float, float]


@dataclass(frozen=True)
class _EntitySpec:
    """One tracked thing and the normalised path the model walks it along."""

    id: str
    role: str
    label: str
    # Bounding-box size as a fraction of frame width/height.
    box_w: float
    box_h: float
    waypoints: tuple[Waypoint, ...]


@dataclass(frozen=True)
class _SportProfile:
    """Playing-surface dimensions plus the cast of entities."""

    name: str
    surface_length_m: float
    surface_width_m: float
    entities: tuple[_EntitySpec, ...]


# Trajectories are data, not code, so they can be re-tuned without touching the
# kinematics below. Each is a plausible attacking sequence: build-up, a burst,
# then a defensive response.
FOOTBALL = _SportProfile(
    name="football",
    surface_length_m=105.0,
    surface_width_m=68.0,
    entities=(
        _EntitySpec(
            id="player_10",
            role="Attacker",
            label="Player #10",
            box_w=0.08,
            box_h=0.22,
            waypoints=(
                (0.00, 0.22, 0.70),
                (0.50, 0.40, 0.65),
                (0.75, 0.62, 0.50),
                (1.00, 0.80, 0.37),
            ),
        ),
        _EntitySpec(
            id="shadow_04",
            role="Defender",
            label="Cover Shadow #04",
            box_w=0.07,
            box_h=0.21,
            waypoints=(
                (0.00, 0.55, 0.72),
                (0.55, 0.47, 0.75),
                (1.00, 0.77, 0.53),
            ),
        ),
        _EntitySpec(
            id="pivot_06",
            role="Midfield Pivot",
            label="Pivot #06",
            box_w=0.07,
            box_h=0.20,
            waypoints=(
                (0.00, 0.52, 0.50),
                (0.45, 0.55, 0.56),
                (1.00, 0.35, 0.44),
            ),
        ),
        _EntitySpec(
            id="ball",
            role="Ball Vector",
            label="Match Ball",
            box_w=0.03,
            box_h=0.04,
            waypoints=(
                (0.00, 0.66, 0.80),
                (0.30, 0.52, 0.74),
                (0.55, 0.45, 0.70),
                (1.00, 0.82, 0.40),
            ),
        ),
    ),
)

BASKETBALL = _SportProfile(
    name="basketball",
    surface_length_m=28.0,
    surface_width_m=15.0,
    entities=(
        _EntitySpec(
            id="guard_03",
            role="Attacker",
            label="Guard #03",
            box_w=0.09,
            box_h=0.26,
            waypoints=(
                (0.00, 0.20, 0.55),
                (0.45, 0.45, 0.42),
                (1.00, 0.72, 0.48),
            ),
        ),
        _EntitySpec(
            id="wing_11",
            role="Defender",
            label="Wing Defender #11",
            box_w=0.09,
            box_h=0.25,
            waypoints=(
                (0.00, 0.55, 0.60),
                (0.50, 0.50, 0.46),
                (1.00, 0.70, 0.52),
            ),
        ),
        _EntitySpec(
            id="post_21",
            role="Midfield Pivot",
            label="Post #21",
            box_w=0.10,
            box_h=0.27,
            waypoints=(
                (0.00, 0.62, 0.30),
                (0.50, 0.68, 0.42),
                (1.00, 0.78, 0.50),
            ),
        ),
        _EntitySpec(
            id="ball",
            role="Ball Vector",
            label="Game Ball",
            box_w=0.04,
            box_h=0.05,
            waypoints=(
                (0.00, 0.22, 0.58),
                (0.40, 0.46, 0.40),
                (0.70, 0.60, 0.38),
                (1.00, 0.79, 0.49),
            ),
        ),
    ),
)

PROFILES: dict[str, _SportProfile] = {
    FOOTBALL.name: FOOTBALL,
    BASKETBALL.name: BASKETBALL,
}
DEFAULT_SPORT = FOOTBALL.name


def _interpolate(waypoints: Sequence[Waypoint], progress: float) -> tuple[float, float]:
    """Position at ``progress`` by linear interpolation between waypoints."""
    if progress <= waypoints[0][0]:
        return waypoints[0][1], waypoints[0][2]
    for (p0, x0, y0), (p1, x1, y1) in zip(waypoints, waypoints[1:], strict=False):
        if progress <= p1:
            span = p1 - p0
            t = 0.0 if span <= 0 else (progress - p0) / span
            return x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
    return waypoints[-1][1], waypoints[-1][2]


def _sample_indices(frame_count: int, max_samples: int) -> list[int]:
    """Evenly spaced frame indices covering the clip, endpoints included."""
    if frame_count <= 1:
        return [0]
    count = min(max_samples, frame_count)
    if count <= 1:
        return [0]
    last = frame_count - 1
    return [round(i * last / (count - 1)) for i in range(count)]


def resolve_profile(sport: str | None) -> _SportProfile:
    """Look up a sport profile, falling back to football for anything unknown."""
    return PROFILES.get((sport or "").strip().lower(), FOOTBALL)


def simulate_tracking(
    *,
    frame_count: int,
    fps: float,
    width: int,
    height: int,
    sport: str = DEFAULT_SPORT,
    max_samples: int = MAX_SAMPLES,
) -> dict[str, Any]:
    """Generate tracking telemetry shaped to a specific clip.

    The clip's real frame count, frame rate and dimensions drive the output:
    sample timestamps come from ``fps``, coordinates are in the clip's own pixel
    space, and speeds use each sample interval's true duration. Only the
    *trajectories* are invented.

    Distances assume the frame shows the whole playing surface, which is how the
    pixel-to-metre scale is derived. That assumption is part of the simulation
    and is reported back in ``surface``.
    """
    profile = resolve_profile(sport)
    frame_count = max(int(frame_count), 1)
    fps = float(fps) if fps and fps > 0 else 0.0

    indices = _sample_indices(frame_count, max_samples)
    # Clip duration, matching `process_video`'s metadata. Note this is one frame
    # longer than the span the samples cover: timestamps run 0 .. (N-1)/fps, so
    # the last sample lands just short of the end. Keeping both definitions the
    # same key in the same payload would be worse than this one-frame gap.
    duration_sec = frame_count / fps if fps else 0.0

    # Accumulated per-entity state across the walk.
    distance_m: dict[str, float] = {spec.id: 0.0 for spec in profile.entities}
    top_speed: dict[str, float] = {spec.id: 0.0 for spec in profile.entities}
    previous_m: dict[str, tuple[float, float]] = {}

    frames: list[dict[str, Any]] = []
    previous_timestamp = 0.0
    last = frame_count - 1

    for position, index in enumerate(indices):
        progress = index / last if last else 0.0
        timestamp = index / fps if fps else float(position)
        dt = timestamp - previous_timestamp if position else 0.0

        tracked: list[dict[str, Any]] = []
        for spec in profile.entities:
            nx, ny = _interpolate(spec.waypoints, progress)

            # Metres first (scale-independent), pixels second (for the overlay).
            mx = nx * profile.surface_length_m
            my = ny * profile.surface_width_m

            speed = 0.0
            if spec.id in previous_m and dt > 0:
                prev_mx, prev_my = previous_m[spec.id]
                step = math.dist((mx, my), (prev_mx, prev_my))
                distance_m[spec.id] += step
                speed = step / dt
                top_speed[spec.id] = max(top_speed[spec.id], speed)
            previous_m[spec.id] = (mx, my)

            tracked.append(
                {
                    "id": spec.id,
                    "role": spec.role,
                    "label": spec.label,
                    "coordinates": {
                        "x": round(nx * width, 1),
                        "y": round(ny * height, 1),
                        "width": round(spec.box_w * width, 1),
                        "height": round(spec.box_h * height, 1),
                    },
                    "speed_mps": round(speed, 1),
                    "distance_covered_m": round(distance_m[spec.id], 1),
                }
            )

        frames.append(
            {
                "frame_index": index,
                "timestamp_sec": round(timestamp, 2),
                "tracked_entities": tracked,
            }
        )
        previous_timestamp = timestamp

    return {
        "source": SOURCE_SIMULATED,
        "sport": profile.name,
        "surface": {
            "length_m": profile.surface_length_m,
            "width_m": profile.surface_width_m,
            # Spelt out so the UI does not have to re-derive the assumption.
            "assumption": "frame is treated as showing the full playing surface",
        },
        "frame_width": int(width),
        "frame_height": int(height),
        "duration_sec": round(duration_sec, 2),
        "total_frames_sampled": len(frames),
        "frames": frames,
        "tactical_summary": _summarise(
            profile=profile,
            duration_sec=duration_sec,
            sample_count=len(frames),
            distance_m=distance_m,
            top_speed=top_speed,
        ),
    }


def _summarise(
    *,
    profile: _SportProfile,
    duration_sec: float,
    sample_count: int,
    distance_m: dict[str, float],
    top_speed: dict[str, float],
) -> str:
    """Describe the run that was just generated, using its own numbers."""
    window = f"{duration_sec:.1f}s" if duration_sec else "unknown duration"
    lines = [
        f"Simulated {profile.name} sequence — {window}, "
        f"{sample_count} sampled keyframes.",
    ]
    for spec in profile.entities:
        if spec.id == "ball":
            continue
        lines.append(
            f"• {spec.label} ({spec.role}): peak {top_speed[spec.id]:.1f} m/s, "
            f"{distance_m[spec.id]:.1f} m covered."
        )
    lines.append(
        "Figures come from a kinematic model, not from tracking this footage — "
        "treat them as a pipeline placeholder, not scouting data."
    )
    return "\n".join(lines)
