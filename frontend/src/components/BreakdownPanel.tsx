import React, { useMemo, useRef, useState } from "react";
import { AlertTriangle, Clapperboard, Mic, MicOff, RefreshCw, Sparkles } from "lucide-react";
import type { Breakdown, TacticalAnalysis, VideoTelemetry } from "../api/types";

// Pure view over the breakdown the worker persisted. The page owns fetching
// and polling; this component only renders and reports clicks.

const STAGE_LABEL: Record<string, string> = {
  script: "Writing the commentary script…",
  voice: "Recording the voice-over…",
  render: "Rendering the tactical clip…",
};

const EVENT_LABEL: Record<string, string> = {
  pass: "Pass",
  long_pass: "Long pass",
  turnover: "Turnover",
  sprint: "Sprint",
};

const formatTime = (sec: number) => {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
};

export interface BreakdownPanelProps {
  breakdown?: Breakdown;
  videoUrl: string | null;
  analysis?: TacticalAnalysis;
  telemetry?: VideoTelemetry;
  /** True while the item itself is still being tracked. */
  itemInFlight: boolean;
  regenerating: boolean;
  onRegenerate: () => void;
}

export const BreakdownPanel: React.FC<BreakdownPanelProps> = ({
  breakdown,
  videoUrl,
  analysis,
  telemetry,
  itemInFlight,
  regenerating,
  onRegenerate,
}) => {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [currentTime, setCurrentTime] = useState(0);

  const teams = telemetry?.teams ?? {};
  const status = breakdown?.status;
  const busy = status === "queued" || status === "running" || regenerating;
  const canRegenerate = Boolean(analysis) && !busy && !itemInFlight;

  // Map each script segment to where its freeze sits in the rendered clip.
  const segments = useMemo(() => {
    const timeline = breakdown?.render?.timeline ?? [];
    return (breakdown?.segments ?? []).map((segment, index) => {
      const slot = timeline.find((t) => t.segment === index);
      return { ...segment, start: slot?.output_start_sec ?? null, end: slot?.output_end_sec ?? null };
    });
  }, [breakdown]);

  const activeIndex = segments.findIndex(
    (s) => s.start !== null && s.end !== null && currentTime >= s.start && currentTime < s.end,
  );

  const seek = (sec: number | null) => {
    if (sec === null || !videoRef.current) return;
    videoRef.current.currentTime = sec;
    void videoRef.current.play();
  };

  const header = (
    <div className="flex items-center justify-between border-b border-pitch-border px-4 py-3">
      <div className="flex items-center gap-2">
        <Clapperboard className="h-4 w-4 text-volt" />
        <h2 className="text-sm font-semibold tracking-wide text-white">
          {breakdown?.title ?? "Tactical breakdown"}
        </h2>
      </div>
      <button
        type="button"
        onClick={onRegenerate}
        disabled={!canRegenerate}
        title={analysis ? "Write, voice and render a fresh breakdown" : "Needs real tracking data"}
        className="flex cursor-pointer items-center gap-1.5 rounded border border-pitch-border bg-pitch-dark px-3 py-1.5 font-mono text-xs text-zinc-300 transition-colors hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
      >
        <RefreshCw className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
        <span>{status === "ready" ? "Regenerate" : "Generate"}</span>
      </button>
    </div>
  );

  let body: React.ReactNode;
  if (itemInFlight) {
    body = <Note>Tracking players and the ball. The breakdown starts as soon as that finishes.</Note>;
  } else if (!analysis) {
    body = (
      <Note>
        This clip only has simulated telemetry, so there is no real play to break down. Reprocess it
        with the YOLO tracker enabled (<code>TRACKER=yolo</code>).
      </Note>
    );
  } else if (busy) {
    body = (
      <div className="flex items-center gap-3 p-6 font-mono text-xs text-zinc-400">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-volt border-t-transparent" />
        <span>
          {status === "running" && breakdown?.stage ? STAGE_LABEL[breakdown.stage] : "Queued…"}
        </span>
      </div>
    );
  } else if (status === "failed" || status === "unavailable") {
    body = (
      <div className="m-4 flex items-start gap-2 rounded border border-vector-crimson/30 bg-vector-crimson/10 p-3 font-mono text-xs text-vector-crimson">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>{breakdown?.error ?? "The breakdown could not be generated."}</span>
      </div>
    );
  } else if (status === "ready" && videoUrl) {
    body = (
      <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="flex flex-col gap-3">
          <video
            ref={videoRef}
            key={videoUrl}
            src={videoUrl}
            controls
            playsInline
            onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
            className="w-full rounded-lg border border-pitch-border bg-black"
          />
          <Provenance breakdown={breakdown} />
        </div>

        <ol className="flex max-h-[28rem] flex-col gap-2 overflow-y-auto pr-1">
          {segments.map((segment, index) => (
            <li key={index}>
              <button
                type="button"
                onClick={() => seek(segment.start)}
                className={`w-full rounded-lg border p-3 text-left transition-colors ${
                  index === activeIndex
                    ? "border-volt/50 bg-volt/10"
                    : "border-pitch-border bg-pitch-dark hover:bg-pitch-hover"
                }`}
              >
                <div className="mb-1 flex items-center gap-2 font-mono text-[10px] uppercase text-zinc-500">
                  <span className="text-volt">
                    {segment.start !== null ? formatTime(segment.start) : "—"}
                  </span>
                  <span>·</span>
                  <span>{segment.kind}</span>
                  <span>·</span>
                  <span>clip {segment.at_sec.toFixed(1)}s</span>
                </div>
                <p className="text-xs leading-relaxed text-zinc-200">{segment.text}</p>
              </button>
            </li>
          ))}
        </ol>
      </div>
    );
  } else {
    body = <Note>No breakdown yet. Press Generate to create one.</Note>;
  }

  return (
    <section className="w-full overflow-hidden rounded-xl border border-pitch-border bg-pitch-card/40 shadow-2xl backdrop-blur">
      {header}
      {body}
      {analysis ? <AnalysisSummary analysis={analysis} teams={teams} /> : null}
    </section>
  );
};

