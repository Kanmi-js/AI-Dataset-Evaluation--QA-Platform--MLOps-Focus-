"""Render the tactical breakdown video.

The output is the original clip with broadcast-style overlays (team-coloured
markers at each player's feet, the ball and its trail, a marker over the ball
carrier), plus a **freeze** for every script segment: the clip pauses on that
moment, dims everything except the players being discussed, draws rings and
pass/run arrows, and holds while the segment is read out — the way pundits
pause the tape on a podcast. Captions reveal word by word in time with the
voice.

Frames are piped as raw BGR into ffmpeg, which encodes H.264 and muxes the
voice-over track in the same pass.
"""

from __future__ import annotations

import bisect
import math
import subprocess
import textwrap
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.workers.voiceover import SAMPLE_RATE, Speech

# Silence before the voice starts and after it ends, within each freeze.
LEAD_IN_SEC = 0.35
TAIL_SEC = 0.45
# How long the rings/arrows take to draw themselves on.
DRAW_ON_SEC = 0.6
BALL_TRAIL_SEC = 0.8

AMBER = (11, 158, 245)  # BGR of the dashboard's vector-amber
VOLT = (0, 255, 204)  # BGR of the dashboard's volt
NEUTRAL = (200, 200, 200)

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


# ---------------------------------------------------------------------------
# Track lookup with interpolation between analysed frames
# ---------------------------------------------------------------------------


class _TrackIndex:
    def __init__(self, tracks: dict[str, Any], analysis: dict[str, Any], scale: float):
        self.frames = tracks["frames"]
        self.indices = [f["i"] for f in self.frames]
        self.scale = scale
        self.fps = float(tracks["fps"])
        self.team_colour = {
            key: tuple(info["overlay_bgr"]) for key, info in tracks["teams"].items()
        }
        self.spells = analysis.get("spells", [])

    def colour(self, team: str) -> tuple[int, int, int]:
        return self.team_colour.get(team, NEUTRAL)  # type: ignore[return-value]

    def at(
        self, frame_index: int
    ) -> tuple[dict[int, dict[str, Any]], tuple[float, float, float] | None]:
        """Players (by id) and ball at a source frame, in output pixels."""
        pos = bisect.bisect_right(self.indices, frame_index) - 1
        if pos < 0:
            return {}, None
        a = self.frames[pos]
        b = self.frames[pos + 1] if pos + 1 < len(self.frames) else None
        span = (b["i"] - a["i"]) if b else 0
        t = (frame_index - a["i"]) / span if span else 0.0

        players: dict[int, dict[str, Any]] = {}
        b_players = {p["id"]: p for p in b["players"]} if b else {}
        for p in a["players"]:
            box = p["box"]
            other = b_players.get(p["id"])
            if other is not None:
                box = [x + (y - x) * t for x, y in zip(box, other["box"], strict=True)]
            players[p["id"]] = {**p, "box": [v * self.scale for v in box]}

        ball = a["ball"]
        if ball and b and b["ball"]:
            ball = [x + (y - x) * t for x, y in zip(ball, b["ball"], strict=True)]
        ball_out = tuple(v * self.scale for v in ball) if ball else None
        return players, ball_out  # type: ignore[return-value]

    def carrier(self, t: float) -> int | None:
        for spell in self.spells:
            if spell["start_t"] <= t <= spell["end_t"]:
                return spell["player"]
        return None

    def ball_trail(self, frame_index: int) -> list[tuple[int, int]]:
        window = int(BALL_TRAIL_SEC * self.fps)
        lo = bisect.bisect_left(self.indices, frame_index - window)
        hi = bisect.bisect_right(self.indices, frame_index)
        return [
            (int(f["ball"][0] * self.scale), int(f["ball"][1] * self.scale))
            for f in self.frames[lo:hi]
            if f["ball"]
        ]


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _foot(box: list[float]) -> tuple[int, int]:
    return int((box[0] + box[2]) / 2), int(box[3])


