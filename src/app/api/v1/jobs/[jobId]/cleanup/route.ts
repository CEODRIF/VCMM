import "server-only";

import { NextResponse } from "next/server";

import { ApiError, toErrorResponse } from "@/lib/api/errors";
import { requireOwnedJob } from "@/lib/api/owned-job";
import {
  deleteClipRow,
  listClipsForJob,
  markClipDeleting,
  markJobPurged,
  restoreClipStatus,
  setJobCleanupError,
} from "@/lib/jobs/results";
import { deleteClipObject, deleteSourceObject, validateClipPath, validateSourcePath } from "@/lib/storage/clips";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * POST /api/v1/jobs/[jobId]/cleanup — job-level permanent cleanup
 * (TASK 015 P4). Deletes ALL of the job's generated clips AND the source
 * object, then marks the job purged (the Phase 6 marker, reused).
 *
 * Body: `{ "confirm": true }` is REQUIRED — the UI performs the explicit
 * user confirmation; the server refuses to run without the flag so a stray
 * request can never destroy media.
 *
 * Safety rules:
 *   - ownership verified (session); foreign/unknown jobs → uniform 404;
 *   - only TERMINAL jobs (completed/failed) may be cleaned up — an active
 *     job's media is in use → 409;
 *   - clips in 'deleting' are skipped (a concurrent single-clip delete
 *     owns them); 'processing'/'pending' clips of a terminal job have no
 *     active worker (lease expired) and are cleaned up;
 *   - deletion order: clips first, then the source, then the purge marker
 *     (storage before DB — same at-least-once contract as the Phase 6
 *     purge, so a crash mid-way is retried by calling again);
 *   - PARTIAL FAILURE: the job stays un-purged, `cleanup_error` records
 *     what failed (visible in the results UI + retryable via this route);
 *     clips already removed stay removed (idempotent).
 */
export async function POST(
  request: Request,
  context: { params: { jobId: string } }
) {
  try {
    const { jobId } = context.params;
    if (!UUID_PATTERN.test(jobId)) {
      throw new ApiError(404, "Job not found.");
    }

    const body = (await request.json().catch(() => null)) as { confirm?: boolean } | null;
    if (body?.confirm !== true) {
      throw new ApiError(400, "Cleanup requires explicit confirmation (confirm: true).");
    }

    const job = await requireOwnedJob(request, jobId);
    if (job.status !== "completed" && job.status !== "failed") {
      throw new ApiError(
        409,
        "This job is still active. Cleanup is available once the job finishes."
      );
    }
    if (job.purged_at) {
      // Idempotent: everything eligible is already gone.
      return NextResponse.json({
        purged: true,
        alreadyPurged: true,
        clipsDeleted: 0,
        sourcesDeleted: 0,
        errors: [],
      });
    }

    const clips = await listClipsForJob(job.id);
    const errors: string[] = [];
    let clipsDeleted = 0;
    let sourcesDeleted = 0;

    for (const clip of clips) {
      if (clip.status === "deleting") {
        // A concurrent single-clip delete owns this row — leave it to that
        // request (its row/object will be gone before its finish).
        continue;
      }
      const previous = clip.status;
      const grabbed = await markClipDeleting(job.id, clip.id, [previous]).catch(() => false);
      if (!grabbed) {
        // Lost the race (status changed between list and transition).
        if (previous !== "ready" && previous !== "failed") {
          errors.push(`clip ${clip.id}: status changed during cleanup (was ${previous})`);
        }
        continue;
      }
      try {
        const hasObject =
          clip.storage_path !== null && validateClipPath(clip.storage_path, job.id, clip.id);
        if (hasObject) {
          await deleteClipObject({ jobId: job.id, clipId: clip.id });
        }
        await deleteClipRow(job.id, clip.id);
        clipsDeleted += 1;
      } catch (error) {
        await restoreClipStatus(job.id, clip.id, previous).catch(() => false);
        errors.push(`clip ${clip.id}: ${error instanceof Error ? error.message : "deletion failed"}`);
      }
    }

    // The source object (only the canonical job-scoped path).
    if (validateSourcePath(job.source_path, job.id)) {
      try {
        const result = await deleteSourceObject(job.id, job.source_path as string);
        sourcesDeleted = result.deleted ? 1 : 0;
        if (result.missing) sourcesDeleted = 0; // already gone — nothing counted
      } catch (error) {
        errors.push(`source: ${error instanceof Error ? error.message : "deletion failed"}`);
      }
    }

    if (errors.length === 0) {
      const marked = await markJobPurged(job.id);
      return NextResponse.json({
        purged: marked,
        alreadyPurged: false,
        clipsDeleted,
        sourcesDeleted,
        errors: [],
      });
    }

    // Partial failure: VISIBLE (cleanup_error) and RETRYABLE (un-purged).
    const message = `Cleanup incomplete — ${errors.length} item(s) failed: ${errors
      .slice(0, 3)
      .join("; ")}${errors.length > 3 ? " …" : ""}`;
    await setJobCleanupError(job.id, message);
    return NextResponse.json({
      purged: false,
      alreadyPurged: false,
      clipsDeleted,
      sourcesDeleted,
      errors,
      retryable: true,
    });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
