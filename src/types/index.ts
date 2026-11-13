import type { ProcessingState } from "@/config/app";

/** Where the source video comes from. */
export type SourceType = "youtube" | "upload";

/** A video file selected in the browser. UI metadata only in Phase 1. */
export interface UploadedFileMeta {
  name: string;
  sizeBytes: number;
}

/** The configuration a user submits on the creation page. */
export interface CreationOptions {
  sourceType: SourceType;
  youtubeUrl?: string;
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
  requestedClips: number;
  clipDurationSeconds?: number;
  status: ProcessingState;
  /** 0-100. */
  progress: number;
  errorMessage?: string;
  createdAt: string;
  completedAt?: string;
  /** Set when the upload was verified and the job entered the queue. */
  enqueuedAt?: string;
  expiresAt?: string;
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
