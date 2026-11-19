"""Real multi-object tracking over match footage.

Replaces the kinematic placeholder in ``app.workers.telemetry`` with a detector
run over the actual frames:

1. **Detect + track** players (COCO ``person``) and the ball (COCO
   ``sports ball``) with an Ultralytics YOLO model and its ByteTrack tracker.
2. **Compensate camera motion.** Broadcast cameras pan and zoom constantly, so
   raw pixel displacement is mostly camera, not player. Background features
   (everything outside a player box) are tracked with optical flow and a
   similarity transform maps every frame back onto the first frame's
   coordinates. Speeds and distances are measured in that stabilised space.
3. **Split players into teams** by clustering the colour of each track's
   torso — the kit — into two groups. Tracks far from both kit colours
   (referees, keepers) are labelled ``other``.
4. **Estimate a metric scale** from player height in pixels. There is no pitch
   homography yet, so this is an estimate, and the payload says so.

Two outputs:

* ``telemetry`` — the downsampled payload the dashboard radar already renders
  (same contract as ``simulate_tracking``), with ``source`` naming the model;
* ``tracks`` — every analysed frame at full temporal resolution, written to S3
  as JSON and consumed by ``app.workers.events`` and ``app.workers.render``.

``ultralytics`` (and so torch) is imported lazily: the API process and the test
suite never load it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.workers.telemetry import MAX_SAMPLES, resolve_profile

logger = logging.getLogger(__name__)

COCO_PERSON = 0
COCO_SPORTS_BALL = 32

# Inference resolution. 640 (the Ultralytics default) loses the ball on wide
# broadcast shots; 960 keeps it at roughly 2x the CPU cost.
INFERENCE_SIZE = 960
DETECTION_CONF = 0.15

# Typical standing height, used to turn a bounding box into metres per pixel.
PLAYER_HEIGHT_M = {"football": 1.80, "basketball": 2.00}

# Anything faster than this between two samples is a tracking glitch (an ID
# switch, a bad camera estimate), not a player. Usain Bolt peaks near 12.4 m/s.
MAX_PLAUSIBLE_SPEED_MPS = 11.0

# A track must be seen this many times to count as a player rather than noise.
MIN_TRACK_OBSERVATIONS = 3

# Ball gaps up to this many analysed frames are filled by interpolation.
MAX_BALL_GAP = 4


# ---------------------------------------------------------------------------
# Small data holders
# ---------------------------------------------------------------------------


@dataclass
class _PlayerObs:
    track_id: int
    box: tuple[float, float, float, float]
    conf: float


@dataclass
class _Frame:
    index: int
    timestamp: float
    # 2x3 affine mapping this frame's pixels onto the reference (first) frame.
    to_ref: np.ndarray
    players: list[_PlayerObs] = field(default_factory=list)
    ball: tuple[float, float, float] | None = None  # cx, cy, radius
    ball_interpolated: bool = False


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _compose(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Return the 2x3 affine equal to applying ``b`` then ``a``."""
    a3 = np.vstack([a, [0.0, 0.0, 1.0]])
    b3 = np.vstack([b, [0.0, 0.0, 1.0]])
    return (a3 @ b3)[:2]


def _apply(m: np.ndarray, x: float, y: float) -> tuple[float, float]:
    return (
        float(m[0, 0] * x + m[0, 1] * y + m[0, 2]),
        float(m[1, 0] * x + m[1, 1] * y + m[1, 2]),
    )


def _scale_of(m: np.ndarray) -> float:
    """Uniform scale factor of a similarity transform."""
    return float(math.sqrt(abs(m[0, 0] * m[1, 1] - m[0, 1] * m[1, 0]))) or 1.0


