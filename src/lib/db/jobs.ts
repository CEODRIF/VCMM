import "server-only";

import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Job data access (server-only, service role).
 *
 * EVERY query is scoped by session_id — job rows are only ever fetched when
 * the server-verified session owns them. There is no code path that returns a
 * job by id alone, which prevents cross-session enumeration.
 *
 * Phase 3: the jobs row carries the ingestion queue state (lease, attempts,
 * upload completion, verified source metadata). Queue-control writes by the
 * WORKER happen exclusively through the restricted RPCs in migration
 * 20261115000000; the API performs only its own guarded transitions
 * (creation, upload authorization, upload completion).
 */

export type JobStatus =
  | "uploading"
  | "queued"
  | "validating"
  | "ready"
  | "transcribing"
  | "analyzing"
  | "rendering"
  | "completed"
  | "failed";

export interface JobRow {
  id: string;
  session_id: string;
  source_type: "youtube" | "upload";
  source_url: string | null;
  source_path: string | null;
  requested_clips: number;
  clip_duration_seconds: number;
  captions_enabled: boolean;
  status: JobStatus;
  progress: number;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
  expires_at: string | null;
  // Phase 3 — queue / ingestion
  attempt_count: number;
  max_attempts: number;
  locked_by: string | null;
  locked_at: string | null;
  lease_expires_at: string | null;
  last_error: string | null;
  upload_completed_at: string | null;
  upload_auth_expires_at: string | null;
  source_metadata: Record<string, unknown>;
  updated_at: string;
}

export interface NewJobInput {
  id: string;
  sessionId: string;
  sourceType: "youtube" | "upload";
  sourceUrl?: string;
  sourcePath?: string;
  requestedClips: number;
  clipDurationSeconds: number;
  captionsEnabled: boolean;
  /** Initial state: upload jobs wait for the file ('uploading'); provider-backed jobs start 'queued'. */
  initialStatus: "uploading" | "queued";
}

/** Inserts a job row with the given initial status (DB enforces the transition map). */
export async function insertJob(input: NewJobInput): Promise<{ id: string }> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .insert({
      id: input.id,
      session_id: input.sessionId,
      source_type: input.sourceType,
      source_url: input.sourceUrl ?? null,
      source_path: input.sourcePath ?? null,
      requested_clips: input.requestedClips,
      clip_duration_seconds: input.clipDurationSeconds,
      captions_enabled: input.captionsEnabled,
      status: input.initialStatus,
      progress: 0,
    })
    .select("id")
    .single();
  if (error) {
    throw new Error(`Job creation failed: ${error.code ?? "unknown"}`);
  }
  return (data as { id: string }) ?? { id: input.id };
}

/** Fetches one job only if it belongs to the given session (else null). */
export async function findJobForSession(sessionId: string, jobId: string): Promise<JobRow | null> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .select("*")
    .eq("id", jobId)
    .eq("session_id", sessionId)
    .maybeSingle();
  if (error) {
    throw new Error(`Job lookup failed: ${error.code ?? "unknown"}`);
  }
  return (data as JobRow | null) ?? null;
}

/** Lists only the jobs of the given session, newest first. */
export async function listJobsForSession(sessionId: string, limit = 20): Promise<JobRow[]> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .select("*")
    .eq("session_id", sessionId)
    .order("created_at", { ascending: false })
    .limit(limit);
  if (error) {
    throw new Error(`Job list failed: ${error.code ?? "unknown"}`);
  }
  return (data as JobRow[]) ?? [];
}

/**
 * Records the server-generated upload path and authorization expiry for a job
 * still in the 'uploading' state. Scoped by session + job + state.
 * Returns the number of updated rows (0 when the guard did not match).
 */
export async function setUploadAuthorization(
  sessionId: string,
  jobId: string,
  sourcePath: string,
  authExpiresAt: string,
  declared: { fileName: string; sizeBytes: number; mimeType: string }
): Promise<number> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .update({
      source_path: sourcePath,
      upload_auth_expires_at: authExpiresAt,
      source_metadata: {
        original_file_name: declared.fileName,
        declared_size_bytes: declared.sizeBytes,
        declared_mime_type: declared.mimeType,
      },
    })
    .eq("id", jobId)
    .eq("session_id", sessionId)
    .eq("status", "uploading")
    .select("id");
  if (error) {
    throw new Error(`Upload authorization update failed: ${error.code ?? "unknown"}`);
  }
  // Exactly one row can match (primary key) — the guarded update either
  // touched it or it did not.
  return (data ?? []).length;
}

/**
 * Atomically marks the upload as completed: 'uploading' → 'queued'.
 * The state guard makes duplicate finalization impossible — only the first
 * caller transitions the row (the transition trigger enforces legality).
 * Returns the number of updated rows (0 when already finalized or invalid).
 */
export async function markUploadComplete(sessionId: string, jobId: string): Promise<number> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .update({
      status: "queued",
      upload_completed_at: new Date().toISOString(),
    })
    .eq("id", jobId)
    .eq("session_id", sessionId)
    .eq("status", "uploading")
    .select("id");
  if (error) {
    throw new Error(`Upload completion update failed: ${error.code ?? "unknown"}`);
  }
  // Exactly one row can match (primary key) — the guarded update either
  // touched it or it did not.
  return (data ?? []).length;
}