def _draw_live(
    img: np.ndarray,
    index: _TrackIndex,
    frame_index: int,
    carrier: int | None,
) -> None:
    players, ball = index.at(frame_index)
    for pid, p in players.items():
        box = p["box"]
        w = box[2] - box[0]
        fx, fy = _foot(box)
        colour = index.colour(p["team"])
        axes = (max(int(w * 0.55), 6), max(int(w * 0.18), 3))
        cv2.ellipse(img, (fx, fy), axes, 0, 0, 360, colour, 2, cv2.LINE_AA)
        if pid == carrier:
            top = int(box[1]) - 8
            tri = np.array([[fx - 7, top - 12], [fx + 7, top - 12], [fx, top]], dtype=np.int32)
            cv2.fillPoly(img, [tri], VOLT, cv2.LINE_AA)

    trail = index.ball_trail(frame_index)
    for k in range(1, len(trail)):
        alpha = k / len(trail)
        cv2.line(img, trail[k - 1], trail[k], AMBER, max(1, int(3 * alpha)), cv2.LINE_AA)
    if ball:
        cx, cy, r = ball
        cv2.circle(img, (int(cx), int(cy)), max(int(r) + 4, 7), AMBER, 2, cv2.LINE_AA)


def _arrow(
    img: np.ndarray, start: tuple[int, int], end: tuple[int, int], progress: float, colour
) -> None:
    if progress <= 0:
        return
    ex = int(start[0] + (end[0] - start[0]) * progress)
    ey = int(start[1] + (end[1] - start[1]) * progress)
    cv2.line(img, start, (ex, ey), (0, 0, 0), 7, cv2.LINE_AA)
    cv2.arrowedLine(img, start, (ex, ey), colour, 4, cv2.LINE_AA, tipLength=0.0)
    if progress >= 1.0:
        length = math.hypot(end[0] - start[0], end[1] - start[1]) or 1.0
        tip = min(0.25, 26.0 / length)
        cv2.arrowedLine(img, start, end, colour, 4, cv2.LINE_AA, tipLength=tip)


def _draw_freeze(
    base: np.ndarray,
    index: _TrackIndex,
    frame_index: int,
    segment: dict[str, Any],
    elapsed: float,
) -> np.ndarray:
    """Telestrated still: spotlight, rings and arrows, animated by ``elapsed``."""
    players, _ = index.at(frame_index)
    focus = [pid for pid in segment["rings"] if pid in players]
    for arrow in segment["arrows"]:
        focus.extend(pid for pid in (arrow["from_player"], arrow["to_player"]) if pid in players)

    img = base.copy()
    # Dim everything, then restore brightness around the players in focus.
    dimmed = (img * 0.45).astype(np.uint8)
    if focus:
        mask = np.zeros(img.shape[:2], dtype=np.uint8)
        for pid in set(focus):
            box = players[pid]["box"]
            cx, cy = int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2)
            radius = int((box[3] - box[1]) * 0.9)
            cv2.circle(mask, (cx, cy), radius, 255, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=12)
        alpha = (mask.astype(np.float32) / 255.0)[..., None]
        img = (img * alpha + dimmed * (1 - alpha)).astype(np.uint8)
    else:
        img = (img * 0.75).astype(np.uint8)

    progress = min(elapsed / DRAW_ON_SEC, 1.0)
    pulse = 1.0 + 0.08 * math.sin(elapsed * 2 * math.pi / 1.2)
    for pid in segment["rings"]:
        if pid not in players:
            continue
        box = players[pid]["box"]
        fx, fy = _foot(box)
        w = box[2] - box[0]
        axes = (int(max(w * 0.8, 14) * progress * pulse), int(max(w * 0.28, 6) * progress * pulse))
        if axes[0] > 0 and axes[1] > 0:
            colour = index.colour(players[pid]["team"])
            cv2.ellipse(img, (fx, fy), axes, 0, 0, 360, (0, 0, 0), 6, cv2.LINE_AA)
            cv2.ellipse(img, (fx, fy), axes, 0, 0, 360, colour, 3, cv2.LINE_AA)

    for arrow in segment["arrows"]:
        a, b = players.get(arrow["from_player"]), players.get(arrow["to_player"])
        if a and b:
            _arrow(img, _foot(a["box"]), _foot(b["box"]), progress, VOLT)

    return img


