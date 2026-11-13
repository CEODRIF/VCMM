import { describe, expect, it } from "vitest";

import { SlidingWindowRateLimiter } from "@/lib/security/rate-limit";

const WINDOW = 1000;
const MAX = 3;

describe("sliding-window rate limiter", () => {
  it("allows up to max attempts within the window", () => {
    const limiter = new SlidingWindowRateLimiter({ windowMs: WINDOW, max: MAX });
    for (let i = 0; i < MAX; i++) {
      const result = limiter.check("ip-a", 1000 + i);
      expect(result.limited).toBe(false);
      expect(result.remaining).toBe(MAX - i - 1);
    }
  });

  it("blocks attempts beyond the maximum", () => {
    const limiter = new SlidingWindowRateLimiter({ windowMs: WINDOW, max: MAX });
    for (let i = 0; i < MAX; i++) limiter.check("ip-a", 1000 + i);
    const blocked = limiter.check("ip-a", 1500);
    expect(blocked.limited).toBe(true);
    expect(blocked.remaining).toBe(0);
    expect(blocked.retryAfterMs).toBeGreaterThan(0);
    expect(blocked.retryAfterMs).toBeLessThanOrEqual(WINDOW);
  });

  it("re-allows once the oldest hit slides out of the window", () => {
    const limiter = new SlidingWindowRateLimiter({ windowMs: WINDOW, max: MAX });
    limiter.check("ip-a", 0);
    limiter.check("ip-a", 1);
    limiter.check("ip-a", 2);
    expect(limiter.check("ip-a", 100).limited).toBe(true);
    // t=1001: hit at t=0 has left the window (t > 1001-1000=1 keeps t=1? t=1 is not > 1 -> also out)
    expect(limiter.check("ip-a", 1002).limited).toBe(false);
  });

  it("tracks keys independently", () => {
    const limiter = new SlidingWindowRateLimiter({ windowMs: WINDOW, max: MAX });
    for (let i = 0; i < MAX; i++) limiter.check("ip-a", 1000 + i);
    expect(limiter.check("ip-b", 1005).limited).toBe(false);
    expect(limiter.check("ip-a", 1005).limited).toBe(true);
  });

  it("reset clears a key", () => {
    const limiter = new SlidingWindowRateLimiter({ windowMs: WINDOW, max: MAX });
    for (let i = 0; i < MAX; i++) limiter.check("ip-a", 1000 + i);
    limiter.reset("ip-a");
    expect(limiter.check("ip-a", 1010).limited).toBe(false);
  });
});
