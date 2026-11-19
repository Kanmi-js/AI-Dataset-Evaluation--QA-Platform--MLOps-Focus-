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

# Default inference resolution (overridable via settings.inference_size). The
# ball on a wide broadcast shot is a handful of pixels; 640 (the Ultralytics
# default) loses it, 1280 keeps it at roughly 4x the CPU cost of 640.
INFERENCE_SIZE = 1280

# Raw detections are kept down to this confidence. Players then go through
# ByteTrack, which separates strong and weak detections itself; the ball does
# not (see _pick_ball), because a tracker will not start a track below 0.25 and
# a small, fast, motion-blurred ball rarely scores that high.
DETECTION_CONF = 0.05
# With no recent ball position to support it, a lone candidate needs this much.
BALL_START_CONF = 0.15
# A candidate this confident is accepted even if it is far from the last one.
BALL_SURE_CONF = 0.35

# Typical standing height, used to turn a bounding box into metres per pixel.
PLAYER_HEIGHT_M = {"football": 1.80, "basketball": 2.00}

# Ball diameter (football ~0.22 m, basketball ~0.24 m) relative to player
# height, with generous slack for motion blur and box padding. Candidates
# outside this band are heads, hoops, logos or crowd, not the ball.
BALL_SIZE_RATIO = (0.04, 0.30)

# Fastest the ball plausibly travels between two analysed frames, in m/s: a
# struck football reaches ~30 m/s, a basketball pass ~15 m/s.
MAX_BALL_SPEED_MPS = {"football": 32.0, "basketball": 18.0}

# Anything faster than this is a tracking glitch (an ID switch, a bad camera
# estimate, a box shrinking behind a defender), not a player. Football
# sprinters top out near 10.5 m/s; basketball's short court caps around 9.
MAX_PLAUSIBLE_SPEED_MPS = {"football": 11.0, "basketball": 9.5}

# Speeds are measured over this window rather than frame to frame, so box
# jitter is averaged out instead of read as acceleration.
SPEED_WINDOW_SEC = 0.5

# A track must be seen this many times to count as a player rather than noise.
MIN_TRACK_OBSERVATIONS = 3

# Ball gaps up to this many analysed frames are filled by interpolation.
MAX_BALL_GAP = 6

# ByteTrack tuning. The buffer is in analysed frames (10 fps by default), so 30
# keeps a lost player's ID alive for ~3 s through screens and crossings.
TRACKER_SETTINGS = {
    "tracker_type": "bytetrack",
    "track_high_thresh": 0.25,
    "track_low_thresh": 0.1,
    "new_track_thresh": 0.3,
    "track_buffer": 30,
    "match_thresh": 0.8,
    "fuse_score": True,
    "model": None,
}

# Tracklet stitching: a track that ends and another that starts within this
# window, close enough to be the same runner and in the same kit, are merged.
STITCH_MAX_GAP_SEC = 2.0
STITCH_MAX_COLOUR_DIST = 28.0


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


# Torso pixels within this Lab distance of the surrounding background are
# treated as background (court floor, pitch, advertising boards) and dropped.
BACKGROUND_COLOUR_DIST = 22.0


def _crop(frame: np.ndarray, x1: float, y1: float, x2: float, y2: float) -> np.ndarray:
    h, w = frame.shape[:2]
    xa, xb = int(max(x1, 0)), int(min(x2, w))
    ya, yb = int(max(y1, 0)), int(min(y2, h))
    if xb <= xa or yb <= ya:
        return frame[0:0, 0:0]
    return frame[ya:yb, xa:xb]


