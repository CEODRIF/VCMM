import "server-only";

import { NextResponse } from "next/server";

import { securityConfig } from "@/config/security";
import { requireSession } from "@/lib/api/auth";
import { BodyTooLargeError, readJsonBody } from "@/lib/api/body";
import { toErrorResponse } from "@/lib/api/errors";
import type { JobRow } from "@/lib/db/jobs";
import { listJobsForSession } from "@/lib/db/jobs";
import { createJob } from "@/lib/jobs/service";
import { validateCreateJobPayload } from "@/lib/jobs/validate";
import { jobCreationRateLimiter } from "@/lib/security/rate-limit";

/**
 * Job API (session-scoped).
 *
 * Every handler first verifies the HttpOnly session cookie server-side.
 * Responses expose only public job fields — never internal storage paths.
 */

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
    // Error detail is shared only with the owning session, and only on failure.
    errorMessage: row.status === "failed" ? (row.error_message ?? undefined) : undefined,
  };
}

/**
 * POST /api/v1/jobs — create a job.
 *
 * Body: { sourceType, youtubeUrl? | fileName?, clipCount, clipDurationSeconds, captionsEnabled }
 * The session (and therefore job ownership) comes exclusively from the
 * verified cookie — client-provided ownership is impossible.
 */
export async function POST(request: Request) {
  try {
    const session = await requireSession(request);

    // TASK 012: bounded body read — an oversized payload fails with a typed
    // 413 instead of being buffered in memory.
    let body: unknown;
    try {
      body = await readJsonBody(request, securityConfig.bodyLimit.maxBytes);
    } catch (err) {
      if (err instanceof BodyTooLargeError) {
        return NextResponse.json({ error: "Request payload is too large." }, { status: 413 });
      }
      return NextResponse.json({ error: "Invalid JSON payload." }, { status: 400 });
    }

    // TASK 012: per-session job-creation rate limit (unforgeable key).
    const limit = jobCreationRateLimiter.check(`job-create:${session.sessionId}`);
    if (limit.limited) {
      const res = NextResponse.json(
        { error: "Too many job creations. Please try again shortly." },
        { status: 429 }
      );
      res.headers.set("Retry-After", String(Math.max(1, Math.ceil(limit.retryAfterMs / 1000))));
      return res;
    }

    const validation = validateCreateJobPayload(body);
    if (!validation.success) {
      return NextResponse.json({ error: validation.error }, { status: 400 });
    }

    const job = await createJob(validation.data, session.sessionId);
    return NextResponse.json({ job }, { status: 201 });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}

/** GET /api/v1/jobs — list the current session's jobs (never others'). */
export async function GET(request: Request) {
  try {
    const session = await requireSession(request);
    const rows = await listJobsForSession(session.sessionId, 20);
    return NextResponse.json({ jobs: rows.map(toPublicJob) });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
