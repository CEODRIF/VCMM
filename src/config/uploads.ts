import "server-only";

/**
 * Upload + media infrastructure configuration (Phase 3).
 *
 * No secrets here; limits are read from the environment at call time so
 * deployments can tune them without code changes.
 */

function envInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export const uploadConfig = {
  /** Private bucket for original source videos. */
  sourcesBucket: "clipforge-sources",
  /** Private bucket for rendered clips (Phases 5-6). */
  clipsBucket: "clipforge-clips",

  /**
   * Allowed source container formats: extension → canonical MIME type.
   * The browser's MIME/extension is a coarse pre-filter only; the worker
   * re-verifies the actual bytes with FFprobe.
   */
  allowedExtensions: {
    mp4: "video/mp4",
    mov: "video/quicktime",
    webm: "video/webm",
  } as const,

  allowedMimeTypes: new Set<string>(["video/mp4", "video/quicktime", "video/webm"]),

  /** Hard cap for a single source upload (bytes). Default 2 GiB. */
  maxUploadSizeBytes: (): number => envInt("MAX_UPLOAD_SIZE_BYTES", 2 * 1024 * 1024 * 1024),

  /**
   * Infrastructure limit for SOURCE video duration (seconds).
   * This is NOT the clip-duration product setting (60-180s) — a source can
   * be any length within this configured infrastructure limit.
   * Default 8 hours.
   */
  maxSourceDurationSeconds: (): number => envInt("MAX_SOURCE_DURATION_SECONDS", 8 * 3600),

  /** Our own upload-authorization TTL (seconds). Default 15 minutes. */
  uploadAuthTtlSeconds: (): number => envInt("UPLOAD_AUTH_TTL_SECONDS", 900),

  /** Rate limit for upload authorization requests per session. */
  authorizeRateLimit: {
    windowMs: 60 * 1000,
    maxAttempts: 10,
  },
} as const;

export type AllowedExtension = keyof typeof uploadConfig.allowedExtensions;
