import "server-only";

import { requireSession } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/errors";
import { findJobForSession, type JobRow } from "@/lib/db/jobs";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Session + ownership gate shared by ALL results routes (Phase 6).
 *
 * Anti-enumeration contract (identical to /api/v1/jobs/[jobId]):
 *   - non-UUID ids are rejected with 404 before any session/DB round trip;
 *   - the lookup is ALWAYS filtered by the server-verified session_id, so a
 *     foreign job and a nonexistent job are indistinguishable (same 404 body);
 *   - client-supplied ownership claims are never accepted — the session is
 *     the only identity source.
 *
 * Returns the full job row (server-side only) so callers can use any column
 * without a second round trip; response MAPPING still decides what leaves
 * the server (internal paths never do).
 */
export async function requireOwnedJob(request: Request, jobId: string): Promise<JobRow> {
  if (!UUID_PATTERN.test(jobId)) {
    throw new ApiError(404, "Job not found.");
  }
  const session = await requireSession(request);
  const row = await findJobForSession(session.sessionId, jobId);
  if (!row) {
    throw new ApiError(404, "Job not found.");
  }
  return row;
}

/**
 * Ownership gate for CLIP-level endpoints (preview / download).
 *
 * Same contract as :func:`requireOwnedJob`, but 404-class failures are
 * re-expressed as "Clip not found." so that at this URL level ONE uniform
 * 404 body covers "you do not own this job", "the clip does not exist",
 * "the clip is not ready" and "the object is missing" — probing can never
 * tell these apart. 401 (no session) is passed through unchanged.
 */
export async function requireOwnedJobForClip(request: Request, jobId: string): Promise<JobRow> {
  try {
    return await requireOwnedJob(request, jobId);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      throw new ApiError(404, "Clip not found.");
    }
    throw error;
  }
}
