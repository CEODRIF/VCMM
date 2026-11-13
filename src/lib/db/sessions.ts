import "server-only";

import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Session data access (server-only, service role).
 *
 * Only token HASHES cross the wire to the database. Row shape mirrors
 * `processing_sessions` from the Phase 1 migration.
 */

export interface SessionRow {
  id: string;
  expires_at: string;
}

export async function findSessionByTokenHash(tokenHash: string): Promise<SessionRow | null> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("processing_sessions")
    .select("id, expires_at")
    .eq("token_hash", tokenHash)
    .maybeSingle();
  if (error) {
    throw new Error(`Session lookup failed: ${error.code ?? "unknown"}`);
  }
  return (data as SessionRow | null) ?? null;
}

/** Inserts a session row; returns its generated id. */
export async function createSessionRow(tokenHash: string, expiresAt: string): Promise<string> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("processing_sessions")
    .insert({ token_hash: tokenHash, expires_at: expiresAt })
    .select("id")
    .single();
  if (error) {
    throw new Error(`Session creation failed: ${error.code ?? "unknown"}`);
  }
  return (data as { id: string }).id;
}

export async function deleteSessionRow(sessionId: string): Promise<void> {
  const supabase = getSupabaseServerClient();
  const { error } = await supabase.from("processing_sessions").delete().eq("id", sessionId);
  if (error) {
    throw new Error(`Session deletion failed: ${error.code ?? "unknown"}`);
  }
}
