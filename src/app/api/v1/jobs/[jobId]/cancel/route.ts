import "server-only";

import { NextResponse } from "next/server";

import { requireSession } from "@/lib/api/auth";
import { ApiError, toErrorResponse } from "@/lib/api/errors";
import { cancelUploadJob, findJobForSession, type JobRow } from "@/lib/db/jobs";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function toPublicJob(row: JobRow) {
  return {
    id: row.id,
    sourceType: row.source_type,
    requestedClips: row.requested_clips,
    clipDurationSeconds: row.clip_duration_seconds,
    captionsEnabled: row.captions_enabled,
    status: row.status,
    progress: row.progress,
    createdAt: row.created_at,
    completedAt: row.completed_at,
    enqueuedAt: row.upload_completed_at,
    errorMessage: row.status === "failed" ? (row.error_message ?? row.last_error ?? undefined) : undefined,
  };
}

/**
 * POST /api/v1/jobs/[jobId]/cancel  —  TASK 017
 *
 * User cancellation of an upload that never finished. The client calls this
 * when the user clicks "Cancel upload" (or the transfer dies): the job row
 * is an 'uploading' zombie otherwise — claim_next_job never touches it and
 * finalization becomes impossible once the upload authorization expires.
 *
 * Rules (all enforced server-side, session-scoped — no cross-session access):
 *   - owner-only: the session must own the job (404 otherwise),
 *   - only 'uploading' jobs can be cancelled: a job already queued or
 *      processing cannot be stopped here (409 with its current state),
 *   - the transition is a guarded update (uploading → failed), so a double
 *     cancel or a cancel racing upload-completion can never touch the row
 *     twice; the loser gets 409.
 *
 * Cancelling arms the Phase 6 retention deadline (the transition is
 * terminal), so any partially uploaded object is covered by the existing
 * purge path instead of leaking.
 */
export async function POST(request: Request, context: { params: { jobId: string } }) {
  try {
    const jobId = context.params.jobId;
    if (!UUID_PATTERN.test(jobId)) {
      throw new ApiError(404, "Job not found.");
    }

    const session = await requireSession(request);
    const job = await findJobForSession(session.sessionId, jobId);
    if (!job) {
      throw new ApiError(404, "Job not found.");
    }
    if (job.source_type !== "upload") {
      throw new ApiError(400, "Only file-upload jobs can be cancelled.");
    }
    if (job.status !== "uploading") {
      throw new ApiError(409, `The upload is no longer in progress (current state: ${job.status}).`);
    }

    // Atomic, guarded transition (uploading → failed).
    const updated = await cancelUploadJob(jobId);
    if (updated === 0) {
      // Lost the race (upload completed or another cancel won) — report the
      // current state instead of pretending.
      const current = await findJobForSession(session.sessionId, jobId);
      throw new ApiError(409, `The upload is no longer in progress (current state: ${current?.status ?? "unknown"}).`);
    }

    const fresh = await findJobForSession(session.sessionId, jobId);
    return NextResponse.json({ cancelled: true, job: toPublicJob(fresh ?? job) });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
