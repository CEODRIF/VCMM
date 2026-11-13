import "server-only";

import { randomUUID } from "node:crypto";

import { insertJob } from "@/lib/db/jobs";
import type { CreateJobInput } from "@/lib/jobs/validate";

/**
 * Job creation service.
 *
 * SECURITY: the session id comes from the server-verified session cookie
 * (see requireSession in api/auth.ts) — never from the request body.
 */

export interface CreateJobResult {
  id: string;
  status: "queued";
  createdAt: string;
}

/** Sanitizes a user-supplied file name to a safe basename component. */
export function sanitizeFileName(name: string): string {
  const safe = name.trim().replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 120);
  return safe.length > 0 ? safe : "video.mp4";
}

export async function createJob(input: CreateJobInput, sessionId: string): Promise<CreateJobResult> {
  const id = randomUUID();
  const createdAt = new Date().toISOString();

  // For uploads the source file is reserved at a per-job storage path.
  // INTEGRATION POINT (Phase 3): the actual multipart transfer fills this
  // path; until then an upload job stays `queued` and cannot progress.
  const sourcePath =
    input.sourceType === "upload" ? `jobs/${id}/uploads/${sanitizeFileName(input.fileName ?? "video.mp4")}` : null;

  await insertJob({
    id,
    sessionId,
    sourceType: input.sourceType,
    sourceUrl: input.sourceType === "youtube" ? input.youtubeUrl?.trim() : undefined,
    sourcePath: sourcePath ?? undefined,
    requestedClips: input.clipCount,
    clipDurationSeconds: input.clipDurationSeconds,
    captionsEnabled: input.captionsEnabled,
  });

  // NOTE: internal storage paths are intentionally NOT returned to the
  // client (no private storage URLs in API responses).
  return { id, status: "queued", createdAt };
}
