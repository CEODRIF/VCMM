import "server-only";

import { ApiError } from "@/lib/api/errors";
import { getSessionToken, verifySessionToken, type ActiveSession } from "@/lib/security/session";

/**
 * Server-side authorization gate for protected routes.
 *
 * The session token is read from the HttpOnly cookie on the incoming request
 * and verified against the database (hash lookup + expiry). Client-supplied
 * session ids, job ownership claims, or body fields are NEVER trusted.
 */
export async function requireSession(request: Request): Promise<ActiveSession> {
  const token = getSessionToken(request);
  if (!token) {
    throw new ApiError(401, "Authentication required. Enter your access code first.");
  }
  const session = await verifySessionToken(token);
  if (!session) {
    throw new ApiError(401, "Your session has expired. Enter your access code again.");
  }
  return session;
}
