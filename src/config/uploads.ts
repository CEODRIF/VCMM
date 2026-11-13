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

  /**
   * Phase 6 — signed clip URLs. SHORT-LIVED on purpose: a preview URL is
   * good for 5 minutes (enough to watch the clip a few times), a download
   * URL for 15 minutes (enough to start + resume a multi-hundred-MB file).
   * They are read-only, path-bound, and expire — no public access exists.
   */
  clipPreviewUrlSeconds: (): number => envInt("CLIP_PREVIEW_URL_SECONDS", 300),
  clipDownloadUrlSeconds: (): number => envInt("CLIP_DOWNLOAD_URL_SECONDS", 900),

  /**
   * Phase 6 — ZIP cap: the TOTAL size of the ready clip objects. The ZIP is
   * streamed (memory stays at chunk size), but serverless execution time
   * and bandwidth are bounded — a clear 413 with a "download individually"
   * fallback beats a mid-stream abort. Default 1 GiB.
   */
  maxZipBytes: (): number => envInt("MAX_ZIP_SIZE_BYTES", 1 * 1024 * 1024 * 1024),
} as const;

export type AllowedExtension = keyof typeof uploadConfig.allowedExtensions;
