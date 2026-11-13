import "server-only";

import { NextResponse } from "next/server";

import { BodyTooLargeError, readJsonBody } from "@/lib/api/body";
import { toErrorResponse } from "@/lib/api/errors";
import { securityConfig } from "@/config/security";
import { getClientIp, accessCodeRateLimiter } from "@/lib/security/rate-limit";
import {
  buildClearSessionCookie,
  buildSessionCookie,
  createSession,
  destroySession,
  getSessionToken,
  SESSION_TTL_SECONDS,
} from "@/lib/security/session";
import { constantTimeEqual, hashAccessCode, normalizeAccessCodeHash } from "@/lib/security/crypto";

/**
 * POST /api/access — verify the shared access code (rate-limited).
 *
 * Security properties:
 *   - The plaintext code is compared ONLY via its SHA-256 hash, in constant
 *     time, against ACCESS_CODE_HASH (server environment variable).
 *   - The code is never logged, stored, or echoed back.
 *   - Successful verification creates a temporary anonymous session: a
 *     256-bit random token in an HttpOnly cookie; only its hash is stored.
 *   - Attempts are rate-limited per client IP (5 / 15 minutes).
 */
export async function POST(request: Request) {
  try {
    // 1. Rate limit first (before touching the secret).
    const ip = getClientIp(request);
    const limit = accessCodeRateLimiter.check(ip);
    if (limit.limited) {
      const minutes = Math.max(1, Math.ceil(limit.retryAfterMs / 60000));
      const res = NextResponse.json(
        { error: `Too many attempts. Try again in about ${minutes} minute${minutes === 1 ? "" : "s"}.` },
        { status: 429 }
      );
      res.headers.set("Retry-After", String(Math.max(1, Math.ceil(limit.retryAfterMs / 1000))));
      return res;
    }

    // 2. Server configuration check (do not leak configuration state).
    const storedHash = process.env.ACCESS_CODE_HASH;
    if (!storedHash || !storedHash.trim()) {
      return NextResponse.json(
        { error: "Access service is not configured on this deployment." },
        { status: 503 }
      );
    }

    // 3. Parse and verify (bounded body read — TASK 012).
    let body: unknown;
    try {
      body = await readJsonBody(request, securityConfig.bodyLimit.maxBytes);
    } catch (err) {
      if (err instanceof BodyTooLargeError) {
        return NextResponse.json({ error: "Request payload is too large." }, { status: 413 });
      }
      return NextResponse.json({ error: "Invalid JSON payload." }, { status: 400 });
    }
    const code = typeof (body as { code?: unknown })?.code === "string" ? ((body as { code: string }).code) : "";
    if (code.length === 0) {
      return NextResponse.json({ error: "Provide the access code." }, { status: 400 });
    }

    const providedHash = hashAccessCode(code);
    if (!constantTimeEqual(providedHash, normalizeAccessCodeHash(storedHash))) {
      return NextResponse.json({ error: "Invalid access code." }, { status: 401 });
    }

    // 4. Issue a temporary anonymous session.
    const session = await createSession();
    const response = NextResponse.json(
      { ok: true, expiresInHours: securityConfig.session.ttlHours },
      { status: 200 }
    );
    response.headers.set("set-cookie", buildSessionCookie(session.token, SESSION_TTL_SECONDS, process.env.NODE_ENV === "production"));
    return response;
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}

/**
 * DELETE /api/access — sign out: destroys the session row (if any) and
 * clears the cookie. Idempotent.
 */
export async function DELETE(request: Request) {
  try {
    const token = getSessionToken(request);
    if (token) {
      await destroySession(token);
    }
    const response = NextResponse.json({ ok: true });
    response.headers.set("set-cookie", buildClearSessionCookie());
    return response;
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
