import "server-only";

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

let cachedClient: SupabaseClient | null = null;

/**
 * Server-only Supabase client.
 *
 * SECURITY: this client uses SUPABASE_SERVICE_ROLE_KEY, which bypasses Row
 * Level Security. It must ONLY be imported from server code (route handlers,
 * server components, server actions). Never import it into client
 * components, and never place the service-role key in NEXT_PUBLIC_* vars.
 */
export function getSupabaseServerClient(): SupabaseClient {
  if (cachedClient) return cachedClient;

  const url = process.env.SUPABASE_URL ?? process.env.NEXT_PUBLIC_SUPABASE_URL;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

  if (!url || !serviceRoleKey) {
    throw new Error(
      "Server-side Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in your server environment (see .env.example). The service-role key must never be exposed to the client."
    );
  }

  cachedClient = createClient(url, serviceRoleKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  });
  return cachedClient;
}