def _background_lab(frame: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray | None:
    """Median Lab colour of the strips either side of a player, at torso height.

    This is whatever the player is standing in front of: grass, a wooden or
    painted court, boards. Sampling it per player means the kit filter works on
    any surface, not only on green pitches.
    """
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    ty1, ty2 = y1 + h * 0.15, y1 + h * 0.55
    strips = [
        _crop(frame, x1 - w * 0.45, ty1, x1 - w * 0.05, ty2),
        _crop(frame, x2 + w * 0.05, ty1, x2 + w * 0.45, ty2),
    ]
    pixels = [cv2.cvtColor(s, cv2.COLOR_BGR2LAB).reshape(-1, 3) for s in strips if s.size]
    if not pixels:
        return None
    stacked = np.concatenate(pixels)
    if len(stacked) < 8:
        return None
    return np.median(stacked, axis=0).astype(np.float32)


def _torso_colour(frame: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray | None:
    """Median Lab colour of the shirt, with background-coloured pixels removed.

    Pixels matching the local background (and, as a backstop, pitch green) are
    dropped before taking the median, so a player in front of a light wooden
    floor is not read as wearing orange.
    """
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    if w < 6 or h < 14:
        return None
    crop = _crop(frame, x1 + w * 0.25, y1 + h * 0.15, x2 - w * 0.25, y1 + h * 0.50)
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    green = (hsv[:, 0] > 35) & (hsv[:, 0] < 85) & (hsv[:, 1] > 60)
    keep = ~green

    background = _background_lab(frame, box)
    if background is not None:
        near_bg = np.linalg.norm(lab - background, axis=1) < BACKGROUND_COLOUR_DIST
        if (keep & ~near_bg).sum() >= 8:
            keep &= ~near_bg
        # Otherwise the kit really is the background's colour (white on a white
        # floor); keep the green-filtered pixels rather than return nothing.

    if keep.sum() < 8:
        return None
    return _dominant_lab(lab[keep])


def _dominant_lab(pixels: np.ndarray) -> np.ndarray:
    """Kit colour from a torso crop's pixels (N x 3 Lab).

    A plain median blends the shirt with whatever sits on it: a white sponsor,
    the number, an arm. On a red kit that blend lands on orange. Instead the
    pixels are split into up to three patches and the kit is chosen from them:

    * skin-toned patches are dropped when anything else is sizeable;
    * a strongly coloured patch covering a quarter of the torso beats a larger
      washed-out one (a red shirt under a big white sponsor), while a white kit
      with small coloured trim stays white;
    * otherwise the largest patch wins.
    """
    pixels = pixels.astype(np.float32)
    if len(pixels) < 30:
        return np.median(pixels, axis=0).astype(np.float32)
    k = 3 if len(pixels) >= 60 else 2
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, _ = cv2.kmeans(pixels, k, None, criteria, 2, cv2.KMEANS_PP_CENTERS)
    labels = labels.reshape(-1)

    patches = []
    for c in range(k):
        members = pixels[labels == c]
        if not len(members):
            continue
        centre = np.median(members, axis=0)
        bgr = np.array(_lab_to_bgr(centre), dtype=np.uint8).reshape(1, 1, 3)
        h, sat, val = (int(v) for v in cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).reshape(3))
        hue = h * 2
        patches.append(
            {
                "share": len(members) / len(pixels),
                "centre": centre,
                "sat": sat,
                "skin": 5 <= hue <= 40 and 35 <= sat <= 170 and val >= 120,
            }
        )
    patches.sort(key=lambda p: p["share"], reverse=True)

    non_skin = [p for p in patches if not p["skin"]]
    if non_skin and max(p["share"] for p in non_skin) >= 0.2:
        patches = non_skin

    top = patches[0]
    if top["sat"] < 45:
        coloured = [p for p in patches if p["sat"] >= 60 and p["share"] >= 0.25]
        if coloured:
            top = coloured[0]
    return np.asarray(top["centre"], dtype=np.float32)


def _lab_to_bgr(lab: np.ndarray) -> list[int]:
    pixel = np.clip(lab, 0, 255).astype(np.uint8).reshape(1, 1, 3)
    bgr = cv2.cvtColor(pixel, cv2.COLOR_LAB2BGR).reshape(3)
    return [int(c) for c in bgr]


def colour_name(bgr: list[int]) -> str:
    """Rough human name for a kit colour, for captions and the script."""
    pixel = np.array(bgr, dtype=np.uint8).reshape(1, 1, 3)
    h, s, v = (int(c) for c in cv2.cvtColor(pixel, cv2.COLOR_BGR2HSV).reshape(3))
    if v < 55:
        return "black"
    if s < 45:
        if v > 185:
            return "white"
        return "grey" if v > 95 else "dark grey"
    hue = h * 2  # OpenCV hue is 0-179
    if 175 <= hue < 260:
        # Blues are where kits differ most and plain "blue" helps least:
        # navy vs sky blue is usually exactly what separates the two teams.
        if v < 115:
            return "navy"
        if s < 140 and v > 150:
            return "light blue"
        return "blue"
    if 140 <= hue < 175:
        return "teal" if v < 150 else "turquoise"
    if 18 <= hue < 40 and v < 140:
        return "brown"
    for limit, name in (
        # Red kits under floodlights drift to ~10-17 degrees; orange starts
        # nearer 20, so the boundary sits at 18 rather than 15.
        (18, "red"),
        (40, "orange"),
        (70, "yellow"),
        (140, "green"),
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

    raw = np.array(
        [np.median(np.array(colours[tid]), axis=0) for tid in track_ids], dtype=np.float32
    )
    # Lightness is halved for clustering: the same shirt in shadow and in
    # floodlight differs mostly in L, while two different kits differ in hue.
    weights = np.array([0.5, 1.0, 1.0], dtype=np.float32)
    vectors = raw * weights
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.5)
    _, labels, centres = cv2.kmeans(vectors, 2, None, criteria, 5, cv2.KMEANS_PP_CENTERS)
    labels = labels.reshape(-1)
    centres = centres / weights

    distances = np.linalg.norm(vectors - (centres * weights)[labels], axis=1)
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


def _new_tracker():
    """A fresh ByteTrack instance, driven by hand with player detections only."""
    from types import SimpleNamespace

    from ultralytics.trackers.byte_tracker import BYTETracker

    return BYTETracker(SimpleNamespace(**TRACKER_SETTINGS))


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
    inference_size: int = INFERENCE_SIZE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run detection + tracking over a clip. Returns ``(telemetry, tracks)``."""
    profile = resolve_profile(sport)
    stride = max(1, round(fps / analysis_fps)) if analysis_fps > 0 else 1
    limit = int(max_seconds * fps) if max_seconds > 0 else frame_count
    last_frame = min(frame_count, limit) if frame_count > 0 else limit

    # Fresh model and tracker per clip so IDs and state never leak between
    # clips processed by the same worker.
    model = _load_model(weights)
    tracker = _new_tracker()
    source = f"{Path(weights).stem}+bytetrack"

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError("Could not open video for tracking")

    frames: list[_Frame] = []
    colours: dict[int, list[np.ndarray]] = {}
    prev_gray: np.ndarray | None = None
    prev_boxes: list[tuple[float, ...]] = []
    to_ref = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    last_ball: tuple[float, float, float] | None = None
    frames_since_ball = 0

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

            # One detector pass for everything; players and ball then part ways.
            result = model.predict(
                image,
                classes=[COCO_PERSON, COCO_SPORTS_BALL],
                conf=DETECTION_CONF,
                imgsz=inference_size,
                verbose=False,
            )[0]
            detections = result.boxes.cpu().numpy()
            cls = detections.cls.astype(int)

            frame = _Frame(index=index, timestamp=index / fps, to_ref=to_ref)

            people = detections[cls == COCO_PERSON]
            tracked = tracker.update(people, image) if len(people) else np.empty((0, 8))
            for row in tracked:
                x1, y1, x2, y2, track_id = (float(v) for v in row[:5])
                box = (x1, y1, x2, y2)
                frame.players.append(_PlayerObs(int(track_id), box, float(row[5])))
                colour = _torso_colour(image, box)
                if colour is not None:
                    samples = colours.setdefault(int(track_id), [])
                    if len(samples) < 40:
                        samples.append(colour)

            confident_heights = [
                float(b[3] - b[1])
                for b, c in zip(people.xyxy, people.conf, strict=True)
                if c >= 0.3
            ]
            person_h = float(np.median(confident_heights)) if confident_heights else None
            balls = detections[cls == COCO_SPORTS_BALL]
            candidates = [
                (float(c), (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, max(b[2] - b[0], b[3] - b[1]) / 2)
                for b, c in zip(balls.xyxy, balls.conf, strict=True)
            ]
            dt = stride / fps * (frames_since_ball + 1)
            frame.ball = _pick_ball(
                candidates,
                previous=last_ball,
                person_height_px=person_h,
                max_jump_px=_max_ball_jump_px(profile.name, person_h, dt),
            )
            if frame.ball is not None:
                last_ball, frames_since_ball = frame.ball, 0
            else:
                frames_since_ball += 1
                if frames_since_ball > MAX_BALL_GAP * 2:
                    last_ball = None  # stale: require a confident re-acquisition

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
    _stitch_tracklets(frames, colours, profile.name)
    _renumber_tracks(frames, colours)
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


# ---------------------------------------------------------------------------
# Ball
# ---------------------------------------------------------------------------


def _max_ball_jump_px(sport: str, person_height_px: float | None, dt: float) -> float | None:
    """How far, in pixels, the ball can plausibly move in ``dt`` seconds."""
    if not person_height_px or dt <= 0:
        return None
    px_per_m = person_height_px / PLAYER_HEIGHT_M.get(sport, 1.8)
    return MAX_BALL_SPEED_MPS.get(sport, 30.0) * dt * px_per_m + person_height_px * 0.5


def _pick_ball(
    candidates: list[tuple[float, float, float, float]],
    *,
    previous: tuple[float, float, float] | None,
    person_height_px: float | None,
    max_jump_px: float | None,
) -> tuple[float, float, float] | None:
    """Choose this frame's ball from ``(conf, cx, cy, radius)`` candidates.

    Size is checked against player height, so heads, hoops and crowd blobs are
    dropped. With a recent ball position, the candidate that is both confident
    and close to it wins, and one that teleports is rejected unless it is very
    confident. Without one, only a reasonably confident candidate starts a new
    trail.
    """
    plausible = candidates
    if person_height_px:
        lo, hi = BALL_SIZE_RATIO
        plausible = [
            c for c in candidates if lo * person_height_px <= 2 * c[3] <= hi * person_height_px
        ]
    if not plausible:
        return None

    if previous is None or not max_jump_px:
        best = max(plausible, key=lambda c: c[0])
        return best[1:] if best[0] >= BALL_START_CONF else None

    def score(c: tuple[float, float, float, float]) -> float:
        jump = math.hypot(c[1] - previous[0], c[2] - previous[1]) / max_jump_px
        return c[0] - 0.4 * min(jump, 2.0)

    for c in sorted(plausible, key=score, reverse=True):
        jump = math.hypot(c[1] - previous[0], c[2] - previous[1])
        if jump <= max_jump_px or c[0] >= BALL_SURE_CONF:
            return c[1:]
    return None


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


# ---------------------------------------------------------------------------
# Track identity
# ---------------------------------------------------------------------------


@dataclass
class _Tracklet:
    track_id: int
    first_pos: int
    last_pos: int
    first_t: float
    last_t: float
    first_ref: tuple[float, float]
    last_ref: tuple[float, float]
    metres_per_px: float
    colour: np.ndarray | None


def _tracklets(
    frames: list[_Frame], colours: dict[int, list[np.ndarray]], sport: str
) -> dict[int, _Tracklet]:
    height_m = PLAYER_HEIGHT_M.get(sport, 1.8)
    out: dict[int, _Tracklet] = {}
    heights: dict[int, list[float]] = {}
    for pos, frame in enumerate(frames):
        scale = _scale_of(frame.to_ref)
        for obs in frame.players:
            ref = _apply(frame.to_ref, *_foot_point(obs.box))
            heights.setdefault(obs.track_id, []).append((obs.box[3] - obs.box[1]) * scale)
            t = out.get(obs.track_id)
            if t is None:
                out[obs.track_id] = _Tracklet(
                    obs.track_id, pos, pos, frame.timestamp, frame.timestamp, ref, ref, 0.0, None
                )
            else:
                t.last_pos, t.last_t, t.last_ref = pos, frame.timestamp, ref
    for tid, t in out.items():
        h = float(np.median(heights[tid])) if heights.get(tid) else 0.0
        t.metres_per_px = height_m / h if h > 1 else 0.0
        samples = colours.get(tid)
        t.colour = np.median(np.array(samples), axis=0) if samples else None
    return out


def _stitch_tracklets(
    frames: list[_Frame], colours: dict[int, list[np.ndarray]], sport: str
) -> dict[int, int]:
    """Merge tracks that are one player whose ID broke (a screen, a crossing).

    A later track is joined onto an earlier one when the earlier ended before
    the later began (they never coexist), the gap is short, the distance
    between where one left off and the other started is runnable in that gap,
    and the kit colours match. Greedy, closest-first. Rewrites ``frames`` and
    ``colours`` in place and returns the old -> new id mapping.
    """
    tracklets = _tracklets(frames, colours, sport)
    max_speed = MAX_PLAUSIBLE_SPEED_MPS.get(sport, 11.0)
    candidates: list[tuple[float, int, int]] = []
    for a in tracklets.values():
        for b in tracklets.values():
            gap = b.first_t - a.last_t
            if a is b or b.first_pos <= a.last_pos or gap > STITCH_MAX_GAP_SEC:
                continue
            mpp = a.metres_per_px or b.metres_per_px
            if not mpp:
                continue
            dist_m = (
                math.hypot(b.first_ref[0] - a.last_ref[0], b.first_ref[1] - a.last_ref[1]) * mpp
            )
            if dist_m > max_speed * gap + 1.5:
                continue
            if a.colour is not None and b.colour is not None:
                if float(np.linalg.norm(a.colour - b.colour)) > STITCH_MAX_COLOUR_DIST:
                    continue
            candidates.append((dist_m + gap, a.track_id, b.track_id))

    root: dict[int, int] = {tid: tid for tid in tracklets}
    chain_end: dict[int, int] = {tid: tid for tid in tracklets}  # root -> last tracklet id
    has_successor: set[int] = set()
    has_predecessor: set[int] = set()
    for _, a_id, b_id in sorted(candidates):
        if a_id in has_successor or b_id in has_predecessor:
            continue
        ra = root[a_id]
        if chain_end[ra] != a_id or root[b_id] == ra:
            continue
        # Fold b's whole chain into a's.
        rb = root[b_id]
        for tid, r in root.items():
            if r == rb:
                root[tid] = ra
        chain_end[ra] = chain_end[rb]
        has_successor.add(a_id)
        has_predecessor.add(b_id)

    for frame in frames:
        for obs in frame.players:
            obs.track_id = root.get(obs.track_id, obs.track_id)
    for old, new in root.items():
        if old != new and old in colours:
            colours.setdefault(new, []).extend(colours.pop(old))
    return root


def _renumber_tracks(frames: list[_Frame], colours: dict[int, list[np.ndarray]]) -> None:
    """Relabel tracks 1..N by first appearance, so viewers never see "#316"."""
    mapping: dict[int, int] = {}
    for frame in frames:
        for obs in frame.players:
            if obs.track_id not in mapping:
                mapping[obs.track_id] = len(mapping) + 1
            obs.track_id = mapping[obs.track_id]
    renamed = {mapping[k]: v for k, v in colours.items() if k in mapping}
    colours.clear()
    colours.update(renamed)


# ---------------------------------------------------------------------------
# Kinematics
# ---------------------------------------------------------------------------


def _smooth(values: np.ndarray, window: int) -> np.ndarray:
    if len(values) < window or window < 2:
        return values
    pad = window // 2
    kernel = np.ones(window) / window
    return np.convolve(np.pad(values, pad, mode="edge"), kernel, mode="valid")[: len(values)]


def _kinematics(
    frames: list[_Frame], team_of: dict[int, str], sport: str
) -> tuple[dict[int, dict[str, Any]], dict[tuple[int, int], float]]:
    """Distance and speed per player in stabilised, height-scaled metres.

    Positions are smoothed, and speed at each sample is the distance covered
    over the last ``SPEED_WINDOW_SEC`` divided by that time — not the jump
    between two frames, which turns box jitter into fake sprints.
    """
    height_m = PLAYER_HEIGHT_M.get(sport, 1.8)
    max_speed = MAX_PLAUSIBLE_SPEED_MPS.get(sport, 11.0)
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
        ts = np.array([s[1] for s in samples])
        xs = _smooth(np.array([s[2] for s in samples]), 5)
        ys = _smooth(np.array([s[3] for s in samples]), 5)
        mpp = float(np.median([s[4] for s in samples]))

        distance = 0.0
        for k in range(1, len(samples)):
            dt = ts[k] - ts[k - 1]
            if dt <= 0:
                continue
            step = math.hypot(xs[k] - xs[k - 1], ys[k] - ys[k - 1]) * mpp
            if step / dt <= max_speed:
                distance += step

        # Only trust windows that span the full SPEED_WINDOW_SEC and sit clear
        # of the track's ends, where edge-padded smoothing ramps the position
        # in: a short or edge window averages an impossible burst down into a
        # plausible-looking number instead of exposing it.
        edge = 2 if len(samples) > 6 else 0
        top = 0.0
        j = 0
        for k in range(1, len(samples)):
            while j < k - 1 and ts[k] - ts[j + 1] >= SPEED_WINDOW_SEC:
                j += 1
            dt = ts[k] - ts[j]
            if dt <= 0:
                continue
            full_window = dt >= SPEED_WINDOW_SEC * 0.8 or ts[-1] - ts[0] < SPEED_WINDOW_SEC
            if not full_window or j < edge or k > len(samples) - 1 - edge:
                continue
            speed = math.hypot(xs[k] - xs[j], ys[k] - ys[j]) * mpp / dt
            if speed > max_speed:
                continue  # an ID switch or bad box, not a player
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
