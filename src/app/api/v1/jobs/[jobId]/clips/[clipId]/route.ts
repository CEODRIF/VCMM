import "server-only";

import { NextResponse } from "next/server";

import { ApiError, toErrorResponse } from "@/lib/api/errors";
import { requireOwnedJobForClip } from "@/lib/api/owned-job";
import {
  deleteClipRow,
  findClipForJob,
  markClipDeleting,
  restoreClipStatus,
  type ClipStatus,
} from "@/lib/jobs/results";
import { deleteClipObject, validateClipPath } from "@/lib/storage/clips";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * DELETE /api/v1/jobs/[jobId]/clips/[clipId] — PERMANENT clip deletion
 * (TASK 015 P4).
 *
 * Protocol (accuracy over optimism — a success is only reported after the
 * storage object is verified gone and the row is removed):
 *
 *   1. session + ownership (foreign/unknown → uniform 404);
 *   2. the clip must be FINISHED ('ready' or 'failed'): pending/processing
 *      clips are still being produced → 409; a clip already 'deleting'
 *      (concurrent delete) → 409;
 *   3. ATOMIC 'ready'|'failed' → 'deleting' (download/preview only serve
 *      'ready', so the clip is instantly un-downloadable);
 *   4. delete the storage object + VERIFY absence (idempotent when the
 *      object is already gone);
 *   5. on ANY storage failure: roll the row back to its previous status and
 *      answer 502 — the clip stays intact and downloadable;
 *   6. remove the row (guarded on 'deleting') → 200 { deleted: true }.
 *
 * Retries are idempotent: after the row is gone the next DELETE is the same
 * uniform 404 as any unknown clip — never a spurious error, never a
 * duplicate deletion.
 */
export async function DELETE(
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

    // 2) a clip still being produced is never deletable — UNLESS the job
    //    is TERMINAL and its worker lease is EXPIRED (no worker can hold it:
    //    the clip is stuck after a crash, and its half-rendered object is
    //    not "in active use").
    const jobTerminal = job.status === "completed" || job.status === "failed";
    const leaseExpired =
      job.lease_expires_at !== null && new Date(job.lease_expires_at).getTime() < Date.now();
    if (clip.status === "pending" || clip.status === "processing") {
      if (!(jobTerminal && leaseExpired)) {
        throw new ApiError(409, "This clip is still being processed. Try again in a moment.");
      }
    }
    if (clip.status === "deleting") {
      throw new ApiError(409, "This clip is already being deleted.");
    }

    // 3) atomic transition from the EXACT verified status (concurrency
    //    guard: if the status changed between step 2 and now, the update
    //    matches no row and we answer 409 — never a blind re-delete).
    const from: ClipStatus[] = [clip.status];
    const grabbed = await markClipDeleting(job.id, clip.id, from);
    if (!grabbed) {
      throw new ApiError(409, "This clip is already being deleted.");
    }
    const previous: "ready" | "failed" | "processing" =
      clip.status === "ready" || clip.status === "failed" ? clip.status : "processing";

    try {
      // 4) storage deletion + absence verification (canonical path only).
      const hasObject =
        clip.storage_path !== null && validateClipPath(clip.storage_path, job.id, clip.id);
      if (hasObject) {
        await deleteClipObject({ jobId: job.id, clipId: clip.id });
      }
      // (a 'failed' clip without a verified path is a tombstone — nothing
      // to delete in storage; the row removal below is the whole deletion)
    } catch (error) {
      // 5) NEVER report success: roll back and surface the inaccuracy.
      const restored = await restoreClipStatus(job.id, clip.id, previous).catch(() => false);
      if (!restored) {
        throw new ApiError(
          500,
          "Deletion stopped and the clip status could not be restored. Contact support."
        );
      }
      console.warn(`clip delete aborted for ${job.id}/${clip.id}:`, error);
      throw new ApiError(
        502,
        "Deletion failed before the file was removed — the clip is still available. Try again."
      );
    }

    // 6) remove the row (guarded on 'deleting').
    await deleteClipRow(job.id, clip.id);
    return NextResponse.json({ deleted: true, clipId: clip.id });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