def _foot_point(box: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, _, x2, y2 = box
    return (x1 + x2) / 2.0, y2


def _estimate_camera_motion(
    prev_gray: np.ndarray,
    gray: np.ndarray,
    mask: np.ndarray,
) -> np.ndarray:
    """Similarity transform taking ``gray`` pixels onto ``prev_gray`` pixels.

    Returns identity when there is too little texture to trust an estimate
    (a close-up, a fade to black) rather than a wild guess.
    """
    identity = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    points = cv2.goodFeaturesToTrack(
        prev_gray, maxCorners=400, qualityLevel=0.01, minDistance=8, mask=mask
    )
    if points is None or len(points) < 12:
        return identity
    moved, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, points, None)
    if moved is None:
        return identity
    good = status.reshape(-1) == 1
    if good.sum() < 12:
        return identity
    # Estimate cur -> prev, which is the direction needed to walk back to the
    # reference frame.
    matrix, inliers = cv2.estimateAffinePartial2D(
        moved[good], points[good], method=cv2.RANSAC, ransacReprojThreshold=2.0
    )
    if matrix is None or inliers is None or inliers.sum() < 10:
        return identity
    return matrix


def _background_mask(shape: tuple[int, int], boxes: list[tuple[float, ...]]) -> np.ndarray:
    """Mask of pixels usable for camera-motion features (players excluded)."""
    height, width = shape
    mask = np.full((height, width), 255, dtype=np.uint8)
    for x1, y1, x2, y2 in boxes:
        pad_x = (x2 - x1) * 0.15
        pad_y = (y2 - y1) * 0.1
        cv2.rectangle(
            mask,
            (int(max(x1 - pad_x, 0)), int(max(y1 - pad_y, 0))),
            (int(min(x2 + pad_x, width - 1)), int(min(y2 + pad_y, height - 1))),
            0,
            thickness=-1,
        )
    # Broadcast graphics (score bug, clock) sit in the top band and never move
    # with the camera; excluding it stops them pinning the estimate to zero.
    mask[: int(height * 0.12), :] = 0
    return mask


