/**
 * Security configuration for ClipForge AI (Phase 2).
 *
 * Tunable values only — secrets NEVER live here. The access code hash is
 * read from the ACCESS_CODE_HASH server environment variable.
 */
export const securityConfig = {
  session: {
    /** HttpOnly cookie that carries the anonymous session token. */
    cookieName: "clipforge_session",
    /** Session lifetime in hours (temporary anonymous sessions). */
    ttlHours: 24,
    /** Token entropy in bytes (256 bits via crypto.randomBytes). */
    tokenBytes: 32,
  },
  /** Rate limiting for access-code attempts (per client IP). */
  rateLimit: {
    windowMs: 15 * 60 * 1000,
    maxAttempts: 5,
  },
  /**
   * Rate limiting for job CREATION (per session, not per IP — the session
   * token is unforgeable client-side, so it cannot be bypassed by IP
   * rotation; 10 jobs/minute is far above legitimate use and far below the
   * cost of a flood against the queue + worker pool).
   */
  jobCreationRateLimit: {
    windowMs: 60 * 1000,
    maxAttempts: 10,
  },
  /**
   * Maximum JSON request-body size in bytes for the small POST endpoints
   * (job creation, access code, upload authorize/complete). A legitimate
   * payload is a few hundred bytes; the cap exists to bound memory use,
   * not to constrain features.
   */
  bodyLimit: {
    maxBytes: 16 * 1024,
  },
} as const;
