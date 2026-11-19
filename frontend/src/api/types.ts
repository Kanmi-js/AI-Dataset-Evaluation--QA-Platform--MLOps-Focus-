// Shared API types mirroring the backend Pydantic schemas.

export type MediaType = "image" | "text" | "video";

export type ItemStatus =
  | "pending"
  | "processing"
  | "processed"
  | "reviewed"
  | "failed";

export type DatasetStatus = "active" | "archived";

// --- Video telemetry (written by app/workers/telemetry.py) ---

export interface TrackedEntity {
  id: string;
  role: string;
  label: string;
  /** Pixel coordinates in the source clip's own frame space. */
  coordinates: { x: number; y: number; width: number; height: number };
  speed_mps: number;
  distance_covered_m: number;
  /** Real tracker only: "A" | "B" | "other". */
  team?: string | null;
  /** Real tracker only: overlay colour derived from the team's kit. */
  color?: string | null;
}

export interface FrameTelemetry {
  frame_index: number;
  timestamp_sec: number;
  tracked_entities: TrackedEntity[];
}

export interface TelemetrySurface {
  length_m: number;
  width_m: number;
  /** Plain-language note on how the pixel-to-metre scale was derived. */
  assumption: string;
}

export interface VideoTelemetry {
  /**
   * Where the numbers came from. `"simulated"` means a kinematic model, not a
   * tracker run over the footage — the UI badges this, so do not drop it.
   */
  source: string;
  sport: string;
  /**
   * Optional because `item_metadata` is free-form JSONB: a row written by an
   * older worker may predate any given field. Read it defensively.
   */
  surface?: TelemetrySurface;
  frame_width: number;
  frame_height: number;
  duration_sec: number;
  total_frames_sampled: number;
  frames: FrameTelemetry[];
  tactical_summary: string;
  /** Real tracker only: last second of the clip that was analysed. */
  analysed_until_sec?: number;
  /** Real tracker only: kit colour per team. */
  teams?: Record<string, { label: string; colour_name: string; color: string }>;
}

// --- Tactical events (written by app/workers/events.py) ---

export interface TacticalEvent {
  id: string;
  type: "pass" | "long_pass" | "turnover" | "sprint" | string;
  t: number;
  t_end: number;
  frame_index: number;
  team: string | null;
  players: number[];
  confidence: number;
  description: string;
  detail: Record<string, unknown>;
}

export interface TacticalAnalysis {
  possession: Record<string, number>;
  events: TacticalEvent[];
  key_moments: string[];
}

// --- Breakdown (written by app/workers/tasks.generate_breakdown) ---

export type BreakdownStatus = "queued" | "running" | "ready" | "failed" | "unavailable";

export interface BreakdownSegment {
  kind: "intro" | "moment" | "outro";
  event_id: string | null;
  at_sec: number;
  text: string;
  rings: number[];
  arrows: { from_player: number; to_player: number }[];
}

export interface BreakdownTimelineEntry {
  segment: number;
  source_sec: number;
  output_start_sec: number;
  output_end_sec: number;
}

export interface Breakdown {
  status: BreakdownStatus;
  stage?: "script" | "voice" | "render";
  error?: string;
  video_key?: string;
  title?: string;
  segments?: BreakdownSegment[];
  /** Model that wrote the script, or "template". */
  script_source?: string;
  script_fallback_reason?: string | null;
  /** "elevenlabs:<voice>" or "none". */
  voice_source?: string;
  voice_error?: string | null;
  tracking_source?: string;
  render?: { duration_sec: number; timeline: BreakdownTimelineEntry[] };
  generated_at?: string;
}

/** Worker-populated metadata. Keys present depend on the item's media type. */
export interface ItemMetadata {
  [key: string]: unknown;
  width?: number;
  height?: number;
  quality_flag?: string;
  // video
  fps?: number;
  fps_assumed?: boolean;
  frame_count?: number;
  duration_sec?: number;
  poster_frame_index?: number;
  telemetry?: VideoTelemetry;
  analysis?: TacticalAnalysis;
  breakdown?: Breakdown;
}

export interface User {
  id: number;
  email: string;
  full_name: string | null;
  is_active: boolean;
  is_superuser: boolean;
  created_at: string;
}

export interface Dataset {
  id: number;
  name: string;
  description: string | null;
  status: DatasetStatus;
  owner_id: number;
  created_at: string;
  item_count: number;
}

export interface DataItem {
  id: number;
  dataset_id: number;
  filename: string;
  media_type: MediaType;
  status: ItemStatus;
  raw_s3_key: string;
  processed_s3_key: string | null;
  size_bytes: number | null;
  item_metadata: ItemMetadata;
  annotations: Record<string, unknown>;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface DataItemDetail extends DataItem {
  raw_url: string | null;
  processed_url: string | null;
  breakdown_url: string | null;
}

export interface DatasetWithItems extends Dataset {
  items: DataItem[];
}

export interface Token {
  access_token: string;
  token_type: string;
}
