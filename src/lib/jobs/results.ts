import "server-only";

import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Clip data access for the results API (Phase 6).
 *
 * Server-only, service role. EVERY query is scoped by job_id, and job_id is
 * only ever produced by :func:`requireOwnedJob` (session-verified), so a
 * caller can never read clips of a job it does not own.
 *
 * Rows include `storage_path` for SERVER-SIDE decisions (signed URLs, ZIP
 * packaging); the routes' response mapping strips it — internal storage
 * paths are never sent to the client.
 */

export type ClipStatus = "pending" | "processing" | "ready" | "failed" | "deleting";

export interface ClipRow {
  id: string;
  title: string;
  start_time: number;
  end_time: number;
  duration: number;
  sort_order: number;
  status: ClipStatus;
  storage_path: string | null;
  /** TASK 015 P4: which generation mode produced the clip. */
  mode: "highlights" | "full_coverage";
  /** TASK 015 P4: ffprobe-measured output (NULL until rendered + verified). */
  width?: number | null;
  height?: number | null;
  output_fps?: number | null;
  file_size_bytes?: number | null;
}

const SELECT_COLS =
  "id,title,start_time,end_time,duration,sort_order,status,storage_path,mode,width,height,output_fps,file_size_bytes";

/** Lists the clips of one job in presentation order (sort_order). */
export async function listClipsForJob(jobId: string): Promise<ClipRow[]> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("clips")
    .select(SELECT_COLS)
    .eq("job_id", jobId)
    .order("sort_order", { ascending: true });
  if (error) {
    throw new Error(`Clip listing failed: ${error.code ?? "unknown"}`);
  }
  return (data as ClipRow[]) ?? [];
}

/** Fetches ONE clip of ONE job (null when the clip does not belong to it). */
export async function findClipForJob(jobId: string, clipId: string): Promise<ClipRow | null> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("clips")
    .select(SELECT_COLS)
    .eq("job_id", jobId)
    .eq("id", clipId)
    .maybeSingle();
  if (error) {
    throw new Error(`Clip lookup failed: ${error.code ?? "unknown"}`);
  }
  return (data as ClipRow | null) ?? null;
}

// ---------------------------------------------------------------------------
// TASK 015 P4 — permanent-deletion state machine (atomic, job-scoped)
// ---------------------------------------------------------------------------
//
// The whole deletion protocol is built from ATOMIC conditional updates so
// that concurrent deletes, renders and downloads cannot interleave badly:
//
//   markClipDeleting:  status IN ('ready','failed') -> 'deleting'
//                      (a clip still pending/processing can never be
//                       grabbed — the update simply matches no row)
//   restoreClipStatus: 'deleting' -> previous   (storage step failed)
//   deleteClipRow:     delete WHERE status = 'deleting'
//
// Download/preview only ever serve `ready` rows, so a `deleting` clip is
// automatically un-downloadable the moment the transition is taken.

/** Atomically moves a clip into 'deleting' from one of `fromStatuses`;
 *  false = someone else owns the transition (or the clip is in a different
 *  state) — the caller must answer 409, never retry blindly. */
export async function markClipDeleting(
  jobId: string,
  clipId: string,
  fromStatuses: ClipStatus[]
): Promise<boolean> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("clips")
    .update({ status: "deleting" })
    .eq("job_id", jobId)
    .eq("id", clipId)
    .in("status", fromStatuses)
    .select("id");
  if (error) {
    throw new Error(`Clip status transition failed: ${error.code ?? "unknown"}`);
  }
  return (data?.length ?? 0) > 0;
}

/** Rolls a 'deleting' clip back to its previous status (storage step failed).
 *  Guarded on 'deleting' so it can never clobber a concurrent re-delete. */
export async function restoreClipStatus(
  jobId: string,
  clipId: string,
  previous: ClipStatus
): Promise<boolean> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("clips")
    .update({ status: previous })
    .eq("job_id", jobId)
    .eq("id", clipId)
    .eq("status", "deleting")
    .select("id");
  if (error) {
    throw new Error(`Clip status restore failed: ${error.code ?? "unknown"}`);
  }
  return (data?.length ?? 0) > 0;
}

/** Removes the row of a clip already in 'deleting' (its object is gone). */
export async function deleteClipRow(jobId: string, clipId: string): Promise<boolean> {
  const supabase = getSupabaseServerClient();
  const { error } = await supabase
    .from("clips")
    .delete()
    .eq("job_id", jobId)
    .eq("id", clipId)
    .eq("status", "deleting");
  if (error) {
    throw new Error(`Clip row deletion failed: ${error.code ?? "unknown"}`);
  }
  return true;
}

/** User-initiated job cleanup: records a partial failure (visible +
 *  retryable — the job stays un-purged while this is non-NULL). */
export async function setJobCleanupError(jobId: string, error: string | null): Promise<void> {
  const supabase = getSupabaseServerClient();
  const { error: updateError } = await supabase
    .from("jobs")
    .update({ cleanup_error: error })
    .eq("id", jobId);
  if (updateError) {
    throw new Error(`Job cleanup marker update failed: ${updateError.code ?? "unknown"}`);
  }
}

/**
 * Guarded purge marker — mirrors the Phase 6 semantics: only a TERMINAL
 * job that is still un-purged is marked, and the marker is set together
 * with a cleared cleanup_error (a successful cleanup is both).
 */
export async function markJobPurged(jobId: string): Promise<boolean> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .update({ purged_at: new Date().toISOString(), cleanup_error: null })
    .eq("id", jobId)
    .in("status", ["completed", "failed"])
    .is("purged_at", null)
    .select("id");
  if (error) {
    throw new Error(`Job purge marker update failed: ${error.code ?? "unknown"}`);
  }
  return (data?.length ?? 0) > 0;
}
