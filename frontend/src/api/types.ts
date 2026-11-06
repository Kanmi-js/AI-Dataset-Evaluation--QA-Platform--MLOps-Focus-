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
}

export interface DatasetWithItems extends Dataset {
  items: DataItem[];
}

export interface Token {
  access_token: string;
  token_type: string;
}
