import type { ProcessingState } from "@/config/app";

/** Where the source video comes from. */
export type SourceType = "youtube" | "twitch" | "kick" | "upload" | "direct_url";

/** A video file selected in the browser. UI metadata only in Phase 1. */
export interface UploadedFileMeta {
  name: string;
  sizeBytes: number;
}

/** The configuration a user submits on the creation page. */
export interface CreationOptions {
  sourceType: SourceType;
  youtubeUrl?: string;
  twitchUrl?: string;
  uploadedFile?: UploadedFileMeta;
  clipCount: number;
  clipDurationSeconds: number;
  outputAspect: "9:16";
  resolution: "1080p";
  captionsEnabled: boolean;
}

/** A clip-creation job, mirroring the `jobs` table. */
export interface Job {
  id: string;
  sourceType: SourceType;
  sourceUrl?: string;
  /** TASK 015 P3: null = full_coverage (derived at analysis time). */
  requestedClips: number | null;
  clipDurationSeconds?: number;
  /** TASK 015 P3: the generation mode (default 'highlights'). */
  generationMode?: "highlights" | "full_coverage";
  /** TASK 015 P3: explicit output frame rate (30|60) or null (legacy auto). */
  outputFps?: number | null;
  /** TASK 015 P3: the probed source duration (null before ingestion). */
  sourceDurationSeconds?: number | null;
  status: ProcessingState;
  progress: number;
  createdAt: string;
  completedAt?: string;
  enqueuedAt?: string;
  /** Clips detected so far by the Phase 4 analysis (0 before it runs). */
  clipsCount?: number;
  errorMessage?: string;
}

/** A generated clip, mirroring the `clips` table. */
export interface Clip {
  id: string;
  jobId: string;
  title: string;
  /** Seconds from the start of the source video. */
  startTime: number;
  /** Seconds from the start of the source video. */
  endTime: number;
  /** end_time - start_time. */
  duration: number;
  sortOrder: number;
  storagePath?: string;
  status: "pending" | "processing" | "ready" | "failed";
}

/** Result of a validation check. */
export interface ValidationResult {
  valid: boolean;
  error?: string;
}
