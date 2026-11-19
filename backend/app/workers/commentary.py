"""Write the voice-over script for a tactical breakdown.

The script is a short list of *segments*. Each one is anchored to a moment in
the clip, where the rendered video freezes, draws on the play (rings around
players, arrows for passes and runs) and holds while the segment is read out.

Claude writes the script in the voice of a former pro breaking a play down on a
podcast, from the events ``app.workers.events`` detected. The prompt confines it
to those events: no invented names, scores or competitions. Without an API key,
or if the call fails, a template script built from the same events is used, so
a breakdown is always produced.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

SOURCE_TEMPLATE = "template"

MAX_SEGMENTS = 6
MAX_WORDS_PER_SEGMENT = 60

SYSTEM_PROMPT = """\
You are the voice of TerrainAI's tactical breakdowns: a former professional \
player turned analyst, talking a listener through a play the way ex-pros do on \
a sports podcast. Conversational, confident, specific, a little colour — "watch \
this", "here's the key", "look at the space he's left" — but never shouting.

You are given what a computer-vision pipeline detected in one clip: teams \
(identified only by kit colour), players (identified only by tracking number), \
events with timestamps, and measured context such as pressure on the ball and \
team width and depth.

Hard rules:
- Every factual claim must come from the data you are given. Do not invent \
player names, jersey numbers, clubs, scores, competitions or what happened \
before or after the clip.
- Refer to players by kit colour and what they do ("the player in white we've \
ringed", "the receiver on the left"), not by tracking number alone.
- Speeds and distances are estimates; round them and use "about" / "around".
- Events with confidence below 0.6 should be described with some hedging.
- Each segment is read aloud while the video is frozen, so keep each one to \
one to three sentences and at most {max_words} words.

