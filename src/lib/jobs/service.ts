import "server-only";

import { randomUUID } from "node:crypto";

import { insertJob } from "@/lib/db/jobs";
import { ApiError } from "@/lib/api/errors";
import { getTwitchProvider } from "@/lib/providers/twitch";
import { getYouTubeProvider } from "@/lib/providers/youtube";
import type { CreateJobInput } from "@/lib/jobs/validate";

/**
 * Job creation service (Phase 3).
 *
 * - Upload jobs start in 'uploading' with a reserved per-job storage
 *   location (satisfying the Phase 1 `jobs_source_present` constraint). The
 *   authorization endpoint later pins the exact server-generated object
 *   path, and the job is enqueued only after verified upload completion.
 * - YouTube jobs require an AUTHORIZED source provider. When none is
 *   configured, creation fails with a clear 503 — no unauthorized download
 *   path exists in this codebase, and direct uploads remain fully functional.
 *
 * SECURITY: the session id comes from the server-verified session cookie
 * (requireSession) — never from the request body.
 */

export interface CreateJobResult {
  id: string;
  status: "uploading" | "queued";
  createdAt: string;
}

/** Sanitizes a user-supplied file name to a safe basename component. */
export function sanitizeFileName(name: string): string {
  const safe = name.trim().replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 120);
  return safe.length > 0 ? safe : "video.mp4";
}

export async function createJob(input: CreateJobInput, sessionId: string): Promise<CreateJobResult> {
  const id = randomUUID();
  const createdAt = new Date().toISOString();

  if (input.sourceType === "youtube") {
    const provider = getYouTubeProvider();
    if (!provider.isAvailable()) {
      throw new ApiError(
        503,
        "YouTube source provider is not configured on this deployment. Direct video file upload is available."
      );
    }
    // TASK 011 capability gate: metadata availability NEVER implies
    // materialization authorization. A provider that can read metadata but
    // has no authorized media-download path is refused with a clear,
    // distinct message (no job row is created).
    if (!provider.supportsMaterialization()) {
      throw new ApiError(
        503,
        "YouTube sources cannot be materialized on this deployment: the official YouTube APIs expose metadata but no authorized media-download endpoint. Direct video file upload is available."
      );
    }
    // INTEGRATION POINT (authorized provider only): an explicitly approved
    // YouTubeSourceProvider implementation would materialize the source into
    // private storage here. None ships with this codebase, so a configured
    // but unwired provider is still refused rather than guessed at.
    throw new ApiError(
      503,
      "YouTube source provider is not configured on this deployment. Direct video file upload is available."
    );
  }

  if (input.sourceType === "twitch") {
    const provider = getTwitchProvider();
    if (!provider.isAvailable()) {
      throw new ApiError(
        503,
        "Twitch source provider is not configured on this deployment. Direct video file upload is available."
      );
    }
    // Same capability gate as YouTube (TASK 011): the official Twitch APIs
    // (Helix) expose metadata only — no media-download endpoint — so an
    // honest implementation reports supportsMaterialization() = false and
    // job creation is refused with a clear, distinct message.
    if (!provider.supportsMaterialization()) {
      throw new ApiError(
        503,
        "Twitch sources cannot be materialized on this deployment: the official Twitch APIs expose metadata but no authorized media-download endpoint. Direct video file upload is available."
      );
    }
    // INTEGRATION POINT (authorized provider only): a licensed ingestion
    // path explicitly approved and wired here would materialize the VOD
    // into private storage. None ships with this codebase.
    throw new ApiError(
      503,
      "Twitch source provider is not configured on this deployment. Direct video file upload is available."
    );
  }

  // Upload: reserved storage location; replaced by the exact generated path
  // at POST .../upload/authorize.
  await insertJob({
    id,
    sessionId,
    sourceType: "upload",
    sourcePath: `sources/${id}/reserved`,
    requestedClips: input.clipCount,
    clipDurationSeconds: input.clipDurationSeconds,
    captionsEnabled: input.captionsEnabled,
    initialStatus: "uploading",
  });

  // NOTE: internal storage paths are intentionally NOT returned to the
  // client (no private storage URLs in API responses).
  return { id, status: "uploading", createdAt };
}
