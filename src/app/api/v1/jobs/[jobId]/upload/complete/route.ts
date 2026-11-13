import "server-only";

import { NextResponse } from "next/server";

import { securityConfig } from "@/config/security";
import { requireSession } from "@/lib/api/auth";
import { BodyTooLargeError, readJsonBody } from "@/lib/api/body";
import { ApiError, toErrorResponse } from "@/lib/api/errors";
import { findJobForSession, markUploadComplete, type JobRow } from "@/lib/db/jobs";
import { verifySourceObject } from "@/lib/storage/objects";
import { z } from "zod";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const completeSchema = z.object({
  sizeBytes: z.number().int().positive(),
});

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
 * POST /api/v1/jobs/[jobId]/upload/complete
 *
 * Finalizes an upload — but ONLY when the server can verify:
 *   1. the session owns the job (anti cross-session access),
 *   2. the job is an upload job in the 'uploading' state (or already
 *      finalized — idempotent success),
 *   3. the upload authorization has not expired,
 *   4. the object actually exists in the private bucket at the exact
 *      server-generated path, with the declared size.
 *
 * The browser therefore can never "complete" a job without a real, verified
 * upload. The atomic state guard (uploading → queued, enforced by the
 * database transition trigger) makes duplicate finalization impossible.
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
      throw new ApiError(400, "This job was not created for file upload.");
    }

    // Idempotent path: already finalized (and enqueued) by an earlier call.
    if (job.status !== "uploading") {
      if (job.upload_completed_at && ["queued", "validating", "ready", "completed", "failed"].includes(job.status)) {
        return NextResponse.json({ enqueued: true, alreadyCompleted: true, job: toPublicJob(job) });
      }
      throw new ApiError(409, `Job is not awaiting upload completion (current: ${job.status}).`);
    }

    let body: unknown;
    try {
      body = await readJsonBody(request, securityConfig.bodyLimit.maxBytes);
    } catch (err) {
      if (err instanceof BodyTooLargeError) {
        throw new ApiError(413, "Request payload is too large.");
      }
      throw new ApiError(400, "Invalid JSON payload.");
    }
    const parsed = completeSchema.safeParse(body);
    if (!parsed.success) {
      throw new ApiError(400, "Provide the uploaded file size (sizeBytes).");
    }
    const { sizeBytes } = parsed.data;

    if (!job.upload_auth_expires_at || new Date(job.upload_auth_expires_at).getTime() <= Date.now()) {
      throw new ApiError(410, "Upload authorization expired. Request a new authorization and upload again.");
    }

    // Verify the real object in the private bucket (metadata, no proxying).
    const object = await verifySourceObject(jobId, job.source_path);
    if (!object) {
      throw new ApiError(409, "Uploaded object not found. Complete the file transfer first.");
    }

    const declared = job.source_metadata?.declared_size_bytes;
    if (typeof declared === "number" && declared !== object.size) {
      throw new ApiError(400, "Uploaded file size does not match the declared size. Upload the full file again.");
    }
    if (object.size !== sizeBytes) {
      throw new ApiError(400, "Uploaded file size does not match the reported size. Upload the full file again.");
    }

    // Atomic, guarded state transition (uploading → queued).
    const updated = await markUploadComplete(session.sessionId, jobId);
    if (updated === 0) {
      // Someone else won the transition — re-read and answer idempotently.
      const current = await findJobForSession(session.sessionId, jobId);
      if (current?.upload_completed_at) {
        return NextResponse.json({ enqueued: true, alreadyCompleted: true, job: toPublicJob(current) });
      }
      throw new ApiError(409, "Job state changed. Refresh and try again.");
    }

    const fresh = await findJobForSession(session.sessionId, jobId);
    return NextResponse.json({ enqueued: true, alreadyCompleted: false, job: toPublicJob(fresh ?? job) }, { status: 200 });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
