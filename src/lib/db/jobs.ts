import "server-only";

import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Job data access (server-only, service role).
 *
 * EVERY query is scoped by session_id — job rows are only ever fetched when
 * the server-verified session owns them. There is no code path that returns a
 * job by id alone, which prevents cross-session enumeration.
 */

export type JobStatus =
  | "queued"
  | "uploading"
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
}

/** Inserts a job row (status starts at `queued`). */
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
      status: "queued",
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
