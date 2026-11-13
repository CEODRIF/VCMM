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
} as const;
