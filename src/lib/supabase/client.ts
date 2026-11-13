import { createClient, type SupabaseClient } from "@supabase/supabase-js";

let cachedClient: SupabaseClient | null = null;

/**
 * Browser-side Supabase client.
 *
 * Uses the public anon key only (NEXT_PUBLIC_SUPABASE_ANON_KEY). Row Level
 * Security on every table denies direct anonymous access by default, so the
 * browser client is only useful once permissive, session-scoped policies are
 * introduced (Phase 2+).
 */
export function getSupabaseBrowserClient(): SupabaseClient {
  if (cachedClient) return cachedClient;

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!url || !anonKey) {
    throw new Error(
      "Supabase is not configured. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY in your environment (see .env.example)."
    );
  }

  cachedClient = createClient(url, anonKey);
  return cachedClient;
}
