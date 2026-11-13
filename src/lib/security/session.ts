import "server-only";

import { securityConfig } from "@/config/security";
import { generateSessionToken, sha256Hex } from "@/lib/security/crypto";
import { createSessionRow, deleteSessionRow, findSessionByTokenHash } from "@/lib/db/sessions";

/**
 * Temporary anonymous session management (Phase 2).
 *
 * Flow:
 *   1. Server verifies the shared access code (constant-time hash compare).
 *   2. A 256-bit random token is generated; ONLY its SHA-256 hash is stored
 *      in `processing_sessions` together with an expiry.
 *   3. The raw token travels in an HttpOnly, SameSite=Lax cookie.
 *
 * The browser never sees the access code hash, the session hash, or any
 * database credential.
 */

export interface ActiveSession {
  sessionId: string;
  expiresAt: string;
}

export interface CreatedSession extends ActiveSession {
  /** Raw token — must go into the HttpOnly cookie and nowhere else. */
  token: string;
}

/** Verifies a raw session token from a cookie. Returns null when absent/invalid/expired. */
export async function verifySessionToken(token: string | undefined | null): Promise<ActiveSession | null> {
  if (!token || typeof token !== "string") return null;
  const tokenHash = sha256Hex(token);
  const row = await findSessionByTokenHash(tokenHash);
  if (!row) return null;
  if (new Date(row.expires_at).getTime() <= Date.now()) {
    // Expired: clean up and reject.
    await deleteSessionRow(row.id).catch(() => undefined);
    return null;
  }
  return { sessionId: row.id, expiresAt: row.expires_at };
}

/** Creates a session row and returns the raw token plus metadata. */
export async function createSession(): Promise<CreatedSession> {
  const token = generateSessionToken(securityConfig.session.tokenBytes);
  const expiresAt = new Date(Date.now() + securityConfig.session.ttlHours * 60 * 60 * 1000).toISOString();
  const id = await createSessionRow(sha256Hex(token), expiresAt);
  return { sessionId: id, token, expiresAt };
}

/** Destroys a session (logout). Safe to call with unknown tokens. */
export async function destroySession(token: string | undefined | null): Promise<void> {
  if (!token) return;
  const row = await findSessionByTokenHash(sha256Hex(token)).catch(() => null);
  if (row) await deleteSessionRow(row.id).catch(() => undefined);
}

/**
 * Builds the Set-Cookie header value for the session cookie.
 * HttpOnly (invisible to JS), SameSite=Lax (CSRF posture), Secure in
 * production, bounded path, lifetime = session TTL.
 */
export function buildSessionCookie(token: string, ttlSeconds: number, isProduction: boolean): string {
  const parts = [
    `${securityConfig.session.cookieName}=${token}`,
    "HttpOnly",
    "SameSite=Lax",
    "Path=/",
    `Max-Age=${Math.max(0, Math.floor(ttlSeconds))}`,
  ];
  if (isProduction) parts.push("Secure");
  return parts.join("; ");
}

/** Builds a Set-Cookie header that clears the session cookie immediately. */
export function buildClearSessionCookie(): string {
  return buildSessionCookie("expired", 0, false);
}

/** Parses a raw Cookie header into a name → value map. */
export function parseCookieHeader(header: string | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!header) return out;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    const name = part.slice(0, eq).trim();
    const value = part.slice(eq + 1).trim();
    if (name) out[name] = value;
  }
  return out;
}

/** Extracts the session token from a Request (or undefined). */
export function getSessionToken(request: Request): string | undefined {
  const cookies = parseCookieHeader(request.headers.get("cookie"));
  return cookies[securityConfig.session.cookieName];
}

export const SESSION_TTL_SECONDS = securityConfig.session.ttlHours * 60 * 60;
