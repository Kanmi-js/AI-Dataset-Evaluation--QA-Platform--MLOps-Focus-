"""Turn raw tracks into tactical events.

Input is the ``tracks`` payload from ``app.workers.tracking``; output is a list
of events (possession spells, passes, turnovers, sprints) with the context a
pundit would talk about — pressure on the ball, each team's shape — plus a
short list of *key moments* worth freezing the clip on.

Everything here is a heuristic over detections, not ground truth: a pass is
"the ball's nearest player changed and both players wear the same kit". Every
event carries a ``confidence`` so the script and the UI can hedge accordingly.
Pure functions, no I/O, so the logic is unit-testable with hand-built tracks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Tuning per sport. Distances in metres, speeds in m/s, times in seconds.
_SPORT_RULES: dict[str, dict[str, float]] = {
    "football": {
        "control_reach": 0.45,  # ball within this × player height of the box
        "sprint_speed": 7.0,
        "pressure_radius": 4.0,
        "long_pass": 18.0,
    },
    "basketball": {
        "control_reach": 0.35,
        "sprint_speed": 5.5,
        "pressure_radius": 1.8,
        "long_pass": 9.0,
    },
}

# Frames a player must keep the ball before a possession counts. Filters the
# ball passing *near* someone from that player actually controlling it.
MIN_CONTROL_FRAMES = 2
# Same player regaining the ball after this gap is the same spell.
MERGE_GAP_SEC = 1.0
# A loose ball for longer than this is not a pass any more.
MAX_PASS_FLIGHT_SEC = 3.5
MIN_SPRINT_SAMPLES = 3
MAX_KEY_MOMENTS = 4
MIN_KEY_MOMENT_SPACING_SEC = 2.5


@dataclass
class _Spell:
    player: int
    team: str
    start_pos: int
    end_pos: int
    start_t: float
    end_t: float


def _rules(sport: str) -> dict[str, float]:
    return _SPORT_RULES.get(sport, _SPORT_RULES["football"])


def _to_ref(frame: dict[str, Any], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = frame["to_ref"]
    return a * x + b * y + c, d * x + e * y + f


def _ref_scale(frame: dict[str, Any]) -> float:
    a, b, _, d, e, _ = frame["to_ref"]
    return math.sqrt(abs(a * e - b * d)) or 1.0


def _foot(box: list[float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, box[3]


def _box_distance(box: list[float], x: float, y: float) -> float:
    """Distance from a point to a box (0 when inside)."""
    dx = max(box[0] - x, 0.0, x - box[2])
    dy = max(box[1] - y, 0.0, y - box[3])
    return math.hypot(dx, dy)


def _mpp(tracks: dict[str, Any], player_id: int) -> float | None:
    stats = tracks["players"].get(str(player_id))
    return stats["metres_per_ref_px"] if stats else None


def _ref_distance_m(
    tracks: dict[str, Any],
    frame_a: dict[str, Any],
    pa: tuple[float, float],
    frame_b: dict[str, Any],
    pb: tuple[float, float],
    mpp: float,
) -> float:
    ax, ay = _to_ref(frame_a, *pa)
    bx, by = _to_ref(frame_b, *pb)
    return math.hypot(ax - bx, ay - by) * mpp


# ---------------------------------------------------------------------------
# Possession
# ---------------------------------------------------------------------------


def _controller_per_frame(tracks: dict[str, Any]) -> list[int | None]:
    reach = _rules(tracks["sport"])["control_reach"]
    out: list[int | None] = []
    for frame in tracks["frames"]:
        ball = frame["ball"]
        if not ball:
            out.append(None)
            continue
        bx, by, _ = ball
        best: tuple[float, int] | None = None
        for p in frame["players"]:
            box = p["box"]
            height = box[3] - box[1]
            if height <= 0:
                continue
            norm = _box_distance(box, bx, by) / height
            if norm <= reach and (best is None or norm < best[0]):
                best = (norm, p["id"])
        out.append(best[1] if best else None)
    return out


def _spells(tracks: dict[str, Any]) -> list[_Spell]:
    frames = tracks["frames"]
    controller = _controller_per_frame(tracks)
    team_of = {int(k): v["team"] for k, v in tracks["players"].items()}

    raw: list[_Spell] = []
    run_player: int | None = None
    run_start = 0
    for pos, player in enumerate([*controller, None]):
        if player == run_player:
            continue
        if run_player is not None and pos - run_start >= MIN_CONTROL_FRAMES:
            raw.append(
                _Spell(
                    player=run_player,
                    team=team_of.get(run_player, "other"),
                    start_pos=run_start,
                    end_pos=pos - 1,
                    start_t=frames[run_start]["t"],
                    end_t=frames[pos - 1]["t"],
                )
            )
        run_player, run_start = player, pos

    merged: list[_Spell] = []
    for spell in raw:
        last = merged[-1] if merged else None
        if last and last.player == spell.player and spell.start_t - last.end_t <= MERGE_GAP_SEC:
            last.end_pos, last.end_t = spell.end_pos, spell.end_t
        else:
            merged.append(spell)
    return merged


# ---------------------------------------------------------------------------
# Context: pressure and shape
# ---------------------------------------------------------------------------


def _player_in(frame: dict[str, Any], player_id: int) -> dict[str, Any] | None:
    return next((p for p in frame["players"] if p["id"] == player_id), None)


def _pressure(tracks: dict[str, Any], frame: dict[str, Any], carrier: int) -> list[int]:
    """Opponents within the pressure radius of the ball carrier."""
    me = _player_in(frame, carrier)
    mpp = _mpp(tracks, carrier)
    if me is None or mpp is None:
        return []
    my_team = me["team"]
    radius = _rules(tracks["sport"])["pressure_radius"]
    fx, fy = _foot(me["box"])
    near = []
    for p in frame["players"]:
        if p["id"] == carrier or p["team"] in (my_team, "other"):
            continue
        d = _ref_distance_m(tracks, frame, (fx, fy), frame, _foot(p["box"]), mpp)
        if d <= radius:
            near.append(p["id"])
    return near


def team_shape(tracks: dict[str, Any], frame: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Width and depth of each team's visible players, in estimated metres.

    "Width" is the spread along the frame's horizontal axis, "depth" along the
    vertical — on a standard side-on broadcast angle that is touchline-to-
    touchline and goal-to-goal respectively. Only visible players count, so on
    a tight camera this describes the local shape, not the whole team.
    """
    shape: dict[str, dict[str, float]] = {}
    scale = _ref_scale(frame)
    for team in ("A", "B"):
        members = [p for p in frame["players"] if p["team"] == team]
        if len(members) < 3:
            continue
        mpps = [m for p in members if (m := _mpp(tracks, p["id"])) is not None]
        if not mpps:
            continue
        mpp = sorted(mpps)[len(mpps) // 2] * scale  # metres per *frame* pixel
        xs = [_foot(p["box"])[0] for p in members]
        ys = [_foot(p["box"])[1] for p in members]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        spread = sum(math.hypot(x - cx, y - cy) for x, y in zip(xs, ys, strict=True)) / len(xs)
        shape[team] = {
            "visible_players": len(members),
            "width_m": round((max(xs) - min(xs)) * mpp, 1),
            "depth_m": round((max(ys) - min(ys)) * mpp, 1),
            "compactness_m": round(spread * mpp, 1),
        }
    return shape


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _event(
    events: list[dict[str, Any]],
    *,
    kind: str,
    t: float,
    t_end: float,
    team: str | None,
    players: list[int],
    frame_index: int,
    confidence: float,
    detail: dict[str, Any],
    description: str,
) -> None:
    events.append(
        {
            "id": f"e{len(events) + 1}",
            "type": kind,
            "t": round(t, 2),
            "t_end": round(t_end, 2),
            "frame_index": frame_index,
            "team": team,
            "players": players,
            "confidence": round(confidence, 2),
            "detail": detail,
            "description": description,
        }
    )


def _team_label(tracks: dict[str, Any], team: str | None) -> str:
    if team in tracks["teams"]:
        info = tracks["teams"][team]
        return f"{info['label']} ({info['colour_name']})"
    return "an unassigned player"


def detect_events(tracks: dict[str, Any]) -> dict[str, Any]:
    """Possession, passes, turnovers, sprints and the key moments among them."""
    frames = tracks["frames"]
    rules = _rules(tracks["sport"])
    spells = _spells(tracks)
    events: list[dict[str, Any]] = []

    # Ball-carrying spells and transitions between them.
    for prev, nxt in zip(spells, spells[1:], strict=False):
        flight = nxt.start_t - prev.end_t
        if flight > MAX_PASS_FLIGHT_SEC or prev.player == nxt.player:
            continue
        f_from, f_to = frames[prev.end_pos], frames[nxt.start_pos]
        p_from = _player_in(f_from, prev.player)
        p_to = _player_in(f_to, nxt.player)
        mpp = _mpp(tracks, prev.player) or _mpp(tracks, nxt.player)
        distance = (
            _ref_distance_m(tracks, f_from, _foot(p_from["box"]), f_to, _foot(p_to["box"]), mpp)
            if p_from and p_to and mpp
            else None
        )
        pressure = _pressure(tracks, f_from, prev.player)
        detail = {
            "from_player": prev.player,
            "to_player": nxt.player,
            "flight_sec": round(flight, 2),
            "distance_m": round(distance, 1) if distance is not None else None,
            "pressure_on_passer": pressure,
            "shape": team_shape(tracks, f_from),
        }
        same_team = prev.team == nxt.team and prev.team != "other"
        if same_team:
            long = distance is not None and distance >= rules["long_pass"]
            _event(
                events,
                kind="long_pass" if long else "pass",
                t=prev.end_t,
                t_end=nxt.start_t,
                team=prev.team,
                players=[prev.player, nxt.player],
                frame_index=f_from["i"],
                confidence=0.7 if flight < 2.0 else 0.5,
                detail=detail,
                description=(
                    f"{_team_label(tracks, prev.team)}: track {prev.player} finds "
                    f"track {nxt.player}"
                    + (f" over ≈{distance:.0f} m" if distance is not None else "")
                    + (f" with {len(pressure)} opponent(s) closing" if pressure else "")
                    + "."
                ),
            )
        elif prev.team != "other" and nxt.team != "other":
            _event(
                events,
                kind="turnover",
                t=prev.end_t,
                t_end=nxt.start_t,
                team=nxt.team,
                players=[prev.player, nxt.player],
                frame_index=f_to["i"],
                confidence=0.6,
                detail=detail,
                description=(
                    f"Turnover: {_team_label(tracks, nxt.team)} win it back — track "
                    f"{nxt.player} takes it off track {prev.player}."
                ),
            )

    # Sprints: sustained runs above the sport's sprint threshold.
    by_player: dict[int, list[tuple[int, float, float]]] = {}
    for pos, frame in enumerate(frames):
        for p in frame["players"]:
            by_player.setdefault(p["id"], []).append((pos, frame["t"], p["speed_mps"]))
    for pid, samples in by_player.items():
        for run in _fast_runs(samples, rules["sprint_speed"]):
            if len(run) < MIN_SPRINT_SAMPLES:
                continue
            team = tracks["players"][str(pid)]["team"]
            peak = max(s[2] for s in run)
            _event(
                events,
                kind="sprint",
                t=run[0][1],
                t_end=run[-1][1],
                team=team,
                players=[pid],
                frame_index=frames[run[0][0]]["i"],
                confidence=0.6,
                detail={
                    "peak_speed_mps": round(peak, 1),
                    "duration_sec": round(run[-1][1] - run[0][1], 2),
                },
                description=(
                    f"{_team_label(tracks, team)}: track {pid} sprints, peaking "
                    f"≈{peak:.1f} m/s."
                ),
            )

    events.sort(key=lambda e: e["t"])
    for k, event in enumerate(events, start=1):
        event["id"] = f"e{k}"

    return {
        "possession": _possession_share(spells),
        "spells": [
            {
                "player": s.player,
                "team": s.team,
                "start_t": round(s.start_t, 2),
                "end_t": round(s.end_t, 2),
            }
            for s in spells
        ],
        "events": events,
        "key_moments": _key_moments(events),
    }


def _fast_runs(
    samples: list[tuple[int, float, float]], threshold: float
) -> list[list[tuple[int, float, float]]]:
    """Group (frame position, t, speed) samples into consecutive fast runs."""
    runs: list[list[tuple[int, float, float]]] = []
    current: list[tuple[int, float, float]] = []
    for sample in samples:
        contiguous = bool(current) and sample[0] == current[-1][0] + 1
        if sample[2] >= threshold and (contiguous or not current):
            current.append(sample)
            continue
        if current:
            runs.append(current)
        current = [sample] if sample[2] >= threshold else []
    if current:
        runs.append(current)
    return runs


def _possession_share(spells: list[_Spell]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for s in spells:
        if s.team == "other":
            continue
        totals[s.team] = totals.get(s.team, 0.0) + max(s.end_t - s.start_t, 0.1)
    whole = sum(totals.values())
    return {k: round(v / whole * 100, 1) for k, v in totals.items()} if whole else {}


_WEIGHT = {"turnover": 3.0, "long_pass": 2.5, "pass": 1.2, "sprint": 1.5}


def _key_moments(events: list[dict[str, Any]]) -> list[str]:
    """Pick the few events most worth freezing the clip on, in time order."""

    def score(event: dict[str, Any]) -> float:
        base = _WEIGHT.get(event["type"], 1.0) * event["confidence"]
        pressure = event["detail"].get("pressure_on_passer") or []
        return base + 0.4 * len(pressure)

    chosen: list[dict[str, Any]] = []
    for event in sorted(events, key=score, reverse=True):
        if all(abs(event["t"] - c["t"]) >= MIN_KEY_MOMENT_SPACING_SEC for c in chosen):
            chosen.append(event)
        if len(chosen) == MAX_KEY_MOMENTS:
            break
    return [e["id"] for e in sorted(chosen, key=lambda e: e["t"])]