def _caption(
    img: np.ndarray,
    text: str,
    reveal: float,
    *,
    title: str | None = None,
) -> np.ndarray:
    """Lower-third caption box, revealing ``reveal`` (0-1) of the words."""
    h, w = img.shape[:2]
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil, "RGBA")

    words = text.split()
    shown = " ".join(words[: max(1, math.ceil(len(words) * reveal))]) if words else ""
    size = max(int(h * 0.036), 14)
    font = _font(size)
    chars = max(int(w * 0.85 / (size * 0.55)), 20)
    lines = textwrap.wrap(shown, chars)[-3:]

    pad = int(size * 0.7)
    line_h = int(size * 1.3)
    box_h = pad * 2 + line_h * max(len(lines), 1)
    top = h - box_h - int(h * 0.05)
    draw.rounded_rectangle(
        [int(w * 0.05), top, int(w * 0.95), top + box_h], radius=12, fill=(10, 12, 14, 215)
    )
    draw.rectangle([int(w * 0.05), top, int(w * 0.05) + 6, top + box_h], fill=(204, 255, 0, 255))
    for k, line in enumerate(lines):
        draw.text(
            (int(w * 0.05) + pad + 6, top + pad + k * line_h),
            line,
            font=font,
            fill=(245, 245, 245, 255),
        )

    if title:
        tfont = _font(max(int(h * 0.06), 20))
        bbox = draw.textbbox((0, 0), title, font=tfont)
        tx = (w - (bbox[2] - bbox[0])) // 2
        draw.text((tx, int(h * 0.3)), title, font=tfont, fill=(204, 255, 0, 255))

    # Pause tag. The bars are drawn rather than typed: DejaVu has no ⏸ glyph.
    tag = max(int(h * 0.026), 11)
    x0, y0 = int(w * 0.03), int(h * 0.03)
    bar_w = max(tag // 4, 2)
    for dx in (0, bar_w * 2):
        draw.rectangle([x0 + dx, y0 + 2, x0 + dx + bar_w, y0 + tag], fill=(204, 255, 0, 235))
    draw.text((x0 + bar_w * 4 + 4, y0), "TACTICAL PAUSE", font=_font(tag), fill=(204, 255, 0, 235))
    return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def _badge(img: np.ndarray, teams: dict[str, Any]) -> None:
    """Small persistent corner badge with the team key."""
    h, w = img.shape[:2]
    size = max(h / 900, 0.4)
    x = int(w * 0.02)
    line = int(20 * size * 2)
    # Dark backing panel so the key stays legible on bright stands or walls.
    panel_w = int(w * 0.26)
    panel_h = int(h * 0.05) + line * (len(teams) + 1) - line // 2
    region = img[int(h * 0.01) : int(h * 0.01) + panel_h, x // 2 : x // 2 + panel_w]
    region[:] = (region * 0.35).astype(np.uint8)
    cv2.putText(
        img, "TERRAIN AI", (x, int(h * 0.05)), cv2.FONT_HERSHEY_DUPLEX, size, VOLT, 1, cv2.LINE_AA
    )
    y = int(h * 0.05) + line
    for info in teams.values():
        cv2.circle(img, (x + 6, y - 5), 6, tuple(info["overlay_bgr"]), -1, cv2.LINE_AA)
        label = f"{info['label']} ({info['colour_name']})"
        cv2.putText(
            img,
            label,
            (x + 18, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            size * 0.8,
            (240, 240, 240),
            1,
            cv2.LINE_AA,
        )
        y += line


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


@dataclass
class _Freeze:
    segment_pos: int
    source_frame: int
    frames: int
    voice_start_out: int  # output frame at which the voice starts


def _schedule(
    segments: list[dict[str, Any]], speech: list[Speech], fps: float, last_source: int
) -> tuple[list[_Freeze], int]:
    """Place one freeze per segment. Returns ``(freezes, total output frames)``.

    The renderer writes live frame ``s`` and then every freeze anchored at
    ``s``, so a freeze starts after ``s + 1`` live frames plus all earlier
    freezes.
    """
    freezes: list[_Freeze] = []
    frozen_so_far = 0
    pending = sorted(
        (min(max(round(s["at_sec"] * fps), 0), last_source), k) for k, s in enumerate(segments)
    )
    for source_frame, k in pending:
        n = math.ceil((LEAD_IN_SEC + speech[k].duration_sec + TAIL_SEC) * fps)
        start = source_frame + 1 + frozen_so_far
        freezes.append(_Freeze(k, source_frame, n, start + math.ceil(LEAD_IN_SEC * fps)))
        frozen_so_far += n
    return freezes, last_source + 1 + frozen_so_far


def _write_audio(
    path: Path, freezes: list[_Freeze], speech: list[Speech], fps: float, total_frames: int
) -> None:
    total = int(total_frames / fps * SAMPLE_RATE) + SAMPLE_RATE
    mix = np.zeros(total, dtype=np.int32)
    for fz in freezes:
        pcm = speech[fz.segment_pos].pcm
        if pcm is None:
            continue
        start = int(fz.voice_start_out / fps * SAMPLE_RATE)
        end = min(start + len(pcm), total)
        mix[start:end] += pcm[: end - start]
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(np.clip(mix, -32768, 32767).astype(np.int16).tobytes())


def _even(n: float) -> int:
    return max(int(n) // 2 * 2, 2)


def render_breakdown(
    video_path: str | Path,
    out_path: str | Path,
    *,
    tracks: dict[str, Any],
    analysis: dict[str, Any],
    script: dict[str, Any],
    speech: list[Speech],
    max_dim: int,
) -> dict[str, Any]:
    """Write the breakdown MP4 to ``out_path``. Returns timing metadata."""
    fps = float(tracks["fps"]) or 25.0
    src_w, src_h = int(tracks["frame_width"]), int(tracks["frame_height"])
    scale = min(1.0, max_dim / max(src_w, src_h))
    out_w, out_h = _even(src_w * scale), _even(src_h * scale)
    scale_x = out_w / src_w

    segments = script["segments"]
    last_source = int(round(tracks["analysed_until_sec"] * fps))
    freezes, total_frames = _schedule(segments, speech, fps, last_source)
    by_source: dict[int, list[_Freeze]] = {}
    for fz in freezes:
        by_source.setdefault(fz.source_frame, []).append(fz)

    index = _TrackIndex(tracks, analysis, scale_x)
    audio_path = Path(out_path).with_suffix(".wav")
    _write_audio(audio_path, freezes, speech, fps, total_frames)

    encoder = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{out_w}x{out_h}",
            "-r",
            f"{fps}",
            "-i",
            "pipe:0",
            "-i",
            str(audio_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert encoder.stdin is not None

    capture = cv2.VideoCapture(str(video_path))
    written = 0
    timeline: list[dict[str, Any]] = []
    last_image: np.ndarray | None = None
    try:
        for frame_index in range(last_source + 1):
            ok, image = capture.read()
            if not ok:
                if last_image is None:
                    raise ValueError("Could not read the clip for rendering")
                image = last_image  # pad short reads with the last good frame
            else:
                image = cv2.resize(image, (out_w, out_h), interpolation=cv2.INTER_AREA)
                last_image = image
            t = frame_index / fps

            live = image.copy()
            _draw_live(live, index, frame_index, index.carrier(t))
            _badge(live, tracks["teams"])
            encoder.stdin.write(live.tobytes())
            written += 1

            for fz in by_source.get(frame_index, []):
                segment = segments[fz.segment_pos]
                voice = speech[fz.segment_pos].duration_sec
                timeline.append(
                    {
                        "segment": fz.segment_pos,
                        "source_sec": round(t, 2),
                        "output_start_sec": round(written / fps, 2),
                        "output_end_sec": round((written + fz.frames) / fps, 2),
                    }
                )
                title = script["title"] if segment["kind"] == "intro" else None
                for k in range(fz.frames):
                    elapsed = k / fps
                    still = _draw_freeze(image, index, frame_index, segment, elapsed)
                    reveal = min(max((elapsed - LEAD_IN_SEC) / max(voice, 0.1), 0.0), 1.0)
                    still = _caption(still, segment["text"], reveal, title=title)
                    encoder.stdin.write(still.tobytes())
                    written += 1
    finally:
        capture.release()
        encoder.stdin.close()
        stderr = encoder.stderr.read().decode(errors="replace") if encoder.stderr else ""
        code = encoder.wait()
        audio_path.unlink(missing_ok=True)
    if code != 0:
        raise RuntimeError(f"ffmpeg failed ({code}): {stderr[-500:]}")

    return {
        "width": out_w,
        "height": out_h,
        "fps": fps,
        "duration_sec": round(written / fps, 2),
        "frames": written,
        "timeline": timeline,
    }