const Note: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <p className="p-6 font-mono text-xs leading-relaxed text-zinc-500">{children}</p>
);

const Provenance: React.FC<{ breakdown?: Breakdown }> = ({ breakdown }) => {
  if (!breakdown) return null;
  const voiced = breakdown.voice_source && breakdown.voice_source !== "none";
  const templated = breakdown.script_source === "template";
  return (
    <div className="flex flex-col gap-1.5 font-mono text-[10px] text-zinc-500">
      <div className="flex flex-wrap gap-1.5">
        <Badge icon={<Sparkles className="h-3 w-3" />}>
          Script: {templated ? "template" : `AI · ${breakdown.script_source}`}
        </Badge>
        <Badge icon={voiced ? <Mic className="h-3 w-3" /> : <MicOff className="h-3 w-3" />}>
          {voiced ? "AI voice · ElevenLabs" : "Captions only"}
        </Badge>
        {breakdown.tracking_source ? <Badge>Tracking: {breakdown.tracking_source}</Badge> : null}
      </div>
      {templated && breakdown.script_fallback_reason ? (
        <span>Template script used: {breakdown.script_fallback_reason}</span>
      ) : null}
      {!voiced && breakdown.voice_error ? <span>No voice-over: {breakdown.voice_error}</span> : null}
      <span>
        Commentary is generated from automatically detected events and can be wrong. Speeds and
        distances are estimates.
      </span>
    </div>
  );
};

const Badge: React.FC<{ icon?: React.ReactNode; children: React.ReactNode }> = ({ icon, children }) => (
  <span className="flex items-center gap-1 rounded border border-pitch-border bg-pitch-dark px-1.5 py-0.5 uppercase">
    {icon}
    {children}
  </span>
);

const AnalysisSummary: React.FC<{
  analysis: TacticalAnalysis;
  teams: Record<string, { label: string; colour_name: string; color: string }>;
}> = ({ analysis, teams }) => {
  const possession = Object.entries(analysis.possession);
  const keyMoments = new Set(analysis.key_moments);
  const events = analysis.events.slice(0, 12);

  return (
    <div className="grid gap-4 border-t border-pitch-border p-4 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
      <div>
        <h3 className="mb-2 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
          Possession (detected)
        </h3>
        {possession.length ? (
          <>
            <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-pitch-dark">
              {possession.map(([team, share]) => (
                <div
                  key={team}
                  style={{ width: `${share}%`, background: teams[team]?.color ?? "#A1A1AA" }}
                />
              ))}
            </div>
            <ul className="mt-2 space-y-1 font-mono text-[11px] text-zinc-300">
              {possession.map(([team, share]) => (
                <li key={team} className="flex justify-between">
                  <span>
                    {teams[team]?.label ?? team} ({teams[team]?.colour_name ?? "?"})
                  </span>
                  <span>{share.toFixed(0)}%</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="font-mono text-[11px] text-zinc-500">The ball was not tracked well enough.</p>
        )}
      </div>

      <div>
        <h3 className="mb-2 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
          Detected events
        </h3>
        {events.length ? (
          <ul className="space-y-1.5">
            {events.map((event) => (
              <li key={event.id} className="flex gap-2 text-[11px] leading-snug text-zinc-300">
                <span className="w-10 shrink-0 font-mono text-zinc-500">{event.t.toFixed(1)}s</span>
                <span
                  className="h-fit shrink-0 rounded px-1.5 py-0.5 font-mono text-[9px] uppercase text-pitch-dark"
                  style={{ background: (event.team && teams[event.team]?.color) || "#A1A1AA" }}
                >
                  {EVENT_LABEL[event.type] ?? event.type}
                </span>
                <span className={keyMoments.has(event.id) ? "text-white" : undefined}>
                  {event.description}
                  {event.confidence < 0.6 ? (
                    <span className="ml-1 text-zinc-500">(low confidence)</span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="font-mono text-[11px] text-zinc-500">No passes, turnovers or sprints detected.</p>
        )}
      </div>
    </div>
  );
};