def _torso_colour(frame: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray | None:
    """Median Lab colour of the shirt area, with pitch-green pixels removed."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    if w < 6 or h < 14:
        return None
    tx1, tx2 = int(x1 + w * 0.25), int(x2 - w * 0.25)
    ty1, ty2 = int(y1 + h * 0.15), int(y1 + h * 0.50)
    crop = frame[max(ty1, 0) : max(ty2, 0), max(tx1, 0) : max(tx2, 0)]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    green = (hsv[..., 0] > 35) & (hsv[..., 0] < 85) & (hsv[..., 1] > 60)
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB).reshape(-1, 3)
    keep = lab[~green.reshape(-1)]
    if len(keep) < 8:
        return None
    return np.median(keep, axis=0).astype(np.float32)


def _lab_to_bgr(lab: np.ndarray) -> list[int]:
    pixel = np.clip(lab, 0, 255).astype(np.uint8).reshape(1, 1, 3)
    bgr = cv2.cvtColor(pixel, cv2.COLOR_LAB2BGR).reshape(3)
    return [int(c) for c in bgr]


def colour_name(bgr: list[int]) -> str:
    """Rough human name for a kit colour, for captions and the script."""
    pixel = np.array(bgr, dtype=np.uint8).reshape(1, 1, 3)
    h, s, v = (int(c) for c in cv2.cvtColor(pixel, cv2.COLOR_BGR2HSV).reshape(3))
    if v < 60:
        return "black"
    if s < 45:
        return "white" if v > 170 else "grey"
    hue = h * 2  # OpenCV hue is 0-179
    for limit, name in (
        (15, "red"),
        (40, "orange"),
        (70, "yellow"),
        (160, "green"),
        (200, "cyan"),
        (255, "blue"),
        (290, "purple"),
        (340, "pink"),
        (360, "red"),
    ):
        if hue < limit:
            return name
    return "red"


def _display_colour(bgr: list[int]) -> list[int]:
    """Kit colour boosted so it reads as an overlay against the pitch."""
    pixel = np.array(bgr, dtype=np.uint8).reshape(1, 1, 3)
    hsv = cv2.cvtColor(pixel, cv2.COLOR_BGR2HSV).astype(np.int32)
    if hsv[0, 0, 1] >= 45:
        # Only saturate real colours; boosting a white/grey/black kit would
        # invent a hue the shirt does not have.
        hsv[..., 1] = np.clip(hsv[..., 1] * 1.4 + 30, 0, 255)
    hsv[..., 2] = np.clip(hsv[..., 2] * 1.2 + 40, 0, 255)
    out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).reshape(3)
    return [int(c) for c in out]


# ---------------------------------------------------------------------------
# Team assignment
# ---------------------------------------------------------------------------


def _assign_teams(colours: dict[int, list[np.ndarray]]) -> tuple[dict[int, str], dict[str, Any]]:
    """Cluster track kit colours into two teams plus ``other``."""
    track_ids = [tid for tid, samples in colours.items() if len(samples) >= MIN_TRACK_OBSERVATIONS]
    assignment: dict[int, str] = {tid: "other" for tid in colours}
    teams: dict[str, Any] = {}
    if len(track_ids) < 4:
        return assignment, teams

    vectors = np.array(
        [np.median(np.array(colours[tid]), axis=0) for tid in track_ids], dtype=np.float32
    )
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.5)
    _, labels, centres = cv2.kmeans(vectors, 2, None, criteria, 5, cv2.KMEANS_PP_CENTERS)
    labels = labels.reshape(-1)

    distances = np.linalg.norm(vectors - centres[labels], axis=1)
    cutoff = max(float(np.median(distances)) * 2.5, 18.0)

    # Larger cluster becomes Team A so the naming is stable run to run.
    order = np.argsort([-int((labels == k).sum()) for k in range(2)])
    names = {int(order[0]): "A", int(order[1]): "B"}

    for tid, label, dist in zip(track_ids, labels, distances, strict=True):
        assignment[tid] = names[int(label)] if dist <= cutoff else "other"

    for k, key in names.items():
        kit = _lab_to_bgr(centres[k])
        teams[key] = {
            "label": f"Team {key}",
            "kit_bgr": kit,
            "colour_name": colour_name(kit),
            "overlay_bgr": _display_colour(kit),
            "players": sorted(t for t, team in assignment.items() if team == key),
        }
    return assignment, teams


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _load_model(weights: str):
    # Imported here so only the worker pays for torch.
    from ultralytics import YOLO

    return YOLO(weights)


def track_clip(
    path: str | Path,
    *,
    sport: str,
    fps: float,
    frame_count: int,
    width: int,
    height: int,
    weights: str,
    analysis_fps: float,
    max_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run detection + tracking over a clip. Returns ``(telemetry, tracks)``."""
    profile = resolve_profile(sport)
    stride = max(1, round(fps / analysis_fps)) if analysis_fps > 0 else 1
    limit = int(max_seconds * fps) if max_seconds > 0 else frame_count
    last_frame = min(frame_count, limit) if frame_count > 0 else limit

    # A fresh model per clip so ByteTrack's IDs and state never leak between
    # clips processed by the same worker.
    model = _load_model(weights)
    source = f"{Path(weights).stem}+bytetrack"

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError("Could not open video for tracking")

    frames: list[_Frame] = []
    colours: dict[int, list[np.ndarray]] = {}
    prev_gray: np.ndarray | None = None
    prev_boxes: list[tuple[float, ...]] = []
    to_ref = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    try:
        index = 0
        while index < last_frame:
            if index % stride:
                if not capture.grab():
                    break
                index += 1
                continue
            ok, image = capture.read()
            if not ok:
                break

            result = model.track(
                image,
                persist=True,
                classes=[COCO_PERSON, COCO_SPORTS_BALL],
                tracker="bytetrack.yaml",
                conf=DETECTION_CONF,
                imgsz=INFERENCE_SIZE,
                verbose=False,
            )[0]

            frame = _Frame(index=index, timestamp=index / fps, to_ref=to_ref)
            boxes = result.boxes
            best_ball: tuple[float, float, float, float] | None = None  # conf, cx, cy, r
            if boxes is not None and len(boxes):
                xyxy = boxes.xyxy.cpu().numpy()
                cls = boxes.cls.cpu().numpy().astype(int)
                conf = boxes.conf.cpu().numpy()
                ids = boxes.id.cpu().numpy().astype(int) if boxes.id is not None else None
                for j in range(len(xyxy)):
                    x1, y1, x2, y2 = (float(v) for v in xyxy[j])
                    if cls[j] == COCO_SPORTS_BALL:
                        if best_ball is None or conf[j] > best_ball[0]:
                            radius = max(x2 - x1, y2 - y1) / 2.0
                            best_ball = (float(conf[j]), (x1 + x2) / 2, (y1 + y2) / 2, radius)
                        continue
                    if ids is None:
                        continue
                    track_id = int(ids[j])
                    box = (x1, y1, x2, y2)
                    frame.players.append(_PlayerObs(track_id, box, float(conf[j])))
                    colour = _torso_colour(image, box)
                    if colour is not None:
                        samples = colours.setdefault(track_id, [])
                        if len(samples) < 40:
                            samples.append(colour)
            if best_ball is not None:
                frame.ball = best_ball[1:]

            # Camera motion relative to the previous analysed frame.
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                mask = _background_mask(prev_gray.shape[:2], prev_boxes)
                step = _estimate_camera_motion(prev_gray, gray, mask)
                to_ref = _compose(to_ref, step)
                frame.to_ref = to_ref
            prev_gray = gray
            prev_boxes = [p.box for p in frame.players]

            frames.append(frame)
            index += 1
    finally:
        capture.release()

    if not frames:
        raise ValueError("No frames could be decoded for tracking")

    _fill_ball_gaps(frames)
    team_of, teams = _assign_teams(colours)
    player_stats, per_frame_speed = _kinematics(frames, team_of, profile.name)

    tracks = _tracks_payload(
        frames=frames,
        team_of=team_of,
        teams=teams,
        player_stats=player_stats,
        per_frame_speed=per_frame_speed,
        source=source,
        sport=profile.name,
        fps=fps,
        stride=stride,
        width=width,
        height=height,
    )
    telemetry = _telemetry_payload(tracks, profile_name=profile.name, frame_count=frame_count)
    return telemetry, tracks


def _fill_ball_gaps(frames: list[_Frame]) -> None:
    """Linearly interpolate short gaps in ball detections."""
    seen = [i for i, f in enumerate(frames) if f.ball is not None]
    for a, b in zip(seen, seen[1:], strict=False):
        gap = b - a - 1
        if gap <= 0 or gap > MAX_BALL_GAP:
            continue
        start, end = frames[a].ball, frames[b].ball
        assert start is not None and end is not None
        for k in range(1, gap + 1):
            t = k / (gap + 1)
            frames[a + k].ball = tuple(s + (e - s) * t for s, e in zip(start, end, strict=True))  # type: ignore[assignment]
            frames[a + k].ball_interpolated = True


def _kinematics(
    frames: list[_Frame], team_of: dict[int, str], sport: str
) -> tuple[dict[int, dict[str, Any]], dict[tuple[int, int], float]]:
    """Distance and speed per player in stabilised, height-scaled metres."""
    height_m = PLAYER_HEIGHT_M.get(sport, 1.8)
    # track id -> list of (frame position, t, ref_x, ref_y, metres per ref pixel)
    history: dict[int, list[tuple[int, float, float, float, float]]] = {}
    for pos, frame in enumerate(frames):
        scale = _scale_of(frame.to_ref)
        for obs in frame.players:
            fx, fy = _foot_point(obs.box)
            rx, ry = _apply(frame.to_ref, fx, fy)
            box_h_ref = (obs.box[3] - obs.box[1]) * scale
            if box_h_ref <= 1:
                continue
            history.setdefault(obs.track_id, []).append(
                (pos, frame.timestamp, rx, ry, height_m / box_h_ref)
            )

    stats: dict[int, dict[str, Any]] = {}
    speeds: dict[tuple[int, int], float] = {}
    for tid, samples in history.items():
        if len(samples) < MIN_TRACK_OBSERVATIONS:
            continue
        xs = np.array([s[2] for s in samples])
        ys = np.array([s[3] for s in samples])
        # 3-sample moving average: box jitter otherwise reads as running.
        if len(samples) >= 3:
            kernel = np.ones(3) / 3
            xs = np.convolve(np.pad(xs, 1, mode="edge"), kernel, mode="valid")
            ys = np.convolve(np.pad(ys, 1, mode="edge"), kernel, mode="valid")
        mpp = float(np.median([s[4] for s in samples]))

        distance = 0.0
        top = 0.0
        for k in range(1, len(samples)):
            dt = samples[k][1] - samples[k - 1][1]
            if dt <= 0:
                continue
            step = math.hypot(xs[k] - xs[k - 1], ys[k] - ys[k - 1]) * mpp
            speed = step / dt
            if speed > MAX_PLAUSIBLE_SPEED_MPS:
                continue
            distance += step
            top = max(top, speed)
            speeds[(samples[k][0], tid)] = speed

        stats[tid] = {
            "team": team_of.get(tid, "other"),
            "observations": len(samples),
            "first_t": round(samples[0][1], 2),
            "last_t": round(samples[-1][1], 2),
            "distance_m": round(distance, 1),
            "top_speed_mps": round(top, 1),
            "metres_per_ref_px": round(mpp, 5),
        }
    return stats, speeds


def _tracks_payload(
    *,
    frames: list[_Frame],
    team_of: dict[int, str],
    teams: dict[str, Any],
    player_stats: dict[int, dict[str, Any]],
    per_frame_speed: dict[tuple[int, int], float],
    source: str,
    sport: str,
    fps: float,
    stride: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    out_frames: list[dict[str, Any]] = []
    for pos, frame in enumerate(frames):
        players = []
        for obs in frame.players:
            if obs.track_id not in player_stats:
                continue  # too short-lived to be trusted
            players.append(
                {
                    "id": obs.track_id,
                    "team": team_of.get(obs.track_id, "other"),
                    "box": [round(v, 1) for v in obs.box],
                    "conf": round(obs.conf, 2),
                    "speed_mps": round(per_frame_speed.get((pos, obs.track_id), 0.0), 1),
                }
            )
        out_frames.append(
            {
                "i": frame.index,
                "t": round(frame.timestamp, 3),
                "to_ref": [round(float(v), 5) for v in frame.to_ref.reshape(-1)],
                "players": players,
                "ball": [round(v, 1) for v in frame.ball] if frame.ball else None,
                "ball_interpolated": frame.ball_interpolated,
            }
        )

    ball_frames = sum(1 for f in frames if f.ball is not None)
    return {
        "version": 1,
        "source": source,
        "sport": sport,
        "fps": fps,
        "stride": stride,
        "frame_width": width,
        "frame_height": height,
        "analysed_frames": len(frames),
        "analysed_until_sec": round(frames[-1].timestamp, 3),
        "ball_detection_rate": round(ball_frames / len(frames), 3),
        "teams": teams,
        "players": {str(k): v for k, v in player_stats.items()},
        "frames": out_frames,
    }


def _telemetry_payload(
    tracks: dict[str, Any], *, profile_name: str, frame_count: int
) -> dict[str, Any]:
    """Downsample ``tracks`` into the dashboard's telemetry contract."""
    frames = tracks["frames"]
    teams = tracks["teams"]
    players = tracks["players"]
    fps = tracks["fps"]
    profile = resolve_profile(profile_name)

    count = min(MAX_SAMPLES, len(frames))
    if count <= 1:
        picks = [0]
    else:
        picks = sorted({round(i * (len(frames) - 1) / (count - 1)) for i in range(count)})

    running_distance: dict[str, float] = {}
    sampled = []
    for pos in picks:
        frame = frames[pos]
        entities = []
        for p in frame["players"]:
            key = str(p["id"])
            team = p["team"]
            colour = teams.get(team, {}).get("overlay_bgr")
            entities.append(
                {
                    "id": f"player_{key}",
                    "role": teams.get(team, {}).get("label", "Other"),
                    "label": f"{team.upper() if team != 'other' else '?'}·{key}",
                    "team": team,
                    "color": _hex(colour) if colour else None,
                    "coordinates": _xywh(p["box"]),
                    "speed_mps": p["speed_mps"],
                    # Running total is not stored per frame; report the
                    # track's total so the radar's sidebar is meaningful.
                    "distance_covered_m": players.get(key, {}).get("distance_m", 0.0),
                }
            )
            running_distance[key] = players.get(key, {}).get("distance_m", 0.0)
        if frame["ball"]:
            cx, cy, r = frame["ball"]
            entities.append(
                {
                    "id": "ball",
                    "role": "Ball Vector",
                    "label": "Ball",
                    "team": None,
                    "color": None,
                    "coordinates": {
                        "x": round(cx, 1),
                        "y": round(cy, 1),
                        "width": round(2 * r, 1),
                        "height": round(2 * r, 1),
                    },
                    "speed_mps": 0.0,
                    "distance_covered_m": 0.0,
                }
            )
        sampled.append(
            {
                "frame_index": frame["i"],
                "timestamp_sec": round(frame["t"], 2),
                "tracked_entities": entities,
            }
        )

    duration = frame_count / fps if fps else 0.0
    return {
        "source": tracks["source"],
        "sport": profile.name,
        "surface": {
            "length_m": profile.surface_length_m,
            "width_m": profile.surface_width_m,
            "assumption": (
                "metres estimated from player height in pixels "
                f"(~{PLAYER_HEIGHT_M.get(profile.name, 1.8)} m); camera pan/zoom "
                "compensated with background optical flow; no pitch homography"
            ),
        },
        "frame_width": tracks["frame_width"],
        "frame_height": tracks["frame_height"],
        "duration_sec": round(duration, 2),
        "analysed_until_sec": tracks["analysed_until_sec"],
        "total_frames_sampled": len(sampled),
        "frames": sampled,
        "teams": {
            k: {
                "label": v["label"],
                "colour_name": v["colour_name"],
                "color": _hex(v["overlay_bgr"]),
            }
            for k, v in teams.items()
        },
        "tactical_summary": _summarise(tracks),
    }


def _summarise(tracks: dict[str, Any]) -> str:
    """Plain-language summary built only from the measured numbers."""
    players = tracks["players"]
    teams = tracks["teams"]
    lines = [
        f"Tracked {tracks['sport']} footage with {tracks['source']} — "
        f"{tracks['analysed_frames']} frames analysed over "
        f"{tracks['analysed_until_sec']:.1f}s; ball found in "
        f"{tracks['ball_detection_rate'] * 100:.0f}% of them.",
    ]
    for key in ("A", "B"):
        if key not in teams:
            continue
        count = sum(1 for p in players.values() if p["team"] == key)
        lines.append(f"• {teams[key]['label']} ({teams[key]['colour_name']} kit): {count} tracks.")
    fastest = sorted(players.items(), key=lambda kv: kv[1]["top_speed_mps"], reverse=True)[:3]
    for key, stats in fastest:
        lines.append(
            f"• Track {key} ({stats['team']}): peak ≈{stats['top_speed_mps']:.1f} m/s, "
            f"≈{stats['distance_m']:.0f} m covered."
        )
    if not teams:
        lines.append("Too few players tracked to separate the two teams by kit colour.")
    lines.append(
        "Speeds and distances are estimates scaled from player height, not "
        "calibrated pitch measurements."
    )
    return "\n".join(lines)


def _xywh(box: list[float]) -> dict[str, float]:
    """Centre-based box, matching the simulated telemetry's coordinate convention."""
    x1, y1, x2, y2 = box
    return {
        "x": round((x1 + x2) / 2, 1),
        "y": round((y1 + y2) / 2, 1),
        "width": round(x2 - x1, 1),
        "height": round(y2 - y1, 1),
    }


def _hex(bgr: list[int]) -> str:
    b, g, r = bgr
    return f"#{r:02x}{g:02x}{b:02x}"
