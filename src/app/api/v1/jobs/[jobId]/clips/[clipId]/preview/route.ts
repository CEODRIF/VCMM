import "server-only";

import { NextResponse } from "next/server";

import { uploadConfig } from "@/config/uploads";
import { ApiError, toErrorResponse } from "@/lib/api/errors";
import { requireOwnedJobForClip } from "@/lib/api/owned-job";
import { findClipForJob } from "@/lib/jobs/results";
import {
  createClipSignedUrl,
  listJobClipObjects,
  validateClipPath,
} from "@/lib/storage/clips";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * GET /api/v1/jobs/[jobId]/clips/[clipId]/preview — short-lived signed URL
 * for the <video> element.
 *
 * Checks (in order): session + job ownership → clip UUID shape → clip
 * belongs to the job (DB query is job-scoped) → clip is `ready` with a
 * path matching the canonical convention for THIS (job, clip) → the object
 * actually exists in Storage. Only then is a 5-minute signed URL minted.
 *
 * Missing objects / not-ready clips return the SAME generic 404 body as an
 * unknown clip id (no existence disclosure), and no URL is ever issued for
 * a clip that failed a check.
 */
export async function GET(
  request: Request,
  context: { params: { jobId: string; clipId: string } }
) {
  try {
    const { jobId, clipId } = context.params;
    if (!UUID_PATTERN.test(clipId)) {
      throw new ApiError(404, "Clip not found.");
    }

    const job = await requireOwnedJobForClip(request, jobId);
    const clip = await findClipForJob(job.id, clipId);
    if (!clip) {
      throw new ApiError(404, "Clip not found.");
    }
    if (clip.status !== "ready" || !validateClipPath(clip.storage_path, job.id, clip.id)) {
      // Not ready yet, or the stored path is malformed/foreign: same 404
      // shape — the client simply sees "no preview available".
      throw new ApiError(404, "Clip not found.");
    }

    // Existence check (metadata only — one listing for the whole job).
    const objects = await listJobClipObjects(job.id);
    if (!objects.has(`${clipId}.mp4`)) {
      throw new ApiError(404, "Clip not found.");
    }

    const { signedUrl, expiresInSeconds } = await createClipSignedUrl(
      { jobId: job.id, clipId: clip.id },
      uploadConfig.clipPreviewUrlSeconds()
    );
    return NextResponse.json({ url: signedUrl, expiresInSeconds });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
