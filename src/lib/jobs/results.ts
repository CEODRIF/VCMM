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

export type ClipStatus = "pending" | "processing" | "ready" | "failed";

export interface ClipRow {
  id: string;
  title: string;
  start_time: number;
  end_time: number;
  duration: number;
  sort_order: number;
  status: ClipStatus;
  storage_path: string | null;
}

const SELECT_COLS = "id,title,start_time,end_time,duration,sort_order,status,storage_path";

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
