import "server-only";

import { randomUUID } from "node:crypto";

import { insertJob } from "@/lib/db/jobs";
import { ApiError } from "@/lib/api/errors";
import { getYouTubeProvider } from "@/lib/providers/youtube";
import { classifyVideoUrl } from "@/lib/validation/clips";
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
 * - TASK 014 — universal link ingestion:
 *   - `direct_url` jobs (public http(s) links to .mp4/.webm/.mov files,
 *     classified by shape) are queued; the worker downloads them through
 *     the SSRF guard, FFprobe-validates the bytes, and stores the object
 *     in the private sources bucket.
 *   - Twitch CLIP URLs are queued (the worker imports them through the
 *     official Get Clips Download endpoint under the creator's OAuth
 *     authorization; per-class runtime gates apply). Twitch VOD and
 *     live URLs are refused with distinct actionable 503s (platform
 *     facts — no authorized VOD media endpoint exists).
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
    // TASK 014 — honest per-URL-class gate. The input URL is classified by
    // SHAPE (no network). The capability truth for Twitch media lives in
    // the WORKER (official Get Clips Download path under the creator's
    // OAuth authorization, plus the per-class runtime gates), so this
    // layer no longer 503s on the legacy "unconfigured provider" state —
    // an unconfigured worker fails the job with a clear, permanent,
    // actionable error instead (accurate status, no pretend import).
    // Platform FACTS are refused here with distinct actionable 503s:
    // VODs have NO official media-download endpoint; live streams are
    // never completed recordings.
    const twitchUrl = input.twitchUrl ?? input.videoUrl;
    const kind = twitchUrl ? classifyVideoUrl(twitchUrl) : "unrecognized";
    if (kind === "twitch-clip") {
      await insertJob({
        id,
        sessionId,
        sourceType: "twitch",
        sourceUrl: twitchUrl,
        requestedClips: input.clipCount,
        clipDurationSeconds: input.clipDurationSeconds,
        captionsEnabled: input.captionsEnabled,
        initialStatus: "queued",
      });
      return { id, status: "queued", createdAt };
    }
    throw new ApiError(
      503,
      kind === "twitch-vod"
        ? "Twitch VOD media cannot be imported: the official Twitch APIs expose no authorized VOD media-download endpoint. Export the VOD from the Twitch dashboard and upload the file directly."
        : "This Twitch URL is a live stream, not a completed recording — wait for the VOD, or upload the file directly."
    );
  }

  if (input.sourceType === "direct_url") {
    // TASK 014 — direct video file URL (the schema guarantees the URL
    // classified as a public file-shaped video link). The worker downloads
    // it through the SSRF guard, FFprobe-validates the bytes, and stores
    // the object in the private sources bucket — the same pipeline as
    // uploads below the provider.
    const videoUrl = input.videoUrl;
    await insertJob({
      id,
      sessionId,
      sourceType: "direct_url",
      sourceUrl: videoUrl,
      requestedClips: input.clipCount,
      clipDurationSeconds: input.clipDurationSeconds,
      captionsEnabled: input.captionsEnabled,
      initialStatus: "queued",
    });
    return { id, status: "queued", createdAt };
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
