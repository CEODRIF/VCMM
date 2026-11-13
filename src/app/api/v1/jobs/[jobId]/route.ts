import "server-only";

import { NextResponse } from "next/server";

import { requireSession } from "@/lib/api/auth";
import { toErrorResponse } from "@/lib/api/errors";
import { findJobForSession } from "@/lib/db/jobs";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * GET /api/v1/jobs/[jobId] — status of ONE job.
 *
 * Anti-enumeration:
 *   - non-UUID ids are rejected with 404 without a database round trip;
 *   - the lookup is always filtered by the server-verified session_id, so a
 *     foreign or unknown job and a nonexistent job are indistinguishable
 *     (both return the same 404 body).
 */
export async function GET(request: Request, context: { params: { jobId: string } }) {
  try {
    const jobId = context.params.jobId;
    if (!UUID_PATTERN.test(jobId)) {
      return NextResponse.json({ error: "Job not found." }, { status: 404 });
    }

    const session = await requireSession(request);
    const row = await findJobForSession(session.sessionId, jobId);
    if (!row) {
      return NextResponse.json({ error: "Job not found." }, { status: 404 });
    }

    return NextResponse.json({
      job: {
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
      },
    });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