Structure: an optional short intro at 0 seconds that sets the scene, then one \
segment per key moment in time order (use the moment's event id and its \
timestamp), then an optional short outro on the overall pattern. At most \
{max_segments} segments in total.

For each segment choose what to draw on the frozen frame: `rings` for the \
players the listener should watch, and `arrows` for passes or runs between two \
players. Only use player ids that appear in the event data."""


def _segment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["intro", "moment", "outro"]},
            "event_id": {
                "type": "string",
                "description": "Id of the event this segment explains, or empty for intro/outro.",
            },
            "at_sec": {"type": "number", "description": "Clip time to freeze on."},
            "text": {"type": "string"},
            "rings": {"type": "array", "items": {"type": "integer"}},
            "arrows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from_player": {"type": "integer"},
                        "to_player": {"type": "integer"},
                    },
                    "required": ["from_player", "to_player"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["kind", "event_id", "at_sec", "text", "rings", "arrows"],
        "additionalProperties": False,
    }


SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short headline for the breakdown."},
        "segments": {"type": "array", "items": _segment_schema()},
    },
    "required": ["title", "segments"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Prompt input
# ---------------------------------------------------------------------------


def _brief(tracks: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """The facts Claude is allowed to talk about — nothing else is sent."""
    events = {e["id"]: e for e in analysis["events"]}
    return {
        "sport": tracks["sport"],
        "clip_seconds_analysed": tracks["analysed_until_sec"],
        "teams": {
            key: {
                "label": t["label"],
                "kit_colour": t["colour_name"],
                "tracked_players": len(t["players"]),
            }
            for key, t in tracks["teams"].items()
        },
        "possession_share_percent": analysis["possession"],
        "key_moment_ids": analysis["key_moments"],
        "events": [
            {
                "id": e["id"],
                "type": e["type"],
                "t": e["t"],
                "t_end": e["t_end"],
                "team": e["team"],
                "players": e["players"],
                "confidence": e["confidence"],
                "summary": e["description"],
                "detail": e["detail"],
            }
            for e in events.values()
        ][:40],
        "fastest_players": sorted(
            (
                {
                    "player": int(pid),
                    "team": s["team"],
                    "top_speed_mps": s["top_speed_mps"],
                    "distance_m": s["distance_m"],
                }
                for pid, s in tracks["players"].items()
            ),
            key=lambda p: p["top_speed_mps"],
            reverse=True,
        )[:5],
        "ball_detection_rate": tracks["ball_detection_rate"],
    }


# ---------------------------------------------------------------------------
# Validation shared by both sources
# ---------------------------------------------------------------------------


def _clean(
    script: dict[str, Any], tracks: dict[str, Any], analysis: dict[str, Any]
) -> dict[str, Any]:
    """Clamp times, drop unknown players, cap length. Never trust the shape blindly."""
    known = {int(k) for k in tracks["players"]}
    end = float(tracks["analysed_until_sec"])
    events = {e["id"]: e for e in analysis["events"]}

    segments = []
    for seg in script.get("segments", [])[:MAX_SEGMENTS]:
        text = " ".join(str(seg.get("text", "")).split())
        if not text:
            continue
        words = text.split()
        if len(words) > MAX_WORDS_PER_SEGMENT:
            text = " ".join(words[:MAX_WORDS_PER_SEGMENT]).rstrip(",;") + "…"
        event = events.get(seg.get("event_id") or "")
        at = float(event["t"]) if event else float(seg.get("at_sec", 0.0))
        kind = seg.get("kind", "moment")
        if kind == "outro":
            at = end
        segments.append(
            {
                "kind": kind,
                "event_id": event["id"] if event else None,
                "at_sec": round(min(max(at, 0.0), end), 2),
                "text": text,
                "rings": [p for p in seg.get("rings", []) if p in known][:4],
                "arrows": [
                    {"from_player": a["from_player"], "to_player": a["to_player"]}
                    for a in seg.get("arrows", [])
                    if a.get("from_player") in known and a.get("to_player") in known
                ][:3],
            }
        )
    segments.sort(key=lambda s: s["at_sec"])
    return {"title": str(script.get("title") or "Tactical breakdown")[:120], "segments": segments}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _template_script(tracks: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    events = {e["id"]: e for e in analysis["events"]}
    teams = tracks["teams"]
    sport = tracks["sport"]
    segments: list[dict[str, Any]] = []

    if teams:
        kits = " against ".join(f"{t['colour_name']}" for t in teams.values())
        intro = f"Alright, let's break this {sport} clip down. It's {kits}."
        share = analysis["possession"]
        if share:
            leader = max(share, key=share.get)
            intro += (
                f" {teams[leader]['colour_name'].capitalize()} have the ball for about "
                f"{share[leader]:.0f} percent of what we can see."
            )
    else:
        intro = f"Let's break this {sport} clip down."
    segments.append(
        {"kind": "intro", "event_id": "", "at_sec": 0.0, "text": intro, "rings": [], "arrows": []}
    )

    for event_id in analysis["key_moments"]:
        e = events[event_id]
        players = e["players"]
        arrows = (
            [{"from_player": players[0], "to_player": players[1]}]
            if e["type"] in ("pass", "long_pass", "turnover") and len(players) == 2
            else []
        )
        lead = {
            "turnover": "Here's the moment it flips.",
            "long_pass": "Watch the range on this one.",
            "pass": "Look at this ball.",
            "sprint": "Now watch the run.",
        }.get(e["type"], "Watch this.")
        segments.append(
            {
                "kind": "moment",
                "event_id": event_id,
                "at_sec": e["t"],
                "text": f"{lead} {e['description']}",
                "rings": players,
                "arrows": arrows,
            }
        )

    if not analysis["key_moments"]:
        segments.append(
            {
                "kind": "outro",
                "event_id": "",
                "at_sec": tracks["analysed_until_sec"],
                "text": "Not much to pick apart here: the ball was hard to follow in this footage.",
                "rings": [],
                "arrows": [],
            }
        )
    return {"title": f"{sport.capitalize()} breakdown", "segments": segments}


def _claude_script(tracks: dict[str, Any], analysis: dict[str, Any]) -> tuple[dict[str, Any], str]:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.beta.messages.create(
        model=settings.commentary_model,
        max_tokens=16000,
        # A policy decline on the primary model is retried server-side on a
        # fallback model instead of returning no script.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SCRIPT_SCHEMA},
        },
        system=SYSTEM_PROMPT.format(max_words=MAX_WORDS_PER_SEGMENT, max_segments=MAX_SEGMENTS),
        messages=[
            {
                "role": "user",
                "content": (
                    "Here is what the pipeline detected in the clip. Write the "
                    "breakdown script.\n\n" + json.dumps(_brief(tracks, analysis), indent=1)
                ),
            }
        ],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"script generation stopped: {response.stop_reason}")
    text = "".join(block.text for block in response.content if block.type == "text")
    return json.loads(text), response.model


def write_script(tracks: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Return ``{"title", "segments", "source", "fallback_reason"}``."""
    reason: str | None = None
    if settings.anthropic_api_key:
        try:
            raw, model = _claude_script(tracks, analysis)
            script = _clean(raw, tracks, analysis)
            if script["segments"]:
                return {**script, "source": model, "fallback_reason": None}
            reason = "model returned no usable segments"
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the template
            logger.exception("Claude script generation failed")
            reason = f"{type(exc).__name__}: {exc}"[:300]
    else:
        reason = "ANTHROPIC_API_KEY not set"

    script = _clean(_template_script(tracks, analysis), tracks, analysis)
    return {**script, "source": SOURCE_TEMPLATE, "fallback_reason": reason}
