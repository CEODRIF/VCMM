import "server-only";

import { securityConfig } from "@/config/security";

/**
 * In-memory sliding-window rate limiter.
 *
 * NOTE (documented limitation): on Vercel each instance holds its own window,
 * so the effective limit is per-instance. For a globally exact limit, swap in
 * a Redis/Upstash-backed implementation (Phase 7). The API surface stays the
 * same.
 */

export interface RateLimitResult {
  limited: boolean;
  remaining: number;
  /** Milliseconds until the oldest hit leaves the window (0 when allowed). */
  retryAfterMs: number;
}

export class SlidingWindowRateLimiter {
  private hits = new Map<string, number[]>();

  constructor(
    private readonly options: { windowMs: number; max: number }
  ) {}

  /**
   * Records an attempt for `key` and reports whether it is within the limit.
   * `now` is injectable for deterministic tests.
   */
  check(key: string, now: number = Date.now()): RateLimitResult {
    const windowStart = now - this.options.windowMs;
    const recent = (this.hits.get(key) ?? []).filter((t) => t > windowStart);

    if (recent.length >= this.options.max) {
      this.hits.set(key, recent);
      const oldest = recent[0] ?? now;
      return { limited: true, remaining: 0, retryAfterMs: Math.max(0, oldest + this.options.windowMs - now) };
    }

    recent.push(now);
    this.hits.set(key, recent);
    return { limited: false, remaining: this.options.max - recent.length, retryAfterMs: 0 };
  }

  /** Drops all entries (test helper). */
  resetAll(): void {
    this.hits.clear();
  }

  /** Drops entries for one key (test helper). */
  reset(key: string): void {
    this.hits.delete(key);
  }
}

/** Shared limiter for access-code attempts (per client IP). */
export const accessCodeRateLimiter = new SlidingWindowRateLimiter({
  windowMs: securityConfig.rateLimit.windowMs,
  max: securityConfig.rateLimit.maxAttempts,
});

/**
 * Best-effort client IP extraction. Behind Vercel the first entry of
 * X-Forwarded-For is the original client; in dev it falls back to a
 * fixed key so limits still apply locally.
 */
export function getClientIp(request: Request): string {
  const forwarded = request.headers.get("x-forwarded-for");
  if (forwarded) {
    const first = forwarded.split(",")[0]?.trim();
    if (first) return first;
  }
  return "local-dev";
}
