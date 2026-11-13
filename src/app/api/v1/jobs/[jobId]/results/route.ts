import "server-only";

import { NextResponse } from "next/server";

import { uploadConfig } from "@/config/uploads";
import { requireOwnedJob } from "@/lib/api/owned-job";
import { toErrorResponse } from "@/lib/api/errors";
import { listClipsForJob } from "@/lib/jobs/results";
import { listJobClipObjects, validateClipPath } from "@/lib/storage/clips";

/**
 * GET /api/v1/jobs/[jobId]/results — the session's job with its clips.
 *
 * - Session + ownership verified on every request (requireOwnedJob);
 *   foreign/unknown jobs and nonexistent jobs are indistinguishable (404).
 * - Only clips of THIS job are returned (query is job-scoped).
 * - SAFE metadata only: title, duration, sort order, status and a
 *   `downloadable` flag. Internal storage paths, source paths and any
 *   server credential never leave the server.
 * - `downloadable` is true only for clips that are `ready` AND whose stored
 *   path matches the canonical convention for this exact (job, clip) pair.
 */
export async function GET(request: Request, context: { params: { jobId: string } }) {
  try {
    const job = await requireOwnedJob(request, context.params.jobId);
    const clips = await listClipsForJob(job.id);
    const downloadable = (c: (typeof clips)[number]) =>
      c.status === "ready" && validateClipPath(c.storage_path, job.id, c.id);

    // ZIP feasibility for a COMPLETED job (metadata only — one listing).
    // Lets the UI offer, explain-disable or hide "Download all" BEFORE the
    // user clicks (size cap → clear per-clip fallback, no failed download).
    let zip: { available: boolean; totalBytes: number; limitBytes: number } | undefined;
    if (job.status === "completed") {
      const limitBytes = uploadConfig.maxZipBytes();
      const objects = await listJobClipObjects(job.id);
      const totalBytes = clips
        .filter(downloadable)
        .reduce((n, c) => n + (objects.get(`${c.id}.mp4`)?.size ?? 0), 0);
      zip = { available: totalBytes > 0 && totalBytes <= limitBytes, totalBytes, limitBytes };
    }

    return NextResponse.json({
      job: {
        id: job.id,
        status: job.status,
        progress: job.progress,
        createdAt: job.created_at,
        completedAt: job.completed_at,
        /** Phase 6 retention marker: when the artifacts become eligible for cleanup. */
        expiresAt: job.expires_at,
        clipsReady: clips.filter((c) => c.status === "ready").length,
      },
      clips: clips.map((c) => ({
        id: c.id,
        title: c.title,
        duration: c.duration,
        sortOrder: c.sort_order,
        status: c.status,
        downloadable: downloadable(c),
      })),
      zip,
    });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
