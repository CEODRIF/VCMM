import "server-only";

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

/**
 * Cryptographic primitives for the access-code and session machinery.
 *
 * SECURITY: this module is server-only. The plaintext access code is never
 * stored or logged; only its SHA-256 hash exists (in the server environment).
 * Session tokens are 256-bit random values; only their SHA-256 hash is
 * persisted in the database.
 */

/** SHA-256 of a string as lowercase hex. */
export function sha256Hex(input: string): string {
  return createHash("sha256").update(input, "utf8").digest("hex");
}

/**
 * Constant-time string comparison.
 *
 * Both inputs are hashed first so the comparison never leaks length
 * information and always compares equal-length buffers.
 */
export function constantTimeEqual(a: string, b: string): boolean {
  const hashA = createHash("sha256").update(a, "utf8").digest();
  const hashB = createHash("sha256").update(b, "utf8").digest();
  return timingSafeEqual(hashA, hashB);
}

/** Generates a cryptographically secure session token (64 hex chars, 256 bits). */
export function generateSessionToken(byteLength = 32): string {
  return randomBytes(byteLength).toString("hex");
}

/** SHA-256 hash of a shared access code (what ACCESS_CODE_HASH stores). */
export function hashAccessCode(code: string): string {
  return sha256Hex(code.trim());
}

/**
 * Normalizes a stored access-code hash: accepts either a bare SHA-256 hex
 * digest or the documented `sha256:<hex>` format.
 */
export function normalizeAccessCodeHash(stored: string): string {
  const trimmed = stored.trim();
  if (trimmed.toLowerCase().startsWith("sha256:")) {
    return trimmed.slice(7).trim().toLowerCase();
  }
  return trimmed.toLowerCase();
}
