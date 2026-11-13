import "server-only";

import { z } from "zod";

/**
 * Server-side environment variable validation.
 *
 * SECURITY: this module must only be imported from server components,
 * server actions, or API route handlers. It references
 * SUPABASE_SERVICE_ROLE_KEY and ACCESS_CODE_HASH, which must never be
 * shipped to the browser.
 */
/**
 * Strict URL check: zod's built-in `url()` accepts scheme-less hostnames like
 * `localhost:3000` (parsed as a custom-scheme URL), which we must reject.
 */
const httpUrl = z
  .string()
  .url()
  .refine((value) => value.startsWith("http://") || value.startsWith("https://"), {
    message: "must be an http(s) URL",
  });

export const serverEnvSchema = z.object({
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
  SUPABASE_URL: httpUrl.optional(),
  SUPABASE_SERVICE_ROLE_KEY: z.string().min(1).optional(),
  ACCESS_CODE_HASH: z.string().min(1).optional(),
  DATABASE_URL: z.string().min(1).optional(),
});

/**
 * Client-safe environment variables. Only NEXT_PUBLIC_ values are allowed
 * in browser bundles; anything else is a configuration error.
 */
export const clientEnvSchema = z.object({
  NEXT_PUBLIC_APP_URL: httpUrl.optional(),
  NEXT_PUBLIC_SUPABASE_URL: httpUrl.optional(),
  NEXT_PUBLIC_SUPABASE_ANON_KEY: z.string().min(1).optional(),
});

export type ServerEnv = z.infer<typeof serverEnvSchema>;
export type ClientEnv = z.infer<typeof clientEnvSchema>;

export interface EnvParseResult<T> {
  success: boolean;
  data?: T;
  errors?: string[];
}

function toErrorList(error: z.ZodError): string[] {
  return error.issues.map((issue) => {
    const key = issue.path.join(".") || "(root)";
    return `${key}: ${issue.message}`;
  });
}

/** Validates server-side environment variables. */
export function parseServerEnv(env: Record<string, string | undefined> = process.env): EnvParseResult<ServerEnv> {
  const result = serverEnvSchema.safeParse(env);
  if (result.success) return { success: true, data: result.data };
  return { success: false, errors: toErrorList(result.error) };
}

/** Validates client-side (NEXT_PUBLIC_*) environment variables. */
export function parseClientEnv(env: Record<string, string | undefined> = process.env): EnvParseResult<ClientEnv> {
  const result = clientEnvSchema.safeParse(env);
  if (result.success) return { success: true, data: result.data };
  return { success: false, errors: toErrorList(result.error) };
}
