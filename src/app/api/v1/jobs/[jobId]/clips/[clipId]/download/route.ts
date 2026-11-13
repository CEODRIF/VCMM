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
import { sanitizeMediaFilename } from "@/lib/zip/filename";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * GET /api/v1/jobs/[jobId]/clips/[clipId]/download — short-lived signed URL
 * that triggers a browser DOWNLOAD of one clip (Content-Disposition
 * attachment with a server-sanitized filename).
 *
 * Same check chain as the preview endpoint; only the URL flavour differs
 * (longer TTL, `download` parameter instead of inline). A clip is
 * downloadable as soon as IT is ready — the job as a whole may still be
 * rendering the remaining clips.
 *
 * Missing objects / not-ready clips / foreign clips all return the SAME
 * generic 404 body (no existence disclosure) and no URL is ever minted.
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
      // Not rendered yet, or a corrupt/foreign stored path: same 404 shape —
      // the client simply sees "no download available".
      throw new ApiError(404, "Clip not found.");
    }

    // Existence check (metadata only — one listing for the whole job).
    const objects = await listJobClipObjects(job.id);
    if (!objects.has(`${clipId}.mp4`)) {
      throw new ApiError(404, "Clip not found.");
    }

    const { signedUrl, expiresInSeconds } = await createClipSignedUrl(
      { jobId: job.id, clipId: clip.id },
      uploadConfig.clipDownloadUrlSeconds(),
      { download: true, fileName: sanitizeMediaFilename(clip.title) }
    );
    return NextResponse.json({ url: signedUrl, expiresInSeconds });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
